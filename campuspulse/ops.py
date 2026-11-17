"""Campus operations: the event side of the agent workflow.

    readings ──► WATCHER (rules) ──event──► ORCHESTRATOR (Gemini, agent/team.py) ──tools──┐
                                                ▲                                         │
                                                │                inspect_room, recent_incidents,
                                                │                ask_campus_data (SQL analyst), ...
                                                │                set_ac_mode ──► POLICY GUARD (rules)
                                                │                                  ├─ safe: apply ──► device command
                                                │                                  └─ else: hold for a person
                                                │                schedule_check ──► VERIFIER (rules)
                                                └──── verification result ◄────────────┘
                                                close_incident (note checked by the CHECKER)

The watcher, guard and verifier are deterministic code: the orchestrator decides what to do, but it
cannot switch a room off on its own say-so, and it cannot mark its own homework. Every step is
recorded on the incident, which the dashboard shows as the agent activity feed.
"""
from __future__ import annotations

import itertools
import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .campus import LIVE_ROOM, ROOM_BY_ID, slots_for
from .config import settings
from .control import MODE_TEXT, MODES, control
from .network import is_camera, is_replay, is_test_device, is_virtual

log = logging.getLogger("campuspulse.ops")

LIVE_DWELL_S = 20          # a node's room must look wrong for this long before the watcher speaks up
SIM_INCIDENT_GAP_S = 600   # simulated rooms: at most one new incident every 10 minutes, campus-wide
COOLDOWN_S = {"live": 60, "sim": 1800}
VERIFY_AFTER_S = {"live": 15, "sim": 300}
VERIFY_GIVE_UP_S = 900
MAX_AUTO_CHANGES_PER_DAY = 20

_ids = itertools.count(1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _is_node(device_id: str) -> bool:
    """A real or virtual node (not the simulator): it gets the fast, demo-scale watcher."""
    return not str(device_id).startswith("sim-")


class Ops:
    def __init__(self):
        self.lock = threading.RLock()
        self.incidents: deque[dict] = deque(maxlen=40)
        self.queue: queue.Queue = queue.Queue()
        self.autonomy = True
        self.streak: dict[tuple[str, str], datetime] = {}   # (room, event) -> first seen
        self.closed_at: dict[tuple[str, str], datetime] = {}
        self.last_sim_incident: datetime | None = None
        self.auto_changes: list[datetime] = []
        self.worker: threading.Thread | None = None
        self.run_trigger = None  # set by the API: fn(trigger: dict) -> None, runs the orchestrator
        self.manual = False      # tests: no background worker; call drain()

    # ---------------------------------------------------------------- incidents
    def get(self, iid: str) -> dict | None:
        with self.lock:
            return next((i for i in self.incidents if i["id"] == iid), None)

    def open_for(self, room: str, event: str | None = None) -> dict | None:
        with self.lock:
            return next((i for i in self.incidents if i["room_id"] == room and i["status"] not in ("resolved", "escalated", "no_action")
                         and (event is None or i["event"] == event)), None)

    def step(self, inc: dict, agent: str, did: str, detail: str = "", ok: bool = True) -> None:
        with self.lock:
            inc["steps"].append({"at": _iso(_now()), "agent": agent, "did": did, "detail": str(detail)[:400], "ok": ok})

    def set_status(self, inc: dict, status: str) -> None:
        with self.lock:
            inc["status"] = status
            if status in ("resolved", "escalated", "no_action"):
                inc["closed_at"] = _iso(_now())
                self.closed_at[(inc["room_id"], inc["event"])] = _now()

    def feed(self) -> list[dict]:
        with self.lock:
            return [dict(i, steps=list(i["steps"])) for i in self.incidents]

    # ---------------------------------------------------------------- watcher
    def watch(self, rows: list[dict]) -> None:
        """Rules over each new reading (no LLM). Opens an incident and hands it to the orchestrator."""
        if not self.autonomy:
            return
        for r in rows:
            dev = str(r.get("device_id", ""))
            if is_camera(dev) or is_test_device(dev) or is_replay(dev) or r.get("room_id") not in ROOM_BY_ID:
                continue
            if not _is_node(dev) and not settings.agent_watch_sim:
                continue
            try:
                self._watch_one(r, dev)
            except Exception:
                log.exception("watcher failed on a reading")

    def _watch_one(self, r: dict, dev: str) -> None:
        room, now = r["room_id"], _now()
        node = _is_node(dev)
        self._check_verification(room, r, now)
        occupied = bool(r.get("occupied"))
        co2 = r.get("co2_ppm")
        events = {
            "empty_cooled": (not occupied) and bool(r.get("ac_on")) and control.mode(room) != "occupancy",
            "stale_air": occupied and co2 is not None and co2 == co2 and float(co2) > settings.co2_limit_ppm,
        }
        for event, firing in events.items():
            key = (room, event)
            if not firing:
                self.streak.pop(key, None)
                continue
            first = self.streak.setdefault(key, now)
            if node and (now - first).total_seconds() < LIVE_DWELL_S:
                continue
            if self.open_for(room, event):
                continue
            closed = self.closed_at.get(key)
            if closed and (now - closed).total_seconds() < COOLDOWN_S["live" if node else "sim"]:
                continue
            if not node:
                if self.last_sim_incident and (now - self.last_sim_incident).total_seconds() < SIM_INCIDENT_GAP_S:
                    continue
                self.last_sim_incident = now
            self.open(room, event, r, dev, first)

    def open(self, room: str, event: str, r: dict, dev: str, since: datetime | None = None) -> dict:
        what = {"empty_cooled": "AC running with nobody in the room",
                "stale_air": f"CO2 {round(float(r.get('co2_ppm') or 0))} ppm while occupied (limit {settings.co2_limit_ppm})"}[event]
        inc = {"id": f"inc-{next(_ids)}", "room_id": room, "room_name": ROOM_BY_ID[room].name, "event": event,
               "what": what, "device_id": dev, "source": "live node" if not dev.startswith("sim-") and not is_virtual(dev)
               else "virtual node" if is_virtual(dev) else "simulated room",
               "opened_at": _iso(_now()), "since": _iso(since), "status": "investigating", "steps": [],
               "note": None, "proposal": None, "check": None, "closed_at": None, "orchestrated_by": None}
        with self.lock:
            self.incidents.appendleft(inc)
        secs = round((_now() - since).total_seconds()) if since else 0
        self.step(inc, "Watcher (rules)", f"spotted: {what}", f"{inc['source']} {dev}; condition held {secs} s")
        self.queue.put({"kind": "event", "incident": inc["id"]})
        self._ensure_worker()
        return inc

    # ---------------------------------------------------------------- tools the orchestrator calls
    def inspect_room(self, room: str) -> dict:
        """Everything the orchestrator needs to judge one room: now, the timetable, trust, the AC mode."""
        from . import analytics
        from .store import get_store
        if room not in ROOM_BY_ID:
            return {"error": f"unknown room {room}"}
        meta = ROOM_BY_ID[room]
        state = next((x for x in analytics.room_states(get_store()) if x["room_id"] == room), {})
        local = datetime.now(settings.timezone)
        mins = local.hour * 60 + local.minute
        today = sorted(slots_for(room, local.weekday()), key=lambda s: s.start_min)
        now_cls = next((s for s in today if s.start_min <= mins < s.end_min), None)
        nxt = next((s for s in today if s.start_min > mins), None)
        out = {"room_id": room, "name": meta.name, "seats": meta.capacity, "ac_kw": meta.ac_kw,
               "local_time": local.strftime("%a %H:%M"),
               "occupied": state.get("occupied"), "people": state.get("people"), "co2_ppm": state.get("co2_ppm"),
               "temp_c": state.get("temp_c"), "ac_on": state.get("ac_on"), "status": state.get("status"),
               "data_source": "simulated" if state.get("simulated", True) else "live sensor",
               "data_confidence": state.get("confidence"), "minutes_since_reading": state.get("minutes_ago"),
               "ac_mode": control.mode(room), "ac_mode_meaning": MODE_TEXT[control.mode(room)],
               "class_now": f"{now_cls.module} {now_cls.time_label}" if now_cls else None,
               "next_class": f"{nxt.module} {nxt.time_label}" if nxt else None,
               "minutes_to_next_class": (nxt.start_min - mins) if nxt else None,
               "classes_today": len(today)}
        try:
            wk = get_store().query(f"""SELECT COUNT(*) AS hours, COALESCE(SUM(kwh), 0) AS kwh FROM v_empty_cooled
                WHERE room_id = '{room}' AND hour >= DATETIME_SUB((SELECT MAX(hour) FROM v_room_hourly), INTERVAL 7 DAY)""")
            out["empty_cooled_hours_7d"] = int(wk["hours"][0])
            out["empty_cooled_kwh_7d"] = round(float(wk["kwh"][0]), 1)
        except Exception as e:  # history is context, not a requirement
            out["history_error"] = type(e).__name__
        return out

    def recent_incidents(self, n: int = 8) -> list[dict]:
        with self.lock:
            return [{k: i.get(k) for k in ("id", "room_id", "event", "what", "status", "opened_at", "note")}
                    for i in list(self.incidents)[:n]]

    def set_ac_mode(self, inc: dict | None, room: str, mode: str, reason: str, by: str) -> dict:
        """POLICY GUARD (rules): the orchestrator asks; this decides whether it happens now or waits for a person."""
        if room not in ROOM_BY_ID or mode not in MODES:
            return {"applied": False, "held": False, "why": f"refused: unknown room or mode ({room}, {mode})"}
        current = control.mode(room)
        if current == mode:
            return {"applied": False, "held": False, "why": f"{room} is already on '{MODE_TEXT[mode]}'"}
        rank = {m: i for i, m in enumerate(MODES)}
        less_cooling = rank[mode] > rank[current]
        info = self.inspect_room(room)
        reasons = []
        if less_cooling:
            if not self.autonomy:
                reasons.append("autonomy is switched off")
            if info.get("occupied"):
                reasons.append("the room is occupied right now")
            if info.get("data_confidence") is not None and info["data_confidence"] < 80:
                reasons.append(f"its sensor is not trusted (confidence {info['data_confidence']})")
            today = [t for t in self.auto_changes if (_now() - t).total_seconds() < 86400]
            if len(today) >= MAX_AUTO_CHANGES_PER_DAY:
                reasons.append(f"the daily limit of {MAX_AUTO_CHANGES_PER_DAY} automatic changes is reached")
        proposal = {"room_id": room, "mode": mode, "from": current, "reason": reason[:300]}
        if reasons:
            why = "held for a person: " + "; ".join(reasons)
            if inc:
                with self.lock:
                    inc["proposal"] = proposal | {"held_because": why}
                self.step(inc, "Policy guard (rules)", why, f"{current} -> {mode}", ok=False)
                self.set_status(inc, "awaiting_approval")
            return {"applied": False, "held": True, "why": why}
        cmd = self._apply(room, mode, inc["id"] if inc else f"manual-{next(_ids)}", reason, by)
        if less_cooling:
            self.auto_changes.append(_now())
        why = ("safe and reversible: room empty, sensor trusted; AC still pre-cools before booked classes"
               if less_cooling else "more cooling is always allowed (comfort first)")
        if inc:
            with self.lock:
                inc["proposal"] = proposal | {"applied_at": _iso(_now())}
            self.step(inc, "Policy guard (rules)", f"approved: {why}", f"{current} -> {mode}")
            self.step(inc, "Device command", f"AC mode '{MODE_TEXT[mode]}' sent to {room}",
                      cmd.get("delivered") or "", ok=True)
            self.set_status(inc, "acted")
        return {"applied": True, "held": False, "why": why, "delivered": cmd.get("delivered")}

    def _apply(self, room: str, mode: str, ref: str, reason: str, by: str) -> dict:
        kind = {"occupancy": "occupancy_linked_ac", "after_hours_off": "after_hours_ac"}.get(mode)
        action = {"id": f"agent-{room}", "kind": kind or "occupancy_linked_ac", "rooms": [room],
                  "title": f"Agent: {MODE_TEXT[mode]} in {room} ({ref})"}
        cmds = control.apply(action, "approved" if kind else "dismissed", decided_by=by)
        cmd = next((c for c in cmds if c["room_id"] == room), {"delivered": "no change needed"})
        if cmd.get("delivered") == "sending…":
            cmd = cmd | {"delivered": "published over MQTT (retained); the node can also pull it over HTTPS"}
        return cmd

    def flag_for_estates(self, inc: dict | None, room: str, message: str) -> dict:
        if inc:
            with self.lock:
                inc["flag"] = message[:300]
            self.step(inc, "Estates queue", f"flagged for a person: {message[:160]}")
        return {"queued": True}

    def schedule_check(self, inc: dict | None, room: str, expect: str) -> dict:
        """VERIFIER (rules): re-read the room after the action and see whether it worked."""
        if inc is None:
            return {"scheduled": False, "why": "checks belong to an incident"}
        if expect not in ("ac_off", "co2_below_limit"):
            expect = "ac_off" if inc["event"] == "empty_cooled" else "co2_below_limit"
        node = inc["source"] != "simulated room"
        due = _now() + timedelta(seconds=VERIFY_AFTER_S["live" if node else "sim"])
        with self.lock:
            inc["check"] = {"expect": expect, "due": _iso(due), "result": None}
        self.step(inc, "Verifier (rules)", f"will re-read {room} after {due.astimezone(settings.timezone):%H:%M:%S}",
                  "expects the AC to be off" if expect == "ac_off" else f"expects CO2 under {settings.co2_limit_ppm} ppm")
        if inc["status"] == "acted":
            self.set_status(inc, "verifying")
        return {"scheduled": True, "due": _iso(due), "expect": expect}

    def close(self, inc: dict, outcome: str, note: str, by: str) -> None:
        with self.lock:
            inc["note"] = note
            inc["orchestrated_by"] = by
        self.set_status(inc, outcome)

    def _check_verification(self, room: str, r: dict, now: datetime) -> None:
        with self.lock:
            due = [i for i in self.incidents if i["room_id"] == room and i.get("check") and i["check"]["result"] is None]
        for inc in due:
            if now < datetime.fromisoformat(inc["check"]["due"]):
                continue
            expect = inc["check"]["expect"]
            if expect == "ac_off":
                passed = not bool(r.get("ac_on"))
                seen = f"AC {'off' if passed else 'still on'} in the reading from {r.get('device_id')}"
            else:
                co2 = r.get("co2_ppm")
                passed = co2 is not None and float(co2) <= settings.co2_limit_ppm
                seen = f"CO2 {round(float(co2)) if co2 is not None else '–'} ppm"
            with self.lock:
                inc["check"]["result"] = "passed" if passed else "failed"
                inc["check"]["seen"] = seen
            self.step(inc, "Verifier (rules)", "check passed" if passed else "check failed", seen, ok=passed)
            self.queue.put({"kind": "verification", "incident": inc["id"]})
            self._ensure_worker()

    def sweep(self) -> None:
        """Give up on checks whose room went silent, so nothing waits forever."""
        now = _now()
        with self.lock:
            stale = [i for i in self.incidents if i.get("check") and i["check"]["result"] is None
                     and (now - datetime.fromisoformat(i["check"]["due"])).total_seconds() > VERIFY_GIVE_UP_S]
        for inc in stale:
            with self.lock:
                inc["check"]["result"] = "no data"
            self.step(inc, "Verifier (rules)", "no new reading from the room to check against", ok=False)
            self.queue.put({"kind": "verification", "incident": inc["id"]})

    # ---------------------------------------------------------------- the worker
    def _ensure_worker(self) -> None:
        if self.manual:
            return
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.worker = threading.Thread(target=self._work, daemon=True, name="campuspulse-ops")
            self.worker.start()

    def _work(self) -> None:
        while True:
            try:
                trigger = self.queue.get(timeout=30)
            except queue.Empty:
                self.sweep()
                continue
            try:
                if self.run_trigger:
                    self.run_trigger(trigger)
            except Exception:
                log.exception("orchestrator run failed")
                inc = self.get(trigger.get("incident", ""))
                if inc:
                    self.step(inc, "Orchestrator", "run failed; left for a person", ok=False)
                    self.set_status(inc, "escalated")

    def drain(self, timeout: float = 60.0) -> None:
        """Tests: run queued triggers on this thread until the queue is empty."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                trigger = self.queue.get_nowait()
            except queue.Empty:
                return
            if self.run_trigger:
                self.run_trigger(trigger)


ops = Ops()


def as_text(data) -> str:
    return json.dumps(data, default=str)[:3000]

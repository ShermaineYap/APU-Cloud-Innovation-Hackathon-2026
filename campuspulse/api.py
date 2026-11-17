"""CampusPulse service: ingest, decision API and the web app, in one Cloud Run container."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, analytics, decisions, network, report, study
from . import autopilot
from .agent import ask, chart_caption, daily_brief, draft_notices, explain_plan, plan_constraints, suggestions
from .campus import LIVE_ROOM, ROOM_BY_ID
from .network import is_camera, is_replay, is_test_device, is_virtual
from .config import settings
from .control import control
from .weather import weather
from .replay import replayer
from .ops import ops
from .simulator import Fleet
from .store import get_store

log = logging.getLogger("campuspulse")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB = Path(__file__).resolve().parents[1] / "web" / "static"


# --- cache: decisions are recomputed at most once a minute ---------------------

class Cache:
    def __init__(self):
        self.data: dict[str, tuple[float, object]] = {}
        self.lock = threading.Lock()

    def get(self, key, ttl, fn, force=False):
        with self.lock:
            hit = self.data.get(key)
        if hit and not force and time.time() - hit[0] < ttl:
            return hit[1]
        value = fn()
        with self.lock:
            self.data[key] = (time.time(), value)
        return value

    def clear(self):
        with self.lock:
            self.data.clear()

    def drop(self, prefix: str):
        with self.lock:
            for k in [k for k in self.data if k.startswith(prefix)]:
                del self.data[k]


cache = Cache()
state = {"last_real": None, "real_count": 0, "started": datetime.now(timezone.utc), "real_rooms": {}}


def _real_kinds(room_id: str, within_s: int = 300) -> set[str]:
    now = datetime.now(timezone.utc)
    return {kind for (rid, kind), t in state["real_rooms"].items()
            if rid == room_id and (now - t).total_seconds() < within_s}


def _store_simulated(rows: list[dict]) -> None:
    """Write simulated rows. Where real hardware is reporting, it replaces the simulated twin:
    a live sensor node replaces the whole row, a live camera replaces just the head count."""
    out = []
    for r in rows:
        kinds = _real_kinds(r["room_id"])
        if "sensor" in kinds:
            continue
        if "camera" in kinds:
            r = r | {"people": None}
        out.append(r)
    if out:
        get_store().insert_readings(out)
        cache.drop("overview")
        ops.watch(out)  # the agent loop's watcher sees simulated rooms too (rate-limited)


def _live_loop(fleet: Fleet, stop: threading.Event):
    while not stop.wait(30):
        try:
            _sync_control()
            _store_simulated(fleet.tick(datetime.now(timezone.utc)))
        except Exception:  # keep the demo alive
            log.exception("live simulator tick failed")


sim = {"fleet": None, "lock": threading.Lock()}


def _catch_up() -> None:
    """Request mode: produce the readings a background loop would have written since the last request."""
    fleet = sim["fleet"]
    if fleet is None or not sim["lock"].acquire(blocking=False):
        return  # not started, or another request is already catching up
    try:
        try:
            _sync_control()
        except Exception:  # AC modes can catch up later; readings must not wait for them
            log.exception("control sync failed")
        _store_simulated(fleet.catch_up(datetime.now(timezone.utc)))
    except Exception:
        log.exception("simulator catch-up failed")
    finally:
        sim["lock"].release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = get_store()
    if settings.setup_on_start:
        store.setup()
    weather.refresh()  # real KL weather before the simulator runs
    fleet = Fleet()
    fleet.policies = control.policies  # simulated rooms obey approved AC changes
    if store.reading_count() and store.stale_rooms():
        log.info("Campus model changed; clearing old readings")
        store.clear_readings()
    if store.reading_count() == 0:
        log.info("Seeding %d days of simulated history", settings.history_days)
        store.insert_readings(fleet.history())
    else:
        rows = fleet.history()  # also warms the room state so live rows continue smoothly
        # Fill hours when the service was not running, so replay and the 7-day figures never hit a hole.
        have = store.query("SELECT DISTINCT TIMESTAMP_TRUNC(ts, HOUR) AS h FROM readings")["h"]
        have = {pd.Timestamp(h).tz_localize("UTC") if pd.Timestamp(h).tzinfo is None
                else pd.Timestamp(h).tz_convert("UTC") for h in have}
        this_hour = pd.Timestamp.now(tz="UTC").floor("h")
        gap = [r for r in rows
               if (h := pd.Timestamp(r["ts"]).tz_convert("UTC").floor("h")) not in have and h < this_hour]
        if gap:
            log.info("Back-filling %d simulated readings for hours the service was down", len(gap))
            store.insert_readings(gap)
    from .a2a import route, run_skill
    from .agent.team import handle_trigger
    ops.run_trigger = lambda trigger: handle_trigger(trigger, run_skill, route)
    stop = threading.Event()
    if settings.live_simulator and settings.sim_mode == "request":
        sim["fleet"] = fleet
    elif settings.live_simulator:
        threading.Thread(target=_live_loop, args=(fleet, stop), daemon=True).start()
    yield
    stop.set()
    sim["fleet"] = None


app = FastAPI(title="CampusPulse", version=__version__, lifespan=lifespan)


# --- ingest -------------------------------------------------------------------

class Reading(BaseModel):
    device_id: str = Field(max_length=64)
    room_id: str
    ts: datetime | None = None  # server time is used when the node has no clock
    occupied: bool
    people: int | None = Field(default=None, ge=0, le=500)
    co2_ppm: float | None = Field(default=None, ge=300, le=10000)  # None for camera nodes
    temp_c: float | None = Field(default=None, ge=0, le=60)
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    watts: float | None = Field(default=None, ge=0)
    ac_on: bool | None = None
    rssi_dbm: float | None = Field(default=None, ge=-120, le=0)
    packets_sent: int = Field(default=1, ge=0)
    packets_acked: int = Field(default=1, ge=0)
    gateway_id: str | None = None

    def to_row(self) -> dict:
        room = ROOM_BY_ID.get(self.room_id)
        if room is None:
            raise HTTPException(422, f"unknown room_id {self.room_id}")
        if is_camera(self.device_id):
            # A camera counts people and nothing else; energy and air come from the room's sensor node.
            if self.people is None:
                raise HTTPException(422, "camera readings need a people count")
            return self.model_dump() | {"ts": self.ts or datetime.now(timezone.utc),
                                        "gateway_id": self.gateway_id or room.gateway_id,
                                        "occupied": self.people > 0, "watts": None, "ac_on": None,
                                        "co2_ppm": None, "temp_c": None, "humidity_pct": None,
                                        "simulated": False}
        # A node reports what it can measure: a bare ESP32 has no air sensor, a DHT22 node no CO2.
        # Missing values are stored empty and shown as "–". Hardware must still report its Wi-Fi signal
        # (it feeds the trust score); a replayed dataset has no radio, so it may leave it out.
        if self.rssi_dbm is None and not is_replay(self.device_id):
            raise HTTPException(422, "sensor readings need rssi_dbm (the node's Wi-Fi signal)")
        watts = self.watts or 0.0
        return self.model_dump() | {
            "ts": self.ts or datetime.now(timezone.utc),
            "gateway_id": self.gateway_id or room.gateway_id,
            "ac_on": self.ac_on if self.ac_on is not None else watts > room.ac_kw * 1000 * 0.3,
            "watts": watts,
            "simulated": is_virtual(self.device_id),  # hand-set values from a virtual node are not measurements
        }


_publisher = {}


def _publish(rows: list[dict]) -> int:
    """Send readings through Pub/Sub; the push subscription delivers them to /api/pubsub/push."""
    from google.cloud import pubsub_v1

    pub = _publisher.get("client") or _publisher.setdefault("client", pubsub_v1.PublisherClient())
    futures = [pub.publish(settings.pubsub_topic, json.dumps(r, default=str).encode()) for r in rows]
    for f in futures:
        f.result(timeout=10)
    return len(futures)


def _accept(rows: list[dict]) -> int:
    n = _publish(rows) if settings.pubsub_topic else get_store().insert_readings(rows)
    cache.drop("overview")
    ops.watch(rows)  # every node reading goes past the watcher, which may hand an event to the orchestrator
    rows = [r for r in rows if not is_test_device(r["device_id"])]  # test traffic never counts as live hardware
    if not rows:
        return n
    now = datetime.now(timezone.utc)
    if any(is_replay(r["device_id"]) for r in rows):
        state["last_replay"] = now
        state["replay_device"] = next(r["device_id"] for r in rows if is_replay(r["device_id"]))
    virtual = [r for r in rows if is_virtual(r["device_id"])]
    if virtual:
        state["last_virtual"] = now
        state["virtual_device"] = virtual[-1]["device_id"]
    hardware = [r for r in rows if not is_replay(r["device_id"]) and not is_virtual(r["device_id"])]
    if hardware:
        state["last_real"] = now
        sensors = [r for r in hardware if not is_camera(r["device_id"])]
        if sensors:
            state["last_hw"] = sensors[-1]
    state["real_count"] += len(hardware)
    for r in rows:  # any node reporting (real, replayed or virtual) replaces the simulator in its room
        kind = "camera" if is_camera(r["device_id"]) else "sensor"
        state["real_rooms"][(r["room_id"], kind)] = now
    return n


@app.post("/api/ingest")
async def ingest(request: Request, x_device_key: str = Header(default="")):
    # Authenticate before parsing, so unknown senders learn nothing about the schema.
    if not hmac.compare_digest(x_device_key, settings.ingest_key):
        raise HTTPException(401, "missing or wrong X-Device-Key header")
    payload = await request.json()
    try:
        items = [Reading.model_validate(p) for p in (payload if isinstance(payload, list) else [payload])]
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json(include_url=False))) from e
    n = _accept([r.to_row() for r in items])
    return {"accepted": n, "via": "pubsub" if settings.pubsub_topic else "direct"}


def _normalise(r: dict) -> dict:
    """Fill fields a Pub/Sub message may lack, so every stored reading is complete."""
    room = ROOM_BY_ID[r["room_id"]]
    return r | {"gateway_id": r.get("gateway_id") or room.gateway_id,
                "packets_sent": r.get("packets_sent") if r.get("packets_sent") is not None else 1,
                "packets_acked": r.get("packets_acked") if r.get("packets_acked") is not None else 1,
                "simulated": bool(r.get("simulated", False))}


@app.post("/api/pubsub/push")
async def pubsub_push(request: Request, key: str = ""):
    """Pub/Sub push subscription target: decode and store.

    The subscription's push endpoint carries the ingest key (?key=...), so only our own
    subscription can write here."""
    if not hmac.compare_digest(key, settings.ingest_key):
        raise HTTPException(401, "missing or wrong key")
    envelope = await request.json()
    try:
        data = json.loads(base64.b64decode(envelope["message"]["data"]))
    except (KeyError, ValueError, TypeError) as e:
        # Acknowledge malformed messages so Pub/Sub does not redeliver them forever.
        log.warning("dropping malformed Pub/Sub message: %s", e)
        return {"stored": 0}
    rows = [_normalise(r) for r in (data if isinstance(data, list) else [data]) if r.get("room_id") in ROOM_BY_ID]
    get_store().insert_readings(rows)
    cache.drop("overview")
    return {"stored": len(rows)}


# --- read API -------------------------------------------------------------------

def _esp32_status(last: datetime | None, online: bool) -> dict:
    hw = state.get("last_hw") or {}
    reading = {k: hw.get(k) for k in ("temp_c", "humidity_pct", "co2_ppm", "occupied", "ac_on", "rssi_dbm")}
    return {"online": online, "last_seen": last.isoformat() if last else None,
            "seconds_ago": round((datetime.now(timezone.utc) - last).total_seconds()) if last else None,
            "device": hw.get("device_id"), "room": hw.get("room_id") or LIVE_ROOM,
            "reading": reading if hw else None, "ingest_url": "/api/ingest", "command_url": f"/api/rooms/{LIVE_ROOM}/command"}


def _mode() -> dict:
    last = state["last_real"]
    live_seen = bool(last and (datetime.now(timezone.utc) - last).total_seconds() < 120)
    lr = state.get("last_replay")
    replay_status = replayer.status()
    replay_on = replay_status["running"] or bool(lr and (datetime.now(timezone.utc) - lr).total_seconds() < 120)
    lv = state.get("last_virtual")
    virtual_on = bool(lv and (datetime.now(timezone.utc) - lv).total_seconds() < 120)
    # The presenter's switch wins: a running replay is the source even if the board reported a minute ago.
    source = ("replay" if replay_status["running"] else "esp32" if live_seen else "replay" if replay_on
              else "virtual" if virtual_on else "simulation")
    return {"backend": settings.backend, "gemini": settings.gemini_enabled, "model": settings.gemini_model,
            "source": source, "esp32": _esp32_status(last, live_seen), "replay": replay_status,
            "virtual_online": virtual_on, "virtual_device": state.get("virtual_device") if virtual_on else None,
            "replay_online": replay_on,
            "replay_device": (state.get("replay_device") or replay_status.get("device")) if replay_on else None,
            "live_room": LIVE_ROOM, "live_node_online": live_seen,
            "live_node_last_seen": last.isoformat() if last else None,
            "real_readings": state["real_count"], "tariff_rm_per_kwh": settings.tariff_rm_per_kwh,
            "grid_kg_co2e_per_kwh": settings.grid_kg_co2e_per_kwh,
            "co2_limit_ppm": settings.co2_limit_ppm}


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, **_mode()}


def _sync_control():
    """After a restart, rebuild each room's AC mode from the decision log."""
    if not control.synced:
        control.sync(_actions(), _decision_status())


@app.get("/api/overview")
def overview(at: str | None = None):
    """KPIs and room states. `at` (local time, e.g. 2026-11-04T10:00) replays the floor plan as it was then.

    Cached briefly and shared by every viewer (dropped whenever new readings or decisions arrive),
    so an open dashboard costs a few warehouse queries a minute, not a few per viewer."""
    replay = None
    if at:
        try:
            replay = pd.Timestamp(at)
        except ValueError:
            raise HTTPException(422, "at must be a local date-time like 2026-11-04T10:00")
        if replay.tzinfo is not None:
            replay = replay.tz_convert(settings.timezone).tz_localize(None)
        replay = replay.floor("min")
    key = f"overview:{replay.isoformat() if replay is not None else 'live'}"
    # The cached part is the warehouse work; which real source is on show is always answered fresh.
    return cache.get(key, 600 if replay is not None else 20, lambda: _overview(replay)) | {"mode": _mode()}


def _overview(replay: pd.Timestamp | None) -> dict:
    store = get_store()
    rooms = [r | {"ac_mode": control.mode(r["room_id"])} for r in analytics.room_states(store, at=replay)]
    moment = replay.to_pydatetime() if replay is not None else datetime.now(settings.timezone).replace(tzinfo=None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": cache.get("kpis", 60, lambda: analytics.kpis(store)),
        "rooms": rooms,
        "study": study.free_rooms(rooms, moment),
        "weather": cache.get("weather", 600, weather.now),
        "replay_at": replay.isoformat() if replay is not None else None,
        "replay_range": cache.get("replay_range", 300, lambda: analytics.replay_range(store)),
        "mode": _mode(),
    }


def _actions(force=False):
    return cache.get("actions", 60, lambda: decisions.build_actions(get_store()), force)


@app.get("/api/actions")
def actions():
    return _actions()


@app.get("/api/brief")
def brief(refresh: bool = False):
    store = get_store()

    def make():
        acts = _actions(force=refresh)
        return {"brief": daily_brief(acts, analytics.kpis(store)), "actions": acts,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    return cache.get("brief", 1800, make, force=refresh)


class Decision(BaseModel):
    status: Literal["approved", "dismissed"]
    decided_by: str = Field(default="Estates manager", max_length=80)


@app.post("/api/actions/{action_id}/decide")
def decide(action_id: str, body: Decision):
    act = next((a for a in _actions() if a["id"] == action_id), None)
    if act is None:
        raise HTTPException(404, "action not found; refresh the brief")
    _sync_control()  # before logging, so this decision is what changes the rooms' AC modes
    get_store().insert_decision({
        "decided_at": datetime.now(timezone.utc), "action_id": action_id, "kind": act["kind"],
        "title": act["title"], "status": body.status, "kwh_per_week": act["kwh_per_week"],
        "rm_per_week": act["rm_per_week"], "decided_by": body.decided_by,
    })
    commands = control.apply(act, body.status, body.decided_by)
    cache.drop("overview")  # rooms show their new AC mode straight away
    return {"ok": True, "action_id": action_id, "status": body.status, "commands": commands}


# Gemini helps Autopilot read goals and write words, but the plan never waits long for it.
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout  # noqa: E402

_gemini_pool = ThreadPoolExecutor(max_workers=4)
GEMINI_WAIT_S = 10


def _within(fn, *args, fallback):
    fut = _gemini_pool.submit(fn, *args)
    try:
        return fut.result(timeout=GEMINI_WAIT_S)
    except _Timeout:
        log.warning("Gemini took over %ss; using the fallback", GEMINI_WAIT_S)
        return fallback()


def _goals(text: str):
    from .agent.gemini import plan_constraints as read
    return _within(read, text, fallback=lambda: (autopilot.parse_instructions_offline(text).as_dict(),
                                                 "rule-based reader (Gemini error: 504 slow to answer)"))


def _explain(p: dict) -> dict:
    from .agent.gemini import _fallback_explain
    return _within(explain_plan, p, fallback=lambda: _fallback_explain(p) | {"source": "template (Gemini error: 504 slow to answer)"})


def _notices(moves: list[dict]) -> dict:
    from .agent.gemini import _fallback_notice
    return _within(draft_notices, moves, fallback=lambda: {
        "notices": [{"id": m["id"], "text": _fallback_notice(m)} for m in moves],
        "source": "template (Gemini error: 504 slow to answer)"})


class PlanRequest(BaseModel):
    instructions: str = Field(default="", max_length=500)
    day: str | None = None  # YYYY-MM-DD; default: the next teaching day


@app.post("/api/autopilot/plan")
def autopilot_plan(body: PlanRequest):
    """Plan the next teaching day: forecast, optimise rooms, schedule AC, check comfort, compare in the twin."""
    constraints, reader = _goals(body.instructions)
    day = None
    if body.day:
        try:
            day = datetime.fromisoformat(body.day).date()
        except ValueError:
            raise HTTPException(422, "day must be YYYY-MM-DD")
    p = autopilot.plan(get_store(), day=day, constraints=constraints)
    if "error" in p:
        raise HTTPException(422, p["error"])
    p["instructions"] = body.instructions
    p["constraints_read_by"] = reader
    p["explanation"] = _explain(p)
    state["last_plan"] = p["id"]
    return p


@app.get("/api/autopilot/latest")
def autopilot_latest():
    p = autopilot.get_plan(state.get("last_plan") or "")
    return p or {}


class ApplyRequest(BaseModel):
    plan_id: str
    approve_review: bool = False   # also apply the changes that were sent for a person's approval
    decided_by: str = Field(default="Estates manager", max_length=80)


@app.post("/api/autopilot/apply")
def autopilot_apply(body: ApplyRequest):
    """Carry out a plan: AC modes go to the rooms, moves are logged and notices drafted."""
    p = autopilot.get_plan(body.plan_id)
    if p is None:
        raise HTTPException(404, "plan not found; plan again")
    ok = lambda d: d == "auto" or body.approve_review  # noqa: E731
    rooms = [r["room_id"] for r in p["ac_schedule"] if ok(r["decision"])]
    moves = [m for m in p["moves"] if ok(m["decision"])]
    _sync_control()
    act = {"id": f"autopilot-{p['day']}", "kind": "occupancy_linked_ac", "rooms": rooms,
           "title": f"Autopilot plan for {p['day_label']}"}
    commands = control.apply(act, "approved", body.decided_by)
    kwh = p["saving"]["kwh"]
    get_store().insert_decision({
        "decided_at": datetime.now(timezone.utc), "action_id": p["id"], "kind": "autopilot",
        "title": act["title"] + f" ({len(rooms)} rooms, {len(moves)} moves)", "status": "approved",
        "kwh_per_week": kwh, "rm_per_week": p["saving"]["rm"], "decided_by": body.decided_by,
    })
    held = [m for m in p["moves"] if not ok(m["decision"])]
    return {"ok": True, "commands": commands, "rooms": rooms, "moves_applied": moves, "moves_held": len(held),
            "notices": _notices(moves)}


@app.get("/api/commands")
def command_log():
    """Device commands sent for approved changes, newest first, and each room's current AC mode."""
    _sync_control()
    return {"commands": control.log[:50], "modes": dict(control.policies),
            "transport": "mqtt" if settings.mqtt_host else "https-pull"}


@app.get("/api/rooms/{room_id}/command")
def room_command(room_id: str, x_device_key: str | None = Header(default=None)):
    """HTTPS pull for nodes not on MQTT: the AC mode this room should run."""
    if not x_device_key or not hmac.compare_digest(x_device_key, settings.ingest_key):
        raise HTTPException(401, "missing or wrong X-Device-Key")
    if room_id not in ROOM_BY_ID:
        raise HTTPException(404, "unknown room")
    _sync_control()
    return control.state_for(room_id)


@app.get("/api/decisions")
def decision_log():
    df = get_store().query("SELECT * FROM decisions ORDER BY decided_at DESC LIMIT 50")
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _decision_status() -> dict[str, str]:
    """Latest decision per action id."""
    # BigQuery accepts QUALIFY only alongside WHERE, GROUP BY or HAVING, hence WHERE TRUE.
    df = get_store().query("""SELECT action_id, status FROM decisions WHERE TRUE
        QUALIFY ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY decided_at DESC) = 1""")
    return dict(zip(df["action_id"], df["status"]))


@app.get("/api/actions/{action_id}/caption")
def caption(action_id: str):
    act = next((a for a in _actions() if a["id"] == action_id), None)
    if act is None:
        raise HTTPException(404, "action not found; refresh the brief")
    return cache.get(f"caption:{action_id}", 3600, lambda: chart_caption(act))


@app.get("/api/report.pdf")
def estates_report():
    b = brief()
    pdf = report.build_pdf(b["brief"], b["actions"], analytics.kpis(get_store()), _decision_status(), _mode())
    name = f"campuspulse-estates-report-{datetime.now(settings.timezone):%Y-%m-%d}.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/rooms/{room_id}")
def room(room_id: str):
    if room_id not in ROOM_BY_ID:
        raise HTTPException(404, "unknown room")
    return cache.get(f"room:{room_id}", 120, lambda: analytics.room_detail(get_store(), room_id))


@app.get("/api/network")
def network_health():
    store = get_store()

    def make():
        health = [h.as_dict() for h in network.link_health(store, hours=24)]
        out = network.gateway_outages(store)
        outages = [{"gateway_id": r.gateway_id, "hour": str(r.hour)} for r in out.itertuples()]
        return {"nodes": health, "outages": outages}

    return cache.get("network", 60, make)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.post("/api/ask")
def ask_data(body: Question):
    return ask(get_store(), body.question)


@app.get("/api/suggestions")
def ask_suggestions():
    return suggestions()


# --- web app --------------------------------------------------------------------

@app.middleware("http")
async def catch_up_simulation(request: Request, call_next):
    """Request mode (Cloud Run free tier): bring simulated readings up to now before answering."""
    path = request.url.path
    if path.startswith(("/api/", "/a2a")) and not path.startswith(("/api/ingest", "/api/pubsub")):
        if settings.sim_mode == "request":
            await run_in_threadpool(_catch_up)
    return await call_next(request)


class SourceRequest(BaseModel):
    mode: Literal["simulation", "esp32", "replay"]
    preset: str = "arrival"
    every_s: float = Field(default=3.0, ge=1.0, le=60.0)


@app.get("/api/source")
def data_source():
    """Which real data is on show: our ESP32 node in B-08-05, or the published dataset in its own room."""
    return _mode()


@app.post("/api/source")
def set_data_source(body: SourceRequest):
    """Switch the real-data view from the dashboard. "replay" plays the published dataset as its own site
    (an office in Mons, Belgium), never inside an APU room; "esp32" and "simulation" stop it. The ESP32
    shows as live by itself as soon as it reports."""
    if body.mode == "replay":
        replayer.start(body.preset, body.every_s)
    else:
        replayer.stop()
    return _mode()


@app.middleware("http")
async def revalidate_web_app(request: Request, call_next):
    """Browsers must revalidate the dashboard files, so a redeploy is never hidden behind a stale cache."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/campus")
def campus_all_rooms(at: str | None = None):
    """Every physical APU room and its classes from the real timetable, at `at` (local, e.g. 2026-11-11T14:00)."""
    from .campus_all import campus_day
    if at:
        try:
            when = pd.Timestamp(at).to_pydatetime().replace(tzinfo=None)
        except ValueError:
            raise HTTPException(422, "at must be a local date-time like 2026-11-11T14:00")
    else:
        when = datetime.now(settings.timezone).replace(tzinfo=None)
    return campus_day(when)


# --- the agent team: coordinator, roster, runs, and the scheduled run ----------------------------

class TeamRequest(BaseModel):
    message: str = Field(min_length=3, max_length=500)


@app.post("/api/agent")
def agent_team(body: TeamRequest):
    """One request to the whole team: the coordinator decides which specialists to call, in what order."""
    from .a2a import route, run_skill
    from .agent.team import coordinate
    run = coordinate(body.message, run_skill, route, via="CampusPulse app")
    return {k: run[k] for k in ("message", "reply", "steps", "source", "skills", "ms", "at")} | {
        "sql": run["data"].get("sql"), "plan_id": run["data"].get("plan_id")}


@app.get("/api/agents")
def agent_roster():
    """Who is on the team, the prompt each one runs on, and what the team did recently."""
    from .agent.team import RUNS, roster
    return {"agents": roster(), "runs": list(RUNS), "scheduled": state.get("nightly"),
            "patterns": ["Orchestrator", "Event-driven loop", "Sequential pipeline", "Iterative refinement",
                         "Generator and critic", "Human in the loop", "Closed-loop verification"]}


@app.post("/api/autopilot/nightly")
def autopilot_nightly(key: str = "", apply: str = "safe", x_device_key: str = Header(default="")):
    """The run nobody presses a button for (Cloud Scheduler calls this each evening): plan the next
    teaching day, apply the changes that are safe, hold the rest for a person, draft the notices."""
    if not hmac.compare_digest(key or x_device_key, settings.ingest_key):
        raise HTTPException(401, "bad key")
    p = autopilot_plan(PlanRequest(instructions=""))
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "day": p["day_label"],
           "saving_kwh": round(p["saving"]["kwh"]), "saving_pct": round(p["saving"]["pct"] * 100),
           "auto_moves": p["auto_moves"], "held_for_person": p["review_moves"], "applied": False,
           "headline": p["explanation"].get("headline", "")}
    if apply == "safe":
        done = autopilot_apply(ApplyRequest(plan_id=p["id"], approve_review=False, decided_by="Autopilot (scheduled)"))
        out |= {"applied": True, "rooms": len(done["rooms"]), "notices": len(done["notices"]["notices"])}
    state["nightly"] = out
    from .agent.team import RUNS
    RUNS.appendleft({"at": out["at"], "message": "Scheduled run: plan the next teaching day", "via": "Cloud Scheduler",
                     "reply": out["headline"], "source": "no person involved", "ms": None, "steps": [
                         {"agent": "Scheduler", "did": "started Autopilot on its own", "detail": p["day_label"], "ok": True},
                         {"agent": "Autopilot", "did": "planned, checked and explained the day",
                          "detail": f"{out['saving_kwh']} kWh ({out['saving_pct']}%)", "ok": True},
                         {"agent": "Autopilot", "did": "applied the safe changes" if out["applied"] else "proposed only",
                          "detail": f"{out['auto_moves']} automatic, {out['held_for_person']} held for a person", "ok": True}]})
    return out


# Gemini Enterprise (A2A agent) and the Google Sheets / Calendar exports.
from . import a2a, google_tools  # noqa: E402

app.include_router(a2a.router)
app.include_router(google_tools.router)


# --- the agent loop: activity feed, autonomy switch, approvals ----------------------------------

@app.get("/api/ops")
def ops_feed():
    """What the agents are doing: incidents from the watcher, each with every step the orchestrator,
    the policy guard and the verifier took."""
    ops.sweep()
    feed = ops.feed()
    return {"autonomy": ops.autonomy, "incidents": feed,
            "open": sum(i["status"] not in ("resolved", "escalated", "no_action") for i in feed),
            "awaiting_approval": sum(i["status"] == "awaiting_approval" for i in feed),
            "live_room": LIVE_ROOM, "ac_mode_live_room": control.mode(LIVE_ROOM)}


class AutonomyRequest(BaseModel):
    on: bool


@app.post("/api/ops/autonomy")
def ops_autonomy(body: AutonomyRequest):
    ops.autonomy = body.on
    return {"autonomy": ops.autonomy}


@app.post("/api/ops/incidents/{incident_id}/{decision}")
def ops_decide(incident_id: str, decision: Literal["approve", "dismiss"]):
    """A person answers a change the policy guard held back."""
    inc = ops.get(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    p = inc.get("proposal") or {}
    if inc["status"] != "awaiting_approval" or not p:
        raise HTTPException(409, "nothing is waiting for approval on this incident")
    if decision == "dismiss":
        ops.step(inc, "Estates manager", "dismissed the held change")
        ops.close(inc, "no_action", inc.get("note") or "Held change dismissed by a person.", "Estates manager")
        return ops.get(incident_id)
    cmd = ops._apply(p["room_id"], p["mode"], inc["id"], p.get("reason", ""), "Estates manager")
    ops.step(inc, "Estates manager", f"approved: AC in {p['room_id']} to '{p['mode']}'")
    ops.step(inc, "Device command", f"AC mode sent to {p['room_id']}", cmd.get("delivered") or "")
    with ops.lock:
        inc["proposal"] = p | {"applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ops.set_status(inc, "acted")
    ops.schedule_check(inc, p["room_id"], "ac_off" if inc["event"] == "empty_cooled" else "co2_below_limit")
    return ops.get(incident_id)


class ResetRequest(BaseModel):
    room: str = LIVE_ROOM


@app.post("/api/ops/reset")
def ops_reset(body: ResetRequest):
    """Demo helper: put a room back on the building schedule, so the agents can catch it again."""
    if body.room not in ROOM_BY_ID:
        raise HTTPException(422, "unknown room")
    ops._apply(body.room, "schedule", "reset", "reset from the dashboard", "Estates manager (reset)")
    with ops.lock:
        for k in [k for k in ops.closed_at if k[0] == body.room]:
            ops.closed_at.pop(k)
        for k in [k for k in ops.streak if k[0] == body.room]:
            ops.streak.pop(k)
    return {"room": body.room, "ac_mode": control.mode(body.room)}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")

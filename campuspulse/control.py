"""Closing the loop: approved changes become commands for the rooms' devices.

When Estates approves an air-conditioning change, each room it covers gets a new
AC mode. The command goes down the same network the readings come up:

  * MQTT (QoS 1, retained) on `campuspulse/<room_id>/command`, when a broker is
    configured (CP_MQTT_HOST). Retained, so a node that reconnects gets it at once.
  * HTTPS pull at GET /api/rooms/<room_id>/command for nodes on the HTTPS transport.

Simulated rooms obey the same modes, so after an approval the floor plan shows
the empty rooms going dark and the next readings carry the saving.

Modes, strongest last:
  schedule         the building's fixed timetable (07:30-21:00 weekdays): today's default
  after_hours_off  the fixed timetable, but never at night or at weekends
  occupancy        cool only while people are in, while a class is booked, or 1 hour before it
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("campuspulse.control")

MODES = ("schedule", "after_hours_off", "occupancy")
MODE_TEXT = {"schedule": "building schedule", "after_hours_off": "off after hours",
             "occupancy": "follows occupancy"}
MODE_FOR_KIND = {"after_hours_ac": "after_hours_off", "occupancy_linked_ac": "occupancy"}
PRECOOL_MIN = 60


def topic(room_id: str) -> str:
    return f"campuspulse/{room_id}/command"


class ControlPlane:
    def __init__(self):
        self.lock = threading.Lock()
        self.policies: dict[str, str] = {}      # room_id -> mode; missing means "schedule"
        self.by_action: dict[str, tuple[str, list[str]]] = {}   # approved action id -> (mode, rooms)
        self.log: list[dict] = []
        self.synced = False

    # -- state -------------------------------------------------------------
    def mode(self, room_id: str) -> str:
        return self.policies.get(room_id, "schedule")

    def _recompute(self) -> dict[str, str]:
        """Each room takes the strongest mode of the approved actions that cover it."""
        rank = {m: i for i, m in enumerate(MODES)}
        new: dict[str, str] = {}
        for mode, rooms in self.by_action.values():
            for rid in rooms:
                if rank[mode] > rank.get(new.get(rid, "schedule"), 0):
                    new[rid] = mode
        return new

    def apply(self, action: dict, status: str, decided_by: str = "Estates manager") -> list[dict]:
        """Record an approval or dismissal; send a command to every room whose mode changed."""
        mode = MODE_FOR_KIND.get(action["kind"])
        if mode is None:
            return []
        with self.lock:
            before = dict(self.policies)
            if status == "approved":
                self.by_action[action["id"]] = (mode, list(action["rooms"]))
            else:
                self.by_action.pop(action["id"], None)
            self.policies.clear()
            self.policies.update(self._recompute())
            changed = sorted(r for r in set(before) | set(self.policies)
                             if before.get(r, "schedule") != self.policies.get(r, "schedule"))
            cmds = [self._command(r, self.policies.get(r, "schedule"), action, decided_by) for r in changed]
            self.log = (cmds[::-1] + self.log)[:100]
        for c in cmds:
            self._send(c)
        return cmds

    def sync(self, actions: list[dict], status: dict[str, str]) -> None:
        """Rebuild modes from the decision log after a restart (no commands are re-sent)."""
        with self.lock:
            if self.synced:
                return
            for a in actions:
                mode = MODE_FOR_KIND.get(a["kind"])
                if mode and status.get(a["id"]) == "approved":
                    self.by_action[a["id"]] = (mode, list(a["rooms"]))
            self.policies.clear()
            self.policies.update(self._recompute())
            self.synced = True

    def state_for(self, room_id: str) -> dict:
        mode = self.mode(room_id)
        last = next((c for c in self.log if c["room_id"] == room_id), None)
        return {"room_id": room_id, "ac_mode": mode, "ac_mode_text": MODE_TEXT[mode], "precool_min": PRECOOL_MIN,
                "issued_at": last["issued_at"] if last else None, "action_id": last["action_id"] if last else None}

    # -- downlink ------------------------------------------------------------
    def _command(self, room_id: str, mode: str, action: dict, decided_by: str) -> dict:
        return {"issued_at": datetime.now(timezone.utc).isoformat(), "room_id": room_id, "ac_mode": mode,
                "ac_mode_text": MODE_TEXT[mode], "precool_min": PRECOOL_MIN, "action_id": action["id"],
                "action_title": action["title"], "decided_by": decided_by, "topic": topic(room_id),
                "transport": "mqtt" if settings.mqtt_host else "https-pull", "delivered": None}

    def _send(self, cmd: dict) -> None:
        from .campus import LIVE_ROOM
        if not settings.mqtt_host:
            cmd["delivered"] = ("queued; the sensor node picks it up on its next HTTPS poll" if cmd["room_id"] == LIVE_ROOM
                                else "simulated room: obeys it from its next reading")
            return
        threading.Thread(target=self._publish, args=(cmd,), daemon=True).start()

    def _publish(self, cmd: dict) -> None:
        payload = json.dumps({k: cmd[k] for k in ("room_id", "ac_mode", "precool_min", "action_id", "issued_at")})
        try:
            import paho.mqtt.publish as publish
            auth = {"username": settings.mqtt_user, "password": settings.mqtt_pass} if settings.mqtt_user else None
            publish.single(cmd["topic"], payload, qos=1, retain=True, hostname=settings.mqtt_host,
                           port=settings.mqtt_port, auth=auth, client_id="campuspulse-control",
                           tls={} if settings.mqtt_tls else None, keepalive=10)
            cmd["delivered"] = "broker acknowledged (QoS 1)"
        except Exception as e:  # the decision stands; the node picks it up by HTTPS pull instead
            log.warning("MQTT command to %s failed: %s", cmd["topic"], e)
            cmd["delivered"] = f"broker unreachable; node can pull it over HTTPS ({type(e).__name__})"


control = ControlPlane()

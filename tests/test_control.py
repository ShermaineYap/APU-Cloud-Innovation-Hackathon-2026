from datetime import datetime

from fastapi.testclient import TestClient

from campuspulse import api
from campuspulse.campus import ROOM_BY_ID, TIMETABLE
from campuspulse.config import settings
from campuspulse.simulator import Fleet

KEY = {"X-Device-Key": "test-key"}


def test_approve_sends_ac_commands_and_dismiss_reverts():
    with TestClient(api.app) as c:
        acts = c.get("/api/actions").json()
        ac = next(a for a in acts if a["kind"] == "occupancy_linked_ac")
        assert ac["kg_co2e_per_week"] > 0
        # Other tests may have approved it already; start from "dismissed".
        c.post(f"/api/actions/{ac['id']}/decide", json={"status": "dismissed"})
        res =c.post(f"/api/actions/{ac['id']}/decide", json={"status": "approved"}).json()
        assert {cmd["room_id"] for cmd in res["commands"]} == set(ac["rooms"])
        assert all(cmd["ac_mode"] == "occupancy" and cmd["topic"].endswith("/command") for cmd in res["commands"])
        room = ac["rooms"][0]
        assert c.get(f"/api/rooms/{room}/command").status_code == 401
        assert c.get(f"/api/rooms/{room}/command", headers=KEY).json()["ac_mode"] == "occupancy"
        modes = {r["room_id"]: r["ac_mode"] for r in c.get("/api/overview").json()["rooms"]}
        assert modes[room] == "occupancy"
        assert c.get("/api/commands").json()["commands"][0]["room_id"] in ac["rooms"]
        res = c.post(f"/api/actions/{ac['id']}/decide", json={"status": "dismissed"}).json()
        assert all(cmd["ac_mode"] == "schedule" for cmd in res["commands"])
        assert c.get(f"/api/rooms/{room}/command", headers=KEY).json()["ac_mode"] == "schedule"


def test_simulated_rooms_obey_ac_modes():
    fleet = Fleet()
    slot = TIMETABLE[0]
    room = ROOM_BY_ID[slot.room_id]
    # A weekday at 20:00 with nobody in and no class booked: schedule cools it, occupancy does not.
    day = datetime(2026, 11, 2 + slot.weekday, 20, 0, tzinfo=settings.timezone)
    booked_late = any(s.start_min / 60 - 1 <= 20 < s.end_min / 60
                      for s in TIMETABLE if s.room_id == room.room_id and s.weekday == slot.weekday)
    assert fleet._ac_on(room, day, people=0)
    fleet.policies[room.room_id] = "occupancy"
    assert fleet._ac_on(room, day, people=0) == booked_late
    assert fleet._ac_on(room, day, people=5)
    # One hour before a booked class counts as pre-cool.
    pre = datetime(2026, 11, 2 + slot.weekday, slot.start_min // 60, 0, tzinfo=settings.timezone)
    assert fleet._ac_on(room, pre.replace(hour=max(0, pre.hour - 1), minute=30), people=0) or slot.start_min % 60 > 30
    # after_hours_off: never at weekends.
    fleet.policies[room.room_id] = "after_hours_off"
    assert not fleet._ac_on(room, datetime(2026, 11, 7, 11, 0, tzinfo=settings.timezone), people=0)

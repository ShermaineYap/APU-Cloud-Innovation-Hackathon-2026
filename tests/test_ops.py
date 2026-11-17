"""The agent loop: watcher -> orchestrator -> policy guard -> device -> verifier -> closed."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from campuspulse import api
from campuspulse.control import control
from campuspulse.ops import ops

@pytest.fixture(autouse=True)
def clean_loop():
    """Earlier tests approve campus-wide AC changes; start each loop test from the building schedule."""
    with control.lock:
        control.by_action.clear()
        control.policies.clear()
    with ops.lock:
        ops.incidents.clear()
        ops.streak.clear()
        ops.closed_at.clear()
    while not ops.queue.empty():
        ops.queue.get_nowait()
    ops.autonomy, ops.manual = True, True
    yield


H = {"X-Device-Key": "test-key"}
ROOM = "B-08-05"


def board(**kw):
    return {"device_id": "esp32-b0805", "room_id": ROOM, "occupied": False, "ac_on": True, "rssi_dbm": -55,
            "temp_c": 26.0, "humidity_pct": 60} | kw


def _age_streaks(seconds=60):
    with ops.lock:
        for k in list(ops.streak):
            ops.streak[k] -= timedelta(seconds=seconds)


def _due_now(inc):
    with ops.lock:
        inc["check"]["due"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


def test_live_board_closes_the_loop_without_a_person():
    ops.manual = True
    with TestClient(api.app) as c:
        c.post("/api/ops/reset", json={"room": ROOM})
        assert c.post("/api/ingest", json=board(), headers=H).status_code == 200
        assert not ops.open_for(ROOM)            # one reading is not enough: the condition must hold
        _age_streaks()
        c.post("/api/ingest", json=board(), headers=H)
        inc = ops.open_for(ROOM, "empty_cooled")
        assert inc and inc["steps"][0]["agent"] == "Watcher (rules)"

        ops.drain()                              # the orchestrator (rule playbook: Gemini is off in tests)
        agents = [s["agent"] for s in inc["steps"]]
        assert "Policy guard (rules)" in agents and "Device command" in agents
        assert inc["status"] == "verifying" and control.mode(ROOM) == "occupancy"
        assert c.get(f"/api/rooms/{ROOM}/command", headers=H).json()["ac_mode"] == "occupancy"  # what the board pulls

        _due_now(inc)
        c.post("/api/ingest", json=board(ac_on=False), headers=H)   # the board's next reading: LED off
        assert inc["check"]["result"] == "passed"
        ops.drain()
        assert inc["status"] == "resolved" and inc["note"]
        feed = c.get("/api/ops").json()
        assert feed["incidents"][0]["id"] == inc["id"] and feed["ac_mode_live_room"] == "occupancy"

        # a room already following occupancy is not flagged again
        _age_streaks()
        c.post("/api/ingest", json=board(), headers=H)
        assert not ops.open_for(ROOM, "empty_cooled")
        c.post("/api/ops/reset", json={"room": ROOM})
        assert control.mode(ROOM) == "schedule"


def test_guard_holds_a_risky_change_until_a_person_approves():
    ops.manual = True
    with TestClient(api.app) as c:
        c.post("/api/ops/reset", json={"room": ROOM})
        c.post("/api/ingest", json=board(occupied=True), headers=H)
        inc = ops.open(ROOM, "empty_cooled", board(), "esp32-b0805")
        ops.queue.get_nowait()  # this test drives the tools by hand
        out = ops.set_ac_mode(inc, ROOM, "occupancy", "test", "test")
        assert out["held"] and "occupied" in out["why"] and inc["status"] == "awaiting_approval"
        assert control.mode(ROOM) == "schedule"
        r = c.post(f"/api/ops/incidents/{inc['id']}/approve").json()
        assert r["status"] == "verifying" and control.mode(ROOM) == "occupancy"
        assert c.post(f"/api/ops/incidents/{inc['id']}/approve").status_code == 409
        c.post("/api/ops/reset", json={"room": ROOM})


def test_orchestrator_runs_an_event_with_gemini_tools(monkeypatch):
    from types import SimpleNamespace

    from campuspulse.agent import gemini, team
    ops.manual = True
    turns = iter([[("inspect_room", {"room_id": ROOM})],
                  [("set_ac_mode", {"room_id": ROOM, "mode": "occupancy", "reason": "empty, no class"}),
                   ("schedule_check", {"room_id": ROOM, "expect": "ac_off"})],
                  [("close_incident", {"outcome": "acted", "note": f"{ROOM} was cooled while empty; switched to occupancy."})]])

    def generate(client, retries=3, **kw):
        from google.genai import types
        calls = [SimpleNamespace(name=n, args=a) for n, a in next(turns)]
        return SimpleNamespace(function_calls=calls, text="", candidates=[SimpleNamespace(
            content=types.Content(role="model", parts=[types.Part(text="calling")]))])

    saved = team.settings.gemini_api_key
    monkeypatch.setattr(gemini, "_generate", generate)
    monkeypatch.setattr(gemini, "_client", lambda: None)
    object.__setattr__(team.settings, "gemini_api_key", "test")
    try:
        with TestClient(api.app) as c:
            c.post("/api/ops/reset", json={"room": ROOM})
            c.post("/api/ingest", json=board(), headers=H)
            inc = ops.open(ROOM, "empty_cooled", board(), "esp32-b0805")
            ops.drain()
            assert inc["status"] == "verifying" and control.mode(ROOM) == "occupancy"
            assert inc["orchestrated_by"].startswith("Gemini orchestrator")
            dids = " | ".join(s["did"] for s in inc["steps"])
            assert "inspected B-08-05" in dids and "decided: AC" in dids and "approved" in dids
            c.post("/api/ops/reset", json={"room": ROOM})
    finally:
        object.__setattr__(team.settings, "gemini_api_key", saved)

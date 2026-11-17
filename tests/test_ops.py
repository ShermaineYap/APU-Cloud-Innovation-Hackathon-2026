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
        # reset also undoes a campus-wide approval that covered the room (Autopilot, Actions)
        control.apply({"id": "blk-b", "kind": "occupancy_linked_ac", "rooms": [ROOM, "B-08-03"], "title": "t"}, "approved")
        assert control.mode(ROOM) == "occupancy"
        c.post("/api/ops/reset", json={"room": ROOM})
        assert control.mode(ROOM) == "schedule" and control.mode("B-08-03") == "occupancy"


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


def test_orchestrator_directs_its_agents_on_an_event(monkeypatch):
    """Scripted Gemini: the orchestrator delegates to the Investigator, Controller, Verifier and Reporter,
    and each agent runs its own tools."""
    from types import SimpleNamespace

    from campuspulse.agent import gemini, team
    turns = iter([
        [("investigator", {"room_id": ROOM})],                                 # orchestrator
        [("inspect_room", {"room_id": ROOM})],                                 # investigator agent
        "Room is empty with the AC on. Recommend: switch the AC to follow occupancy.",
        [("controller", {"room_id": ROOM, "instruction": "Switch to occupancy: empty, AC on."})],  # orchestrator
        [("set_ac_mode", {"room_id": ROOM, "mode": "occupancy", "reason": "empty, AC on"})],        # controller agent
        "The policy guard applied occupancy mode.",
        [("verifier", {"room_id": ROOM, "expect": "ac_off"}), ("reporter", {})],  # orchestrator
        "AC was running in an empty room; switched to occupancy mode, check pending.",  # reporter agent
        [("close_incident", {"outcome": "acted"})],                            # orchestrator
    ])

    def generate(client, retries=3, **kw):
        from google.genai import types
        t = next(turns)
        if isinstance(t, str):
            return SimpleNamespace(function_calls=[], text=t, candidates=[])
        calls = [SimpleNamespace(name=n, args=a) for n, a in t]
        return SimpleNamespace(function_calls=calls, text="", candidates=[SimpleNamespace(
            content=types.Content(role="model", parts=[types.Part(text="calling")]))])

    saved = team.settings.gemini_api_key
    monkeypatch.setattr(gemini, "_generate", generate)
    monkeypatch.setattr(gemini, "_client", lambda: None)
    object.__setattr__(team.settings, "gemini_api_key", "test")
    try:
        with TestClient(api.app) as c:
            c.post("/api/ingest", json=board(), headers=H)
            inc = ops.open(ROOM, "empty_cooled", board(), "esp32-b0805")
            ops.drain()
            assert inc["status"] == "verifying" and control.mode(ROOM) == "occupancy"
            agents = [s["agent"] for s in inc["steps"]]
            for who in ("Watcher (rules)", "Orchestrator", "Investigator", "Controller", "Policy guard (rules)",
                        "Device command", "Verifier (rules)", "Reporter", "Checker"):
                assert who in agents, who
            assert agents.index("Investigator") < agents.index("Controller") < agents.index("Reporter")
            assert inc["note"].startswith("AC was running")
    finally:
        object.__setattr__(team.settings, "gemini_api_key", saved)


def test_scan_finds_a_real_problem_and_requests_show_in_the_feed():
    with TestClient(api.app) as c:
        out = c.post("/api/ops/scan").json()
        assert out.get("opened") or "No room needs attention" in out["why"]
        r = c.post("/api/agent", json={"message": "Give me the morning brief"}).json()
        feed = c.get("/api/ops").json()["incidents"]
        req = next(i for i in feed if i["id"] == r["activity"])
        assert req["kind"] == "request" and req["status"] == "done"
        assert [s["agent"] for s in req["steps"]][:2] == ["Person", "Orchestrator"]

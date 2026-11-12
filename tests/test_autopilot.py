from fastapi.testclient import TestClient

from campuspulse import api, autopilot
from campuspulse.campus import TIMETABLE


def _overlaps(moves_plan):
    """Every room's classes in the plan never overlap."""
    by_room = {}
    for start, end, room in moves_plan:
        by_room.setdefault(room, []).append((start, end))
    for spans in by_room.values():
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            if b0 < a1:
                return True
    return False


def test_plan_saves_energy_without_double_booking(store):
    p = autopilot.plan(store)
    assert p["solver"]["status"].endswith("Optimal)") or "Optimal" in p["solver"]["status"]
    assert p["plan"]["kwh"] <= p["baseline"]["kwh"] and p["saving"]["kwh"] > 0
    assert p["plan"]["co2_breach_hours"] <= p["baseline"]["co2_breach_hours"]
    wd = autopilot.date.fromisoformat(p["day"]).weekday()
    moved = {(m["module"], m["time"].split("–")[0], m["from"]): m["to"] for m in p["moves"]}
    spans = []
    for s in TIMETABLE:
        if s.weekday == wd:
            key = (s.module, f"{s.start_min // 60:02d}:{s.start_min % 60:02d}", s.room_id)
            spans.append((s.start_min, s.end_min, moved.get(key, s.room_id)))
    assert not _overlaps(spans)
    assert all(m["decision"] in ("auto", "review") for m in p["moves"])
    assert all(m["decision"] == "review" for m in p["moves"] if m["reasons"])
    assert p["accuracy"]["classes"] > 0


def test_constraints_are_respected(store):
    none = autopilot.plan(store, constraints=autopilot.parse_instructions_offline("Don't move any classes"))
    assert none["moves"] == [] and none["constraints"]["no_moves"]
    capped = autopilot.plan(store, constraints={"max_moves": 2})
    assert len(capped["moves"]) <= 2
    near = autopilot.plan(store, constraints=autopilot.parse_instructions_offline("keep moves in the same block"))
    assert all(m["from"].split("-")[0][:2] == m["to"].split("-")[0][:2] for m in near["moves"])
    free = autopilot.plan(store)
    if free["moves"]:
        code = free["moves"][0]["module"].split()[0]
        held = autopilot.plan(store, constraints=autopilot.parse_instructions_offline(f"keep {code} where it is"))
        assert all(not m["module"].startswith(code) for m in held["moves"])


def test_plan_api_and_apply_sends_commands():
    with TestClient(api.app) as c:
        p = c.post("/api/autopilot/plan", json={"instructions": "at most 3 moves"}).json()
        assert p["explanation"]["headline"] and "rule-based" in p["constraints_read_by"]
        assert len(p["moves"]) <= 3
        assert c.get("/api/autopilot/latest").json()["id"] == p["id"]
        r = c.post("/api/autopilot/apply", json={"plan_id": p["id"], "approve_review": True}).json()
        assert r["ok"] and set(r["rooms"]) and len(r["notices"]["notices"]) == len(r["moves_applied"])
        assert c.post("/api/autopilot/apply", json={"plan_id": "nope"}).status_code == 404

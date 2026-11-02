"""The decision engine must find the problems planted in the simulator."""
from campuspulse import analytics, decisions, network
from campuspulse.agent.canned import CANNED
from campuspulse.campus import ROOM_BY_ID
from campuspulse.agent.gemini import _offline_answer
from campuspulse.simulator import OUTAGE_GATEWAY, STUCK_SENSOR_ROOM, WEAK_LINK_ROOM, WEEKEND_AC_ROOM


def test_actions_cover_planted_problems(store):
    acts = decisions.build_actions(store)
    kinds = {a["kind"] for a in acts}
    assert {"after_hours_ac", "occupancy_linked_ac", "air_quality", "right_size", "data_reliability"} <= kinds
    after_hours = [a for a in acts if a["kind"] == "after_hours_ac"]
    assert any(WEEKEND_AC_ROOM in a["rooms"] for a in after_hours)
    reliability_rooms = {r for a in acts if a["kind"] == "data_reliability" for r in a["rooms"]}
    assert {WEAK_LINK_ROOM, STUCK_SENSOR_ROOM} <= reliability_rooms
    # Emerge from APU's real timetable: small classes in auditoriums, crowded sessions with stale air.
    assert any(a["kind"] == "right_size" and any(r.startswith("AUD-") for r in a["rooms"]) for a in acts)
    assert any(a["kind"] == "air_quality" for a in acts)
    assert {"AUD", "B", "TL"} <= {ROOM_BY_ID[r].block for a in acts
                                  if a["kind"] == "occupancy_linked_ac" for r in a["rooms"]}


def test_every_action_carries_evidence(store):
    for a in decisions.build_actions(store):
        assert a["evidence_sql"] and a["evidence_rows"], a["title"]
        assert a["chart"]["x"] and len(a["chart"]["x"]) == len(a["chart"]["y"])
        assert 0 <= a["confidence"] <= 100
        assert a["rm_per_week"] == round(a["kwh_per_week"] * 0.5, 2) or abs(
            a["rm_per_week"] - a["kwh_per_week"] * 0.5) < 0.06


def test_actions_sorted_by_score(store):
    scores = [a["score"] for a in decisions.build_actions(store)]
    assert scores == sorted(scores, reverse=True)


def test_network_flags_weak_and_stuck_nodes(store):
    health = {h.room_id: h for h in network.link_health(store)}
    assert health[WEAK_LINK_ROOM].level == "low"
    assert health[STUCK_SENSOR_ROOM].level != "high"
    assert any("stuck" in i for i in health[STUCK_SENSOR_ROOM].issues)
    assert health["AUD-1"].level == "high"


def test_gateway_outage_detected(store):
    out = network.gateway_outages(store, days=7)
    assert set(out["gateway_id"]) == {OUTAGE_GATEWAY}


def test_kpis_and_room_detail(store):
    k = analytics.kpis(store)
    assert k["wasted_kwh"] > 0 and 0 < k["wasted_share"] < 1
    d = analytics.room_detail(store, WEEKEND_AC_ROOM)
    assert len(d["forecast"]["hour"]) == 24
    assert d["recent"]["hour"]


def test_all_canned_questions_run(store):
    for c in CANNED:
        out = _offline_answer(store, c["q"])
        assert out["sql"], c["q"]


def test_campus_uses_real_apu_timetable():
    from campuspulse.campus import APU_TIMETABLE, ROOMS, TIMETABLE
    assert all(r.apu_room in APU_TIMETABLE["rooms"] for r in ROOMS)
    assert len(TIMETABLE) > 200
    blob = str(APU_TIMETABLE)
    assert "SAMACCOUNTNAME" not in blob and "LECTID" not in blob  # no lecturer data kept

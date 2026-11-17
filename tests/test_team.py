"""The agent team: checker (critic), coordinator, roster, and the scheduled run."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from campuspulse.agent import team


def test_checker_accepts_engine_figures_and_rounding():
    facts = {"saving_kwh": 564, "saving_pct": "42%", "rm": "RM 1,253", "share": 0.413, "hours": 30.5}
    assert team.check_numbers("Save 564 kWh (42%) and RM 1,253; 41% of cooling; 30.5 to 13 hours.", facts | {"after": 13})["ok"]
    assert team.check_numbers("Classes start at 08:30 in 3 rooms.", facts)["ok"]  # clock times and small counts pass


def test_checker_rejects_an_invented_figure():
    v = team.check_numbers("The plan saves 900 kWh.", {"saving_kwh": 564})
    assert not v["ok"] and v["unknown"] == ["900"]


def test_generator_critic_loop_sends_one_draft_back_then_gives_up():
    drafts, seen = iter([{"t": "saves 900 kWh"}, {"t": "saves 564 kWh"}]), []

    def write(feedback):
        seen.append(feedback)
        return next(drafts)

    out, v = team.checked_write(write, {"kwh": 564}, texts=lambda d: d["t"])
    assert out == {"t": "saves 564 kWh"} and v["rounds"] == 2
    assert seen[0] is None and "900" in seen[1]  # the reason travels back to the writer

    out, v = team.checked_write(lambda fb: {"t": "saves 900 kWh"}, {"kwh": 564}, texts=lambda d: d["t"])
    assert out is None and not v["ok"]  # two bad drafts: the caller falls back to the template


def _fake_gemini(monkeypatch, turns):
    """Make the coordinator's Gemini return scripted turns: lists of (tool, args) or a final text."""
    from campuspulse.agent import gemini
    it = iter(turns)

    def generate(client, retries=3, **kw):
        t = next(it)
        if isinstance(t, str):
            return SimpleNamespace(function_calls=[], text=t, candidates=[])
        from google.genai import types
        calls = [SimpleNamespace(name=n, args=a) for n, a in t]
        content = types.Content(role="model", parts=[types.Part(text="calling")])
        return SimpleNamespace(function_calls=calls, text="", candidates=[SimpleNamespace(content=content)])

    monkeypatch.setattr(gemini, "_generate", generate)
    monkeypatch.setattr(gemini, "_client", lambda: None)
    object.__setattr__(team.settings, "gemini_api_key", "test")


def test_coordinator_calls_specialists_in_order_and_checks_its_reply(monkeypatch):
    saved = team.settings.gemini_api_key
    try:
        _fake_gemini(monkeypatch, [[("ask_campus_data", {"question": "Which room wastes most?"})],
                                   [("plan_tomorrow", {"goals": "comfort first"})],
                                   "AUD-2 wasted 548 kWh; tomorrow's plan saves 564 kWh."])
        called = []

        def run_skill(skill, text):
            called.append((skill, text))
            return ({"ask_campus_data": "AUD-2 wasted 548 kWh.", "plan_tomorrow": "Plan saves 564 kWh."}[skill],
                    {"skill": skill})

        run = team.coordinate("Why is AUD-2 wasting energy and what should we do tomorrow?", run_skill, lambda t: "campus_status")
        assert [c[0] for c in called] == ["ask_campus_data", "plan_tomorrow"] and called[1][1] == "comfort first"
        assert run["reply"].startswith("AUD-2 wasted 548") and run["source"].startswith("Gemini orchestrator")
        assert run["steps"][-1]["agent"] == "Checker" and run["steps"][-1]["ok"]

        # a reply with a figure no specialist gave is replaced by the specialists' own words
        _fake_gemini(monkeypatch, [[("campus_status", {})], "We wasted 9,999 kWh."])
        run = team.coordinate("status?", lambda s, t: ("RM 1,253 went to empty rooms.", {"skill": s}), lambda t: "campus_status")
        assert run["reply"] == "RM 1,253 went to empty rooms." and not run["steps"][-1]["ok"]
    finally:
        object.__setattr__(team.settings, "gemini_api_key", saved)


def test_team_endpoints_and_scheduled_run():
    from campuspulse import api
    with TestClient(api.app) as c:
        roster = c.get("/api/agents").json()
        ids = {a["id"] for a in roster["agents"]}
        assert {"orchestrator", "watcher", "guard", "verifier", "sql", "goal_reader", "optimiser", "explainer", "checker"} <= ids
        assert all(a["prompt"] for a in roster["agents"] if a["kind"] == "llm")  # every prompt is on show

        r = c.post("/api/agent", json={"message": "Give me the morning brief"}).json()
        assert r["reply"] and r["steps"] and r["skills"] == ["campus_status"]  # Gemini is off in tests: keyword router

        assert c.post("/api/autopilot/nightly").status_code == 401
        before = len(c.get("/api/commands").json()["commands"])
        n = c.post("/api/autopilot/nightly?key=test-key&apply=none").json()
        assert n["applied"] is False and n["saving_kwh"] > 0
        assert len(c.get("/api/commands").json()["commands"]) == before
        n = c.post("/api/autopilot/nightly?key=test-key").json()
        assert n["applied"] is True and n["rooms"] > 0
        runs = c.get("/api/agents").json()
        assert runs["scheduled"]["applied"] and runs["runs"][0]["via"] == "Cloud Scheduler"

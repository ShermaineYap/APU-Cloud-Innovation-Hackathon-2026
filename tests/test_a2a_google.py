import csv
import io

from fastapi.testclient import TestClient

from campuspulse import api
from campuspulse.a2a import route
from campuspulse.config import settings


def client():
    return TestClient(api.app)


def send(c, text, rpc_id=1, headers=None, **extra):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": "message/send",
            "params": {"message": {"role": "user", "kind": "message", "messageId": "m1",
                                   "parts": [{"kind": "text", "text": text}], **extra}}}
    return c.post("/a2a", json=body, headers=headers or {})


def test_agent_card_describes_three_read_only_skills():
    with client() as c:
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            card = c.get(path).json()
            assert card["name"] == "CampusPulse" and card["url"].endswith("/a2a")
            assert card["protocolVersion"] and card["preferredTransport"] == "JSONRPC"
            assert {s["id"] for s in card["skills"]} == {"ask_campus_data", "plan_tomorrow", "campus_status"}
            assert "security" not in card  # no key configured in tests


def test_router_picks_the_skill_from_the_wording():
    assert route("Plan tomorrow, comfort first") == "plan_tomorrow"
    assert route("How is the campus doing this week? Give me a summary") == "campus_status"
    assert route("Which rooms wasted the most energy cooling while empty this week?") == "ask_campus_data"
    assert route("anything", "campus_status") == "campus_status"


def test_message_send_runs_each_skill_and_never_applies_a_plan():
    with client() as c:
        before = len(c.get("/api/commands").json()["commands"])
        r = send(c, "Which rooms wasted the most energy cooling while empty this week?").json()
        parts = r["result"]["parts"]
        assert r["result"]["role"] == "agent" and parts[0]["kind"] == "text" and parts[1]["data"]["skill"] == "ask_campus_data"
        assert parts[1]["data"]["sql"]  # the SQL behind the answer travels with it

        r = send(c, "Plan tomorrow but don't move any classes", rpc_id=2).json()
        data = r["result"]["parts"][1]["data"]
        assert data["skill"] == "plan_tomorrow" and data["applied"] is False and data["moves"] == []
        assert "Nothing has been applied" in r["result"]["parts"][0]["text"]
        assert len(c.get("/api/commands").json()["commands"]) == before  # no AC command went out

        r = send(c, "Give me the morning brief", rpc_id=3).json()
        assert r["result"]["parts"][1]["data"]["skill"] == "campus_status"


def test_protocol_errors_are_well_formed():
    with client() as c:
        assert c.post("/a2a", content=b"not json").json()["error"]["code"] == -32700
        assert c.post("/a2a", json={"id": 1}).json()["error"]["code"] == -32600
        r = c.post("/a2a", json={"jsonrpc": "2.0", "id": 9, "method": "message/stream", "params": {}}).json()
        assert r["id"] == 9 and r["error"]["code"] == -32601
        r = c.post("/a2a", json={"jsonrpc": "2.0", "id": 5, "method": "message/send",
                                 "params": {"message": {"parts": []}}}).json()
        assert r["error"]["code"] == -32602


def test_optional_key_protects_the_endpoint():
    object.__setattr__(settings, "a2a_key", "s3cret")
    try:
        with client() as c:
            assert send(c, "status").status_code == 401
            assert send(c, "Give me the morning brief", headers={"Authorization": "Bearer s3cret"}).status_code == 200
            assert c.get("/.well-known/agent-card.json").json()["security"] == [{"bearer": []}]
    finally:
        object.__setattr__(settings, "a2a_key", None)


def test_google_sheets_and_calendar_exports():
    with client() as c:
        c.post("/api/autopilot/plan", json={"instructions": ""})  # exports follow the latest plan
        r = c.get("/api/autopilot/latest.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert rows and {"module", "from_room", "to_room", "decision"} <= set(rows[0])
        sched = list(csv.DictReader(io.StringIO(c.get("/api/autopilot/schedule.csv").text)))
        assert len(sched) == 20
        ics = c.get("/api/autopilot/latest.ics").text
        assert ics.startswith("BEGIN:VCALENDAR") and ics.count("BEGIN:VEVENT") == len(rows)
        assert "DTSTART:" in ics and ics.rstrip().endswith("END:VCALENDAR")


def test_integrations_panel_lists_google_services():
    with client() as c:
        d = c.get("/api/integrations").json()
        ids = {i["id"]: i for i in d["items"]}
        assert {"gemini", "gemini_enterprise", "cloud_run", "bigquery", "pubsub", "sheets", "calendar", "looker"} <= set(ids)
        assert ids["gemini_enterprise"]["state"] == "ready"
        assert ids["gemini_enterprise"]["link"].endswith("/.well-known/agent-card.json")
        assert ids["sheets"]["formula"].startswith('=IMPORTDATA("')
        assert ids["calendar"]["link"].startswith("https://calendar.google.com/calendar/r?cid=")


def test_whole_campus_lists_every_physical_room():
    with client() as c:
        d = c.get("/api/campus", params={"at": "2026-11-11T14:00"}).json()
        assert d["weekday"] == "Wed" and d["rooms"] == 213 and d["modelled"] == 20
        rooms = [r for b in d["blocks"] for r in b["rooms"]]
        assert len(rooms) == 213 and not any(r["full_name"].upper().startswith("ONL") for r in rooms)
        assert sum(1 for r in rooms if r["modelled"]) == 20
        assert d["rooms_in_class"] == sum(r["status"] == "in_class" for r in rooms) > 0
        assert d["classes_today"] == sum(len(r["classes"]) for r in rooms)
        sat = c.get("/api/campus", params={"at": "2026-11-14T11:00"}).json()
        assert sat["rooms_in_class"] == 0
        assert c.get("/api/campus", params={"at": "nope"}).status_code == 422


def test_public_url_makes_google_links_usable():
    object.__setattr__(settings, "public_url", "https://campuspulse.example.invalid")
    try:
        with client() as c:
            items = {i["id"]: i for i in c.get("/api/integrations").json()["items"]}
            assert items["sheets"]["state"] == "on" and "https://campuspulse.example.invalid/api/" in items["sheets"]["formula"]
            assert items["calendar"]["state"] == "on"
            assert items["gemini_enterprise"]["link"].startswith("https://campuspulse.example.invalid/")
    finally:
        object.__setattr__(settings, "public_url", None)

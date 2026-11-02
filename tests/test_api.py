from fastapi.testclient import TestClient

from campuspulse import api
from campuspulse.store import get_store

GOOD = {"device_id": "esp32-b0805", "room_id": "B-08-05", "occupied": True, "co2_ppm": 790,
        "temp_c": 24.5, "humidity_pct": 57, "watts": 2200, "rssi_dbm": -60}


def client():
    return TestClient(api.app)


def test_overview_and_brief():
    with client() as c:
        o = c.get("/api/overview").json()
        assert len(o["rooms"]) == 20 and o["kpis"]["wasted_kwh"] > 0
        b = c.get("/api/brief").json()
        assert b["brief"]["headline"] and b["actions"]
        assert "template" in b["brief"]["source"]


def test_ingest_requires_key():
    with client() as c:
        assert c.post("/api/ingest", json=GOOD).status_code == 401
        assert c.post("/api/ingest", json=GOOD, headers={"X-Device-Key": "wrong"}).status_code == 401


def test_ingest_validates_and_marks_real():
    with client() as c:
        h = {"X-Device-Key": "test-key"}
        assert c.post("/api/ingest", json=GOOD | {"co2_ppm": 5}, headers=h).status_code == 422
        assert c.post("/api/ingest", json=GOOD | {"room_id": "Z-99"}, headers=h).status_code == 422
        r = c.post("/api/ingest", json=[GOOD, GOOD | {"co2_ppm": 800}], headers=h)
        assert r.json()["accepted"] == 2
        rows = get_store().query("SELECT simulated, ac_on FROM readings WHERE device_id = 'esp32-b0805'")
        assert len(rows) == 2 and not rows["simulated"].any() and rows["ac_on"].all()
        assert c.get("/api/health").json()["live_node_online"] is True


def test_decide_logs_decision():
    with client() as c:
        aid = c.get("/api/actions").json()[0]["id"]
        assert c.post(f"/api/actions/{aid}/decide", json={"status": "approved"}).json()["ok"]
        assert c.post("/api/actions/nope/decide", json={"status": "approved"}).status_code == 404
        log = c.get("/api/decisions").json()
        assert log[0]["action_id"] == aid and log[0]["status"] == "approved"


def test_room_network_ask():
    with client() as c:
        assert c.get("/api/rooms/AUD-1").status_code == 200
        assert c.get("/api/rooms/nope").status_code == 404
        assert c.get("/api/network").json()["nodes"]
        a = c.post("/api/ask", json={"question": "Which rooms wasted the most energy cooling while empty this week?"})
        assert a.json()["rows"] and a.json()["sql"].startswith("SELECT")
        assert c.get("/").status_code == 200

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


def test_camera_counts_override_people_and_keep_sensor_data():
    with client() as c:
        h = {"X-Device-Key": "test-key"}
        cam = {"device_id": "cam-aud1", "room_id": "AUD-1", "occupied": True, "people": 7}
        assert c.post("/api/ingest", json=cam | {"people": None}, headers=h).status_code == 422
        assert c.post("/api/ingest", json=[cam] * 3, headers=h).json()["accepted"] == 3
        room = next(r for r in c.get("/api/overview").json()["rooms"] if r["room_id"] == "AUD-1")
        assert room["people"] == 7 and room["people_source"] == "camera"
        assert room["co2_ppm"] and room["watts"] is not None  # air and energy still from the sensor node
        api.cache.clear()  # the network panel is cached for a minute
        node = next(n for n in c.get("/api/network").json()["nodes"] if n["device_id"] == "cam-aud1")
        assert not any("stuck" in i for i in node["issues"]) and node["avg_rssi_dbm"] is None
        # A sensor reading without its air fields is rejected; only cameras may omit them.
        assert c.post("/api/ingest", json={k: v for k, v in GOOD.items() if k != "co2_ppm"}, headers=h).status_code == 422


def test_caption_and_report():
    with client() as c:
        acts = c.get("/api/actions").json()
        cap = c.get(f"/api/actions/{acts[0]['id']}/caption").json()
        assert cap["caption"].endswith(".") and cap["source"] == "template"
        assert c.get("/api/actions/nope/caption").status_code == 404
        c.post(f"/api/actions/{acts[0]['id']}/decide", json={"status": "approved"})
        r = c.get("/api/report.pdf")
        assert r.status_code == 200 and r.content[:5] == b"%PDF-"
        assert "attachment" in r.headers["content-disposition"]


def test_overview_replays_a_past_time():
    import pandas as pd
    with client() as c:
        live = c.get("/api/overview").json()
        assert live["replay_at"] is None
        last = pd.Timestamp(live["replay_range"]["last"])
        # The most recent Wednesday before the last reading, mid-morning: classes are on.
        wed = (last - pd.Timedelta(days=(last.weekday() - 2) % 7 or 7)).normalize()
        busy = c.get("/api/overview", params={"at": f"{wed:%Y-%m-%d}T11:00"}).json()
        assert busy["replay_at"].startswith(f"{wed:%Y-%m-%d}T11:00")
        assert any(r["status"] in ("in_use", "stale_air") for r in busy["rooms"])
        # Saturday afternoon: the planted weekend fault leaves AUD-2 cooled while empty.
        sat = wed + pd.Timedelta(days=3)
        if sat < last:
            weekend = {r["room_id"]: r for r in c.get("/api/overview", params={"at": f"{sat:%Y-%m-%d}T14:00"}).json()["rooms"]}
            assert weekend["AUD-2"]["status"] == "empty_cooled"
        assert c.get("/api/overview", params={"at": "not-a-time"}).status_code == 422
        assert c.get("/").headers["cache-control"] == "no-cache"

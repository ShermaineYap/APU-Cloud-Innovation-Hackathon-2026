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
        # Hardware must report its Wi-Fi signal; air fields are optional (a node sends what it can measure).
        assert c.post("/api/ingest", json={k: v for k, v in GOOD.items() if k != "rssi_dbm"}, headers=h).status_code == 422


def test_bare_board_and_partial_sensors_are_accepted_without_nan():
    with client() as c:
        h = {"X-Device-Key": "test-key"}
        bare = {"device_id": "esp32-b0805", "room_id": "B-08-05", "occupied": True, "ac_on": True,
                "rssi_dbm": -61, "packets_sent": 1, "packets_acked": 1}
        assert c.post("/api/ingest", json=bare, headers=h).status_code == 200
        r = c.get("/api/overview")
        assert "NaN" not in r.text  # missing values travel as null, which browsers can parse
        room = next(x for x in r.json()["rooms"] if x["room_id"] == "B-08-05")
        assert room["simulated"] is False and room["co2_ppm"] is None and room["temp_c"] is None
        assert room["occupied"] is True and room["status"] == "in_use" and room["rssi_dbm"] == -61
        dht = bare | {"temp_c": 26.4, "humidity_pct": 61.0}
        assert c.post("/api/ingest", json=dht, headers=h).status_code == 200
        room = next(x for x in c.get("/api/overview").json()["rooms"] if x["room_id"] == "B-08-05")
        assert room["temp_c"] == 26.4 and room["co2_ppm"] is None


def test_replayed_dataset_is_labelled_not_live():
    with client() as c:
        h = {"X-Device-Key": "test-key"}
        row = {"device_id": "replay-uci-occupancy", "room_id": "AUD-3", "occupied": True,
               "co2_ppm": 1105.0, "temp_c": 22.4, "humidity_pct": 27.1}  # no Wi-Fi signal: it's a dataset
        assert c.post("/api/ingest", json=row, headers=h).status_code == 200
        o = c.get("/api/overview").json()
        room = next(x for x in o["rooms"] if x["room_id"] == "AUD-3")
        assert room["replayed"] is True and room["status"] == "stale_air" and room["co2_ppm"] == 1105.0
        assert o["mode"]["replay_online"] is True and o["mode"]["replay_device"] == "replay-uci-occupancy"


def test_dataset_rows_read_through_campuspulse_rules():
    from campuspulse import replay
    rows = replay.load("2015-02-09 07:30")
    assert len(rows) > 4000 and rows[0]["ts"] >= "2015-02-09 07:30"
    states = {replay.reading(r)["status"] for r in rows}
    assert {"in_use", "stale_air", "idle"} <= states  # the real data contains each case


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


def test_study_finder_lists_free_rooms_and_closes_at_night():
    import pandas as pd
    from campuspulse.campus import TIMETABLE
    with client() as c:
        last = pd.Timestamp(c.get("/api/overview").json()["replay_range"]["last"])
        wed = (last - pd.Timedelta(days=(last.weekday() - 2) % 7 or 7)).normalize()
        st = c.get("/api/overview", params={"at": f"{wed:%Y-%m-%d}T11:00"}).json()["study"]
        assert st["open"]
        booked = {s.room_id for s in TIMETABLE if s.weekday == 2 and s.start_min <= 660 < s.end_min}
        for r in st["rooms"]:
            assert r["room_id"] not in booked and r["free_minutes"] >= 30
        cooled = [r["cooled"] for r in st["rooms"]]
        assert cooled == sorted(cooled, reverse=True)  # already-cooled rooms first
        night = c.get("/api/overview", params={"at": f"{wed:%Y-%m-%d}T23:00"}).json()["study"]
        assert not night["open"] and night["rooms"] == []


def test_published_dataset_plays_in_its_own_room_not_an_apu_room():
    from datetime import timedelta
    from campuspulse.replay import replayer
    with client() as c:
        before = next(x for x in c.get("/api/overview").json()["rooms"] if x["room_id"] == "B-08-05")
        m = c.post("/api/source", json={"mode": "replay", "preset": "stale", "every_s": 2}).json()
        rp = m["replay"]
        assert m["source"] == "replay" and rp["running"] and rp["site"]["name"].startswith("Office room, Mons")
        assert rp["reading"]["occupied"] and rp["reading"]["co2_ppm"] > 300 and rp["reading"]["light_lux"] >= 0
        # The position follows the clock, not a thread: pretend two minutes have passed (60 rows of 5 min).
        replayer.run["started"] -= timedelta(seconds=120)
        rp = c.get("/api/source").json()["replay"]
        assert rp["rows_shown"] == 61 and len(rp["history"]) == 61
        assert rp["tally"]["stale_air_min"] > 0 and rp["tally"]["max_co2_ppm"] > 1000
        # Nothing reaches APU's rooms or the warehouse.
        after = next(x for x in c.get("/api/overview").json()["rooms"] if x["room_id"] == "B-08-05")
        assert after["device_id"] == before["device_id"] and not after.get("replayed")
        assert get_store().query("SELECT COUNT(*) AS n FROM readings WHERE room_id = 'B-08-05' "
                                 "AND device_id LIKE 'replay-%'")["n"][0] == 0
        m = c.post("/api/source", json={"mode": "simulation"}).json()
        assert not m["replay"]["running"]


def test_esp32_reading_switches_source_to_live():
    with client() as c:
        h = {"X-Device-Key": "test-key"}
        node = {"device_id": "esp32-b0805", "room_id": "B-08-05", "occupied": True, "ac_on": True,
                "rssi_dbm": -55, "temp_c": 27.1, "humidity_pct": 64}
        assert c.post("/api/ingest", json=node, headers=h).status_code == 200
        m = c.get("/api/source").json()
        assert m["source"] == "esp32" and m["esp32"]["online"] and m["esp32"]["reading"]["temp_c"] == 27.1
        assert c.get("/api/rooms/B-08-05/command", headers=h).json()["ac_mode"]


def test_virtual_node_is_labelled_virtual_never_live():
    with client() as c:
        h = {"X-Device-Key": "test-key"}
        wokwi = {"device_id": "wokwi-b0805", "room_id": "B-07-04", "occupied": True, "co2_ppm": 1200,
                 "temp_c": 26.5, "humidity_pct": 62, "rssi_dbm": -60, "ac_on": True}
        assert c.post("/api/ingest", json=wokwi, headers=h).status_code == 200
        o = c.get("/api/overview").json()
        room = next(x for x in o["rooms"] if x["room_id"] == "B-07-04")
        assert room["virtual"] is True and room["simulated"] is True and room["co2_ppm"] == 1200
        assert o["mode"]["virtual_online"] is True and o["mode"]["virtual_device"] == "wokwi-b0805"
        assert o["mode"]["esp32"]["device"] != "wokwi-b0805"

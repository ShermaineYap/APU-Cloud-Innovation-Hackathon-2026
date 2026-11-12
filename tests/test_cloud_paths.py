"""Cloud-only code paths, checked without a Google Cloud account.

BigQuery ML and the BigQuery store are exercised with fakes: the SQL each one sends is
inspected, and the JSON it would stream is checked for types BigQuery accepts.
"""
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from campuspulse import analytics
from campuspulse.simulator import STEP, Fleet
from campuspulse.store.base import enrich
from campuspulse.store.bq import _json_row


class MLStore:
    """Wraps the local test store and answers BigQuery ML queries with canned frames."""
    supports_ml = True

    def __init__(self, inner, forecast_rows=24, fail=False):
        self.inner, self.forecast_rows, self.fail, self.sql = inner, forecast_rows, fail, []

    def query(self, sql):
        if "ML.FORECAST" in sql or "ML.DETECT_ANOMALIES" in sql:
            self.sql.append(sql)
            if self.fail:
                raise RuntimeError("Not found: Model occupancy_forecast")
            if "ML.FORECAST" in sql:
                start = pd.Timestamp("2030-01-01 00:00")
                return pd.DataFrame({"hour": pd.date_range(start, periods=self.forecast_rows, freq="h"),
                                     "forecast": np.linspace(-2, 30, self.forecast_rows),
                                     "low": 0.0, "high": 40.0})
            return pd.DataFrame({"hour": [pd.Timestamp("2026-11-10 03:00")], "kwh": [9.5],
                                 "expected": [0.4], "score": [0.998]})
        return self.inner.query(sql)


def test_room_drawer_uses_bigquery_ml_when_trained(store):
    s = MLStore(store)
    d = analytics.room_detail(s, "AUD-1")
    assert d["forecast_method"].startswith("BigQuery ML ARIMA_PLUS")
    assert d["anomaly_method"].startswith("BigQuery ML")
    assert len(d["forecast"]["hour"]) == 24 and min(d["forecast"]["forecast"]) >= 0  # clipped at zero
    assert d["anomalies"][0]["score"] == 0.998
    fc_sql = next(q for q in s.sql if "ML.FORECAST" in q)
    assert "room_id = 'AUD-1'" in fc_sql and "MODEL occupancy_forecast" in fc_sql
    assert f"{analytics.ML_HORIZON_H} AS horizon" in fc_sql


def test_room_drawer_falls_back_when_model_missing_or_stale(store):
    for s in (MLStore(store, fail=True), MLStore(store, forecast_rows=5)):
        d = analytics.room_detail(s, "AUD-1")
        assert d["forecast_method"] == "hour-of-week seasonal baseline"
        assert len(d["forecast"]["hour"]) == 24
    assert analytics.room_detail(store, "AUD-1")["forecast_method"] == "hour-of-week seasonal baseline"


def test_ml_horizon_matches_training():
    from campuspulse.store.base import SQL_DIR
    assert f"horizon = {analytics.ML_HORIZON_H}" in (SQL_DIR / "ml.sql").read_text()


def test_bigquery_rows_are_valid_json():
    row = enrich({"ts": datetime.now(timezone.utc), "device_id": "cam-aud1", "room_id": "AUD-1",
                  "people": np.int64(7), "occupied": np.bool_(True), "co2_ppm": float("nan"),
                  "watts": None, "packets_sent": 2.0, "packets_acked": 1, "simulated": False})
    out = _json_row(row)
    text = json.dumps(out, allow_nan=False)  # raises if a NaN slipped through
    assert out["people"] == 7 and isinstance(out["people"], int) and out["packets_sent"] == 2
    assert out["co2_ppm"] is None and out["occupied"] is True
    assert "T" in out["ts"] and not math.isnan(out["weekday"])
    assert '"ts_local"' in text


def test_simulator_catches_up_in_five_minute_steps():
    f = Fleet(end=datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc))
    f.last_ts = f.end
    assert f.catch_up(f.end + timedelta(seconds=10)) == []  # too soon
    rows = f.catch_up(f.end + timedelta(minutes=17))
    stamps = sorted({r["ts"] for r in rows})
    assert len(stamps) == 4  # +5, +10, +15 and now (+17)
    assert stamps[1] - stamps[0] == STEP
    assert f.last_ts == stamps[-1]


def test_request_mode_simulates_on_request():
    from fastapi.testclient import TestClient
    from campuspulse import api
    from campuspulse.store import get_store

    saved = (api.settings.sim_mode, api.settings.live_simulator)
    object.__setattr__(api.settings, "sim_mode", "request")  # Settings is a frozen dataclass
    object.__setattr__(api.settings, "live_simulator", True)
    try:
        with TestClient(api.app) as c:
            fleet = api.sim["fleet"]
            assert fleet is not None
            fleet.last_ts = fleet.last_ts - timedelta(minutes=12)  # pretend the instance slept
            before = get_store().reading_count()
            c.get("/api/health")
            assert get_store().reading_count() > before
        assert api.sim["fleet"] is None
    finally:
        object.__setattr__(api.settings, "sim_mode", saved[0])
        object.__setattr__(api.settings, "live_simulator", saved[1])


def test_pubsub_push_needs_the_key():
    import base64
    from fastapi.testclient import TestClient
    from campuspulse import api

    msg = {"device_id": "esp32-b0805", "room_id": "B-08-05", "ts": "2026-11-11T02:00:00+00:00",
           "occupied": True, "co2_ppm": 700, "temp_c": 24, "rssi_dbm": -60, "simulated": False}
    env = {"message": {"data": base64.b64encode(json.dumps(msg).encode()).decode()}}
    with TestClient(api.app) as c:
        assert c.post("/api/pubsub/push", json=env).status_code == 401
        assert c.post("/api/pubsub/push?key=wrong", json=env).status_code == 401
        assert c.post("/api/pubsub/push?key=test-key", json=env).json()["stored"] == 1
        assert c.post("/api/pubsub/push?key=test-key", json={"message": {}}).json()["stored"] == 0


def test_gemini_retries_temporary_errors(monkeypatch):
    from types import SimpleNamespace
    from campuspulse.agent import gemini

    class Busy(Exception):
        def __init__(self, code):
            self.code = code

    calls = []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) < 3:
            raise Busy(503)
        return "ok"

    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    assert gemini._generate(SimpleNamespace(models=SimpleNamespace(generate_content=flaky)), model="m") == "ok"
    assert len(calls) == 3

    def denied(**kw):
        raise Busy(400)

    try:
        gemini._generate(SimpleNamespace(models=SimpleNamespace(generate_content=denied)), model="m")
        raise AssertionError("a 400 must not be retried")
    except Busy as e:
        assert e.code == 400


def test_test_traffic_stays_off_the_dashboard():
    from fastapi.testclient import TestClient
    from campuspulse import api

    reading = {"device_id": "test-cloud-check", "room_id": "B-06-03", "occupied": True,
               "co2_ppm": 650, "temp_c": 24.0, "rssi_dbm": -60}
    with TestClient(api.app) as c:
        assert c.post("/api/ingest", json=reading, headers={"X-Device-Key": "test-key"}).status_code == 200
        api.cache.clear()
        room = next(r for r in c.get("/api/overview").json()["rooms"] if r["room_id"] == "B-06-03")
        assert room["device_id"] != "test-cloud-check"
        nodes = c.get("/api/network").json()["nodes"]
        assert all(n["device_id"] != "test-cloud-check" for n in nodes), [n["device_id"] for n in nodes]
        assert ("B-06-03", "sensor") not in api.state["real_rooms"]

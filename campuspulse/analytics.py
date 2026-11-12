"""Room state, KPIs, forecasts and anomalies.

Forecasts and anomalies use BigQuery ML when the cloud backend has the models
trained; otherwise an hour-of-week seasonal baseline computed here, which is
the same idea ARIMA_PLUS captures with its weekly seasonal component.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .campus import ROOM_BY_ID
from .config import settings
from .network import CAMERA_PREFIX, NOT_TEST_SQL, STALE_AFTER_MIN, link_health, room_confidence
from .store import Store

log = logging.getLogger(__name__)


def _now_local(store: Store) -> pd.Timestamp:
    return pd.Timestamp(store.query("SELECT MAX(ts_local) AS t FROM readings")["t"].iloc[0])


def _utc_literal(at_local: pd.Timestamp) -> str:
    """A local wall-clock time as a BigQuery UTC TIMESTAMP literal."""
    t = pd.Timestamp(at_local)
    t = (t.tz_localize(settings.timezone) if t.tzinfo is None else t).tz_convert("UTC")
    return f"TIMESTAMP '{t:%Y-%m-%d %H:%M:%S}+00'"


def replay_range(store: Store) -> dict:
    """First and last local times the demo clock can replay."""
    r = store.query("SELECT MIN(ts_local) AS first, MAX(ts_local) AS last FROM readings").iloc[0]
    return {"first": pd.Timestamp(r["first"]).isoformat(), "last": pd.Timestamp(r["last"]).isoformat()}


def room_states(store: Store, at: pd.Timestamp | None = None) -> list[dict]:
    """Every room's state now, or as it was at a past local time (demo clock / replay)."""
    if at is None:
        anchor, upto = store.latest_ts_sql(), ""
    else:
        anchor = _utc_literal(at)
        upto = f"AND ts <= {anchor}"
    latest = store.query(f"""
        SELECT * FROM readings
        WHERE ts >= TIMESTAMP_SUB({anchor}, INTERVAL 2 HOUR) {upto}
          AND NOT STARTS_WITH(device_id, '{CAMERA_PREFIX}') AND {NOT_TEST_SQL}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY room_id ORDER BY ts DESC, simulated ASC) = 1
    """)
    # Edge cameras count heads; a fresh count overrides the sensor node's estimate.
    cams = store.query(f"""
        SELECT room_id, people, device_id, ts FROM readings
        WHERE STARTS_WITH(device_id, '{CAMERA_PREFIX}')
          AND ts >= TIMESTAMP_SUB({anchor}, INTERVAL {STALE_AFTER_MIN} MINUTE) {upto}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY room_id ORDER BY ts DESC) = 1
    """)
    cam_by_room = {c.room_id: c for c in cams.itertuples()}
    newest = latest["ts"].max() if len(latest) else None
    conf = room_confidence(link_health(store, hours=24))
    by_room = {r.room_id: r for r in latest.itertuples()}
    out = []
    for rid, room in ROOM_BY_ID.items():
        r = by_room.get(rid)
        c = conf.get(rid)
        item = room.as_row() | {"confidence": c.confidence if c else 0,
                                "confidence_level": c.level if c else "low",
                                "issues": c.issues if c else ["no data"]}
        if r is None:
            out.append(item | {"status": "offline"})
            continue
        age = (newest - r.ts).total_seconds() / 60
        cam = cam_by_room.get(rid)
        people = None if pd.isna(r.people) else int(r.people)
        occupied = bool(r.occupied)
        if cam is not None and pd.notna(cam.people):
            people, occupied = int(cam.people), int(cam.people) > 0
        if age > STALE_AFTER_MIN:
            status = "offline"
        elif r.co2_ppm >= settings.co2_limit_ppm and occupied:
            status = "stale_air"
        elif occupied:
            status = "in_use"
        elif r.ac_on:
            status = "empty_cooled"
        else:
            status = "idle"
        out.append(item | {
            "status": status, "people": people, "occupied": occupied,
            "people_source": "camera" if cam is not None and pd.notna(cam.people) else
                             ("estimate" if r.simulated else "sensor"),
            "camera_device": cam.device_id if cam is not None else None,
            "co2_ppm": float(r.co2_ppm), "temp_c": float(r.temp_c), "watts": float(r.watts),
            "ac_on": bool(r.ac_on), "rssi_dbm": float(r.rssi_dbm), "device_id": r.device_id,
            "simulated": bool(r.simulated), "ts": r.ts.isoformat(), "minutes_ago": round(age, 1),
        })
    return out


def kpis(store: Store) -> dict:
    df = store.query("""
        SELECT
          SUM(IF(ac_on_frac >= 0.5 AND COALESCE(max_people, 0) = 0 AND occupied_frac = 0, kwh, 0)) AS wasted_kwh,
          SUM(kwh) AS total_kwh,
          COUNTIF(max_co2_ppm >= %d AND occupied_frac > 0) AS stale_air_hours,
          AVG(IF(occupied_frac > 0, utilisation, NULL)) AS avg_utilisation
        FROM v_room_hourly
        WHERE hour >= DATETIME_SUB((SELECT MAX(hour) FROM v_room_hourly), INTERVAL 7 DAY)
    """ % settings.co2_limit_ppm)
    r = df.iloc[0]
    health = link_health(store, hours=24)
    wasted = float(r.wasted_kwh or 0)
    return {
        "window": "last 7 days",
        "wasted_kwh": round(wasted, 1),
        "wasted_rm": round(wasted * settings.tariff_rm_per_kwh, 2),
        "wasted_kg_co2e": round(wasted * settings.grid_kg_co2e_per_kwh, 1),
        "wasted_share": round(wasted / float(r.total_kwh), 3) if r.total_kwh else 0,
        "stale_air_hours": int(r.stale_air_hours or 0),
        "avg_utilisation": round(float(r.avg_utilisation or 0), 3),
        "fleet_confidence": int(np.mean([h.confidence for h in health])) if health else 0,
        "low_confidence_nodes": sum(h.level == "low" for h in health),
        "tariff_rm_per_kwh": settings.tariff_rm_per_kwh,
        "grid_kg_co2e_per_kwh": settings.grid_kg_co2e_per_kwh,
    }


def _hourly(store: Store, room_id: str, days: int) -> pd.DataFrame:
    df = store.query(f"""
        SELECT hour, weekday, hour_of_day, avg_people, max_co2_ppm, avg_co2_ppm, avg_temp_c, kwh, ac_on_frac
        FROM v_room_hourly
        WHERE room_id = '{room_id}'
          AND hour >= DATETIME_SUB((SELECT MAX(hour) FROM v_room_hourly), INTERVAL {days} DAY)
        ORDER BY hour
    """)
    df["hour"] = pd.to_datetime(df["hour"])
    return df


def seasonal_forecast(hist: pd.DataFrame, horizon_h: int = 24, col: str = "avg_people") -> pd.DataFrame:
    """Hour-of-week mean with a 10th-90th percentile band from the past weeks."""
    if hist.empty:
        return pd.DataFrame(columns=["hour", "forecast", "low", "high"])
    h = hist.assign(how=hist["weekday"] * 24 + hist["hour_of_day"])
    stats = h.groupby("how")[col].agg(forecast="mean", low=lambda s: s.quantile(0.1),
                                       high=lambda s: s.quantile(0.9))
    start = hist["hour"].max() + pd.Timedelta(hours=1)
    future = pd.DataFrame({"hour": pd.date_range(start, periods=horizon_h, freq="h")})
    future["how"] = future["hour"].dt.dayofweek * 24 + future["hour"].dt.hour
    future = future.join(stats, on="how").fillna(0.0)
    return future[["hour", "forecast", "low", "high"]]


def detect_anomalies(hist: pd.DataFrame, col: str = "kwh", z: float = 3.5) -> pd.DataFrame:
    """Robust z-score of each hour against the same hour-of-week in other weeks."""
    if hist.empty:
        return hist
    h = hist.assign(how=hist["weekday"] * 24 + hist["hour_of_day"])
    med = h.groupby("how")[col].transform("median")
    mad = h.groupby("how")[col].transform(lambda s: (s - s.median()).abs().median())
    scale = (1.4826 * mad).clip(lower=max(0.05, float(h[col].std() or 0) * 0.1))
    h["score"] = (h[col] - med) / scale
    h["expected"] = med
    return h[h["score"].abs() >= z]


# BigQuery ML: models trained by bigquery/ml.sql. Their time column is the local wall-clock hour stored
# as a TIMESTAMP, so DATETIME(forecast_timestamp) gives the local hour back.
ML_HORIZON_H = 720  # one training covers 30 days of forecasts (must not exceed HORIZON in ml.sql)
ML_CONFIDENCE = 0.8  # 10th to 90th percentile, the same band as the local baseline


def bqml_forecast(store: Store, room_id: str, after: pd.Timestamp, hours: int = 24) -> pd.DataFrame | None:
    """Next `hours` of occupancy from the ARIMA_PLUS model, or None if it is unavailable or out of date."""
    if not getattr(store, "supports_ml", False):
        return None
    try:
        df = store.query(f"""
            SELECT DATETIME(forecast_timestamp) AS hour, forecast_value AS forecast,
                   prediction_interval_lower_bound AS low, prediction_interval_upper_bound AS high
            FROM ML.FORECAST(MODEL occupancy_forecast,
                             STRUCT({ML_HORIZON_H} AS horizon, {ML_CONFIDENCE} AS confidence_level))
            WHERE room_id = '{room_id}' AND DATETIME(forecast_timestamp) > DATETIME '{after:%Y-%m-%d %H:%M:%S}'
            ORDER BY hour
            LIMIT {hours}
        """)
    except Exception as e:  # model not trained yet, or BigQuery ML unavailable
        log.info("BigQuery ML forecast unavailable for %s: %s", room_id, e)
        return None
    if len(df) < hours:  # the model's horizon has run out: retrain with scripts/bq_setup.py --ml
        return None
    df["hour"] = pd.to_datetime(df["hour"])
    for c in ("forecast", "low", "high"):
        df[c] = df[c].astype(float).clip(lower=0)  # people cannot be negative
    return df[["hour", "forecast", "low", "high"]]


def bqml_anomalies(store: Store, room_id: str, limit: int = 8) -> pd.DataFrame | None:
    """Unusual energy hours from the ARIMA_PLUS energy model, or None if it is unavailable."""
    if not getattr(store, "supports_ml", False):
        return None
    try:
        df = store.query(f"""
            SELECT DATETIME(hour) AS hour, kwh, (lower_bound + upper_bound) / 2 AS expected,
                   anomaly_probability AS score
            FROM ML.DETECT_ANOMALIES(MODEL energy_model, STRUCT(0.99 AS anomaly_prob_threshold))
            WHERE room_id = '{room_id}' AND is_anomaly
            ORDER BY hour DESC
            LIMIT {limit}
        """)
    except Exception as e:
        log.info("BigQuery ML anomalies unavailable for %s: %s", room_id, e)
        return None
    df["hour"] = pd.to_datetime(df["hour"])
    return df.sort_values("hour")


def room_detail(store: Store, room_id: str) -> dict:
    room = ROOM_BY_ID[room_id]
    hist = _hourly(store, room_id, days=28)
    recent = hist[hist["hour"] >= hist["hour"].max() - pd.Timedelta(hours=47)]
    ml_fc = bqml_forecast(store, room_id, after=hist["hour"].max()) if not hist.empty else None
    fc = ml_fc if ml_fc is not None else seasonal_forecast(hist)
    ml_an = bqml_anomalies(store, room_id)
    anomalies = ml_an if ml_an is not None else detect_anomalies(hist).tail(8)

    def series(df, cols):
        return {"hour": [t.isoformat() for t in df["hour"]],
                **{c: [None if pd.isna(v) else round(float(v), 2) for v in df[c]] for c in cols}}

    return {
        "room": room.as_row(),
        "recent": series(recent, ["avg_people", "max_co2_ppm", "avg_temp_c", "kwh", "ac_on_frac"]),
        "forecast": series(fc, ["forecast", "low", "high"]),
        "forecast_method": ("BigQuery ML ARIMA_PLUS (model occupancy_forecast, 80% interval)" if ml_fc is not None
                            else "hour-of-week seasonal baseline"),
        "anomaly_method": ("BigQuery ML ARIMA_PLUS (ML.DETECT_ANOMALIES, model energy_model)" if ml_an is not None
                           else "robust z-score against the same hour of the week"),
        "anomalies": [{"hour": a.hour.isoformat(), "kwh": round(float(a.kwh), 2),
                       "expected": round(float(a.expected), 2), "score": round(float(a.score), 3)}
                      for a in anomalies.itertuples()],
    }

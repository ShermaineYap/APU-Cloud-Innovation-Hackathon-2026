"""Room state, KPIs, forecasts and anomalies.

Forecasts and anomalies use BigQuery ML when the cloud backend has the models
trained; otherwise an hour-of-week seasonal baseline computed here, which is
the same idea ARIMA_PLUS captures with its weekly seasonal component.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .campus import ROOM_BY_ID
from .config import settings
from .network import CAMERA_PREFIX, STALE_AFTER_MIN, link_health, room_confidence
from .store import Store


def _now_local(store: Store) -> pd.Timestamp:
    return pd.Timestamp(store.query("SELECT MAX(ts_local) AS t FROM readings")["t"].iloc[0])


def room_states(store: Store) -> list[dict]:
    latest = store.query(f"""
        SELECT * FROM readings
        WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM readings), INTERVAL 2 HOUR)
          AND NOT STARTS_WITH(device_id, '{CAMERA_PREFIX}')
        QUALIFY ROW_NUMBER() OVER (PARTITION BY room_id ORDER BY ts DESC, simulated ASC) = 1
    """)
    # Edge cameras count heads; a fresh count overrides the sensor node's estimate.
    cams = store.query(f"""
        SELECT room_id, people, device_id, ts FROM readings
        WHERE STARTS_WITH(device_id, '{CAMERA_PREFIX}')
          AND ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM readings), INTERVAL {STALE_AFTER_MIN} MINUTE)
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
        "wasted_share": round(wasted / float(r.total_kwh), 3) if r.total_kwh else 0,
        "stale_air_hours": int(r.stale_air_hours or 0),
        "avg_utilisation": round(float(r.avg_utilisation or 0), 3),
        "fleet_confidence": int(np.mean([h.confidence for h in health])) if health else 0,
        "low_confidence_nodes": sum(h.level == "low" for h in health),
        "tariff_rm_per_kwh": settings.tariff_rm_per_kwh,
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
    future["how"] = future["hour"].dt.weekday * 24 + future["hour"].dt.hour
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


def room_detail(store: Store, room_id: str) -> dict:
    room = ROOM_BY_ID[room_id]
    hist = _hourly(store, room_id, days=28)
    recent = hist[hist["hour"] >= hist["hour"].max() - pd.Timedelta(hours=47)]
    fc = seasonal_forecast(hist)
    anomalies = detect_anomalies(hist).tail(8)

    def series(df, cols):
        return {"hour": [t.isoformat() for t in df["hour"]],
                **{c: [None if pd.isna(v) else round(float(v), 2) for v in df[c]] for c in cols}}

    return {
        "room": room.as_row(),
        "recent": series(recent, ["avg_people", "max_co2_ppm", "avg_temp_c", "kwh", "ac_on_frac"]),
        "forecast": series(fc, ["forecast", "low", "high"]),
        "forecast_method": "hour-of-week seasonal baseline (local)" if settings.backend != "bigquery"
        else "BigQuery ML ARIMA_PLUS when trained, else seasonal baseline",
        "anomalies": [{"hour": a.hour.isoformat(), "kwh": round(a.kwh, 2), "expected": round(a.expected, 2),
                       "score": round(a.score, 1)} for a in anomalies.itertuples()],
    }

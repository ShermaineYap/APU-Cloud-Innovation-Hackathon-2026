"""Network layer: how far can we trust each node's data?

A decision is only as good as the readings behind it. Each device gets a
confidence score from four signals the network already gives us for free:
delivery ratio (MQTT QoS 1 acks), signal strength, freshness, and whether
the sensor value is moving at all.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from .store import Store

STALE_AFTER_MIN = 15
CAMERA_PREFIX = "cam-"


def is_camera(device_id: str) -> bool:
    """Camera nodes count people at the edge; they send no CO2, temperature or signal strength."""
    return str(device_id).startswith(CAMERA_PREFIX)


@dataclass
class LinkHealth:
    device_id: str
    room_id: str
    gateway_id: str
    simulated: bool
    delivery_ratio: float
    avg_rssi_dbm: float | None
    minutes_since_seen: float
    distinct_co2_values: int
    confidence: int
    level: str  # high | medium | low
    issues: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def _score(delivery: float, rssi: float | None, age_min: float, distinct: int) -> tuple[int, list[str]]:
    issues: list[str] = []
    score = 100.0
    if delivery < 0.97:
        score -= min(45, (0.97 - delivery) * 220)
        issues.append(f"{(1 - delivery) * 100:.0f}% of reports lost")
    if rssi is not None and rssi < -80:
        score -= min(25, (-80 - rssi) * 2.5)
        issues.append(f"weak signal ({rssi:.0f} dBm)")
    if age_min > STALE_AFTER_MIN:
        score -= 40
        issues.append(f"silent for {age_min:.0f} min")
    if distinct <= 2:
        score -= 50
        issues.append("CO2 reading not changing (sensor may be stuck)")
    return int(max(0, round(score))), issues


def link_health(store: Store, hours: int = 24) -> list[LinkHealth]:
    df = store.query(f"""
        WITH w AS (
          SELECT * FROM readings
          WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM readings), INTERVAL {hours} HOUR)
        )
        SELECT device_id, ANY_VALUE(room_id) AS room_id, ANY_VALUE(gateway_id) AS gateway_id,
               LOGICAL_AND(simulated) AS simulated,
               SAFE_DIVIDE(SUM(packets_acked), SUM(packets_sent)) AS delivery_ratio,
               AVG(rssi_dbm) AS avg_rssi_dbm,
               TIMESTAMP_DIFF((SELECT MAX(ts) FROM readings), MAX(ts), SECOND) / 60.0 AS minutes_since_seen,
               COUNT(DISTINCT ROUND(co2_ppm)) AS distinct_co2, COUNT(co2_ppm) AS co2_reports, COUNT(*) AS reports
        FROM w GROUP BY device_id
    """)
    out = []
    for r in df.itertuples():
        delivery = float(r.delivery_ratio) if pd.notna(r.delivery_ratio) else 0.0
        camera = is_camera(r.device_id)
        rssi = float(r.avg_rssi_dbm) if pd.notna(r.avg_rssi_dbm) else (None if camera else -100.0)
        # A stuck-sensor verdict needs enough CO2 samples; a node that just joined is not stuck.
        distinct = int(r.distinct_co2) if int(r.co2_reports) >= 12 else 99
        conf, issues = _score(delivery, rssi, float(r.minutes_since_seen), distinct)
        level = "high" if conf >= 80 else "medium" if conf >= 55 else "low"
        out.append(LinkHealth(r.device_id, r.room_id, r.gateway_id, bool(r.simulated), round(delivery, 3),
                              round(rssi, 1) if rssi is not None else None,
                              round(float(r.minutes_since_seen), 1), int(r.distinct_co2),
                              conf, level, issues))
    return sorted(out, key=lambda x: x.confidence)


def room_confidence(health: list[LinkHealth]) -> dict[str, LinkHealth]:
    """Best available device per room (a live real node beats its simulated twin)."""
    best: dict[str, LinkHealth] = {}
    for h in health:
        if is_camera(h.device_id):
            continue  # cameras add a head count; the room's air and energy data still come from its sensor
        cur = best.get(h.room_id)
        fresher_real = not h.simulated and h.minutes_since_seen <= STALE_AFTER_MIN
        if cur is None or fresher_real or (cur.simulated == h.simulated and h.confidence > cur.confidence):
            best[h.room_id] = h
    return best


def gateway_outages(store: Store, days: int = 7) -> pd.DataFrame:
    """Hours in which a whole gateway went quiet while its rooms normally report."""
    return store.query(f"""
        WITH per_hour AS (
          SELECT gateway_id, DATETIME_TRUNC(ts_local, HOUR) AS hour, COUNT(*) AS reports
          FROM readings
          WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM readings), INTERVAL {days} DAY)
          GROUP BY gateway_id, DATETIME_TRUNC(ts_local, HOUR)
        ),
        hours AS (SELECT DISTINCT DATETIME_TRUNC(ts_local, HOUR) AS hour FROM readings
                  WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM readings), INTERVAL {days} DAY)),
        gws AS (SELECT DISTINCT gateway_id FROM readings)
        SELECT g.gateway_id, h.hour, COALESCE(p.reports, 0) AS reports
        FROM gws AS g CROSS JOIN hours AS h
        LEFT JOIN per_hour AS p ON p.gateway_id = g.gateway_id AND p.hour = h.hour
        WHERE COALESCE(p.reports, 0) = 0
        ORDER BY h.hour
    """)

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..campus import ROOMS, TIMETABLE
from ..config import settings

SQL_DIR = Path(__file__).resolve().parents[2] / "bigquery"

READING_COLUMNS = [
    "ts", "ts_local", "weekday", "hour_local", "device_id", "room_id", "gateway_id",
    "people", "occupied", "co2_ppm", "temp_c", "humidity_pct", "watts", "ac_on",
    "rssi_dbm", "packets_sent", "packets_acked", "simulated",
]


def enrich(row: dict) -> dict:
    """Add the local-time columns every view relies on."""
    ts = row["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(settings.timezone)
    out = {c: row.get(c) for c in READING_COLUMNS}
    out.update(ts=ts.astimezone(timezone.utc), ts_local=local.replace(tzinfo=None),
               weekday=local.weekday(), hour_local=local.hour)
    return out


def split_statements(text: str) -> list[str]:
    text = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in text.split(";") if s.strip()]


class Store(ABC):
    """What the rest of the app needs from a warehouse."""

    dialect: str = "bigquery"

    def latest_ts_sql(self) -> str:
        """SQL for 'the newest reading time'. The local store asks the table; BigQuery overrides this
        with a constant, so time filters prune to recent partitions instead of scanning the table."""
        return "(SELECT MAX(ts) FROM readings)"

    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def insert_readings(self, rows: list[dict]) -> int: ...

    @abstractmethod
    def query(self, sql: str) -> pd.DataFrame:
        """Run BigQuery-dialect SQL; table names are written without a dataset prefix."""

    @abstractmethod
    def insert_decision(self, row: dict) -> None: ...

    @abstractmethod
    def reading_count(self) -> int: ...

    @abstractmethod
    def clear_readings(self) -> None:
        """Drop all sensor readings (used when the campus model changes)."""

    def stale_rooms(self) -> bool:
        """True when stored readings belong to rooms that are no longer in the campus model."""
        ids = ", ".join(f"'{r.room_id}'" for r in ROOMS)
        df = self.query(f"SELECT COUNT(*) AS n FROM readings WHERE room_id NOT IN ({ids})")
        return int(df["n"].iloc[0]) > 0

    def static_frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        return (pd.DataFrame([r.as_row() for r in ROOMS]),
                pd.DataFrame([s.as_row() for s in TIMETABLE]))

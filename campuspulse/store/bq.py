"""Cloud backend: Google BigQuery."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

from ..config import settings
from .base import SQL_DIR, Store, enrich, split_statements

TABLES = ("readings", "rooms", "timetable", "decisions", "v_room_hourly", "v_empty_cooled",
          "v_network_daily", "v_slot_attendance", "occupancy_forecast", "energy_model")


INT_COLUMNS = ("weekday", "hour_local", "people", "packets_sent", "packets_acked")


def _json_row(r: dict) -> dict:
    """A reading as JSON BigQuery accepts: ISO times, real ints, null instead of NaN."""
    out = {}
    for k, v in r.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out[k] = None
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif k in INT_COLUMNS:
            out[k] = int(round(float(v)))
        elif hasattr(v, "item"):  # numpy scalar
            out[k] = v.item()
        else:
            out[k] = v
    return out


class BigQueryStore(Store):
    supports_ml = True  # BigQuery ML models (bigquery/ml.sql) can be queried
    def __init__(self):
        self.client = bigquery.Client(project=settings.gcp_project, location=settings.bq_location)
        self.ds = f"{self.client.project}.{settings.bq_dataset}"

    def qualify(self, sql: str) -> str:
        """Prefix bare table names with the dataset so app SQL stays portable."""
        pattern = r"(?<![\w.`])(" + "|".join(TABLES) + r")(?![\w`])"
        return re.sub(pattern, lambda m: f"`{self.ds}.{m.group(1)}`", sql)

    def _run_file(self, name: str) -> None:
        text = (SQL_DIR / name).read_text().replace("-- BigQuery only: ", "")
        text = text.replace("{ds}", f"`{self.ds}`.")
        for stmt in split_statements(text):
            self.client.query(stmt).result()

    def setup(self, ml: bool = False) -> None:
        self.client.create_dataset(bigquery.Dataset(self.ds), exists_ok=True)
        self._run_file("schema.sql")
        rooms, timetable = self.static_frames()
        cfg = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
        self.client.load_table_from_dataframe(rooms, f"{self.ds}.rooms", job_config=cfg).result()
        self.client.load_table_from_dataframe(timetable, f"{self.ds}.timetable", job_config=cfg).result()
        self._run_file("views.sql")
        if ml:
            self._run_file("ml.sql")

    def insert_readings(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        recs = [enrich(r) for r in rows]
        if len(recs) > 500:  # history back-fill: one free load job instead of streaming
            df = pd.DataFrame(recs)
            for c in INT_COLUMNS:  # nullable ints: a camera row has no CO2, a sensor row may have no head count
                df[c] = pd.to_numeric(df[c], errors="coerce").round().astype("Int64")
            cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND",
                                         schema=self.client.get_table(f"{self.ds}.readings").schema)
            self.client.load_table_from_dataframe(df, f"{self.ds}.readings", job_config=cfg).result()
            return len(df)
        errors = self.client.insert_rows_json(f"{self.ds}.readings", [_json_row(r) for r in recs])
        if errors:
            raise RuntimeError(f"BigQuery insert failed: {errors[:3]}")
        return len(recs)

    def latest_ts_sql(self) -> str:
        # Readings are produced up to the current minute (live loop or request catch-up), so "now"
        # stands in for MAX(ts). A constant lets BigQuery prune the readings partitions.
        now = datetime.now(timezone.utc)
        return f"TIMESTAMP '{now:%Y-%m-%d %H:%M:%S}+00'"

    def query(self, sql: str) -> pd.DataFrame:
        job_cfg = bigquery.QueryJobConfig(maximum_bytes_billed=2 * 1024**3)
        return self.client.query(self.qualify(sql), job_config=job_cfg).to_dataframe()

    def insert_decision(self, row: dict) -> None:
        r = dict(row, decided_at=row["decided_at"].isoformat())
        self.client.insert_rows_json(f"{self.ds}.decisions", [r])

    def clear_readings(self) -> None:
        self.client.query(self.qualify("TRUNCATE TABLE readings")).result()

    def reading_count(self) -> int:
        df = self.query("SELECT COUNT(*) AS n FROM readings")
        return int(df["n"].iloc[0])

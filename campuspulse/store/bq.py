"""Cloud backend: Google BigQuery."""
from __future__ import annotations

import re

import pandas as pd
from google.cloud import bigquery

from ..config import settings
from .base import SQL_DIR, Store, enrich, split_statements

TABLES = ("readings", "rooms", "timetable", "decisions", "v_room_hourly", "v_empty_cooled",
          "v_network_daily", "v_slot_attendance", "occupancy_forecast", "energy_model")


class BigQueryStore(Store):
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
        df = pd.DataFrame([enrich(r) for r in rows])
        if len(df) > 500:  # history backfill: one load job instead of streaming
            cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
            self.client.load_table_from_dataframe(df, f"{self.ds}.readings", job_config=cfg).result()
            return len(df)
        payload = []
        for r in df.to_dict("records"):
            r["ts"] = r["ts"].isoformat()
            r["ts_local"] = r["ts_local"].isoformat()
            payload.append(r)
        errors = self.client.insert_rows_json(f"{self.ds}.readings", payload)
        if errors:
            raise RuntimeError(f"BigQuery insert failed: {errors[:3]}")
        return len(payload)

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

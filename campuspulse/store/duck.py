"""Local backend: DuckDB, with the BigQuery SQL transpiled by sqlglot."""
from __future__ import annotations

import threading
from pathlib import Path

import duckdb
import pandas as pd
import sqlglot

from .base import READING_COLUMNS, SQL_DIR, Store, enrich, split_statements


def to_duckdb(sql: str) -> str:
    return ";\n".join(sqlglot.transpile(sql, read="bigquery", write="duckdb"))


class DuckStore(Store):
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(path)
        self.con.execute("SET TimeZone = 'UTC'")
        self.lock = threading.Lock()

    def _run_file(self, name: str) -> None:
        text = (SQL_DIR / name).read_text().replace("{ds}", "")
        for stmt in split_statements(text):
            self.con.execute(to_duckdb(stmt))

    def setup(self) -> None:
        with self.lock:
            self._run_file("schema.sql")
            rooms, timetable = self.static_frames()
            self.con.execute("DELETE FROM rooms")
            self.con.execute("DELETE FROM timetable")
            self.con.register("rooms_df", rooms)
            self.con.register("tt_df", timetable)
            self.con.execute("INSERT INTO rooms BY NAME SELECT * FROM rooms_df")
            self.con.execute("INSERT INTO timetable BY NAME SELECT * FROM tt_df")
            self.con.unregister("rooms_df")
            self.con.unregister("tt_df")
            self._run_file("views.sql")

    def insert_readings(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        df = pd.DataFrame([enrich(r) for r in rows], columns=READING_COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        with self.lock:
            self.con.register("incoming", df)
            self.con.execute("INSERT INTO readings BY NAME SELECT * FROM incoming")
            self.con.unregister("incoming")
        return len(df)

    def query(self, sql: str) -> pd.DataFrame:
        with self.lock:
            return self.con.execute(to_duckdb(sql)).df()

    def insert_decision(self, row: dict) -> None:
        with self.lock:
            self.con.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [row["decided_at"], row["action_id"], row["kind"], row["title"], row["status"],
                 row["kwh_per_week"], row["rm_per_week"], row["decided_by"]],
            )

    def reading_count(self) -> int:
        with self.lock:
            return int(self.con.execute("SELECT COUNT(*) FROM readings").fetchall()[0][0])

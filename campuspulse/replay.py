"""Built-in replay of a published real-measurement dataset, shown as its own site.

The fallback when no board is on stage. The data stays in its own context: an office room in Mons,
Belgium (UCI Occupancy Detection, CC BY 4.0; see data/replay/README.md), shown with its own room,
its own sensors and its own timeline. It is never poured into an APU room, so nothing on the APU
side pretends to be measured. CampusPulse's rules run on it as they would on any room: occupied,
stale air (CO2 over the limit while occupied), and lights bright while nobody is there.

There is no background thread and nothing is stored: the replay position comes from the clock
(row n is on screen from started + n * every), so a scale-to-zero instance replays smoothly.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .config import settings

DATA = Path(__file__).resolve().parent / "data" / "replay" / "uci_occupancy_5min.csv"
DEVICE = "replay-uci-occupancy"
SOURCE = "UCI Occupancy Detection (Candanedo 2016, CC BY 4.0)"
HISTORY_ROWS = 72  # six hours of 5-minute rows on the chart
LIT_LUX = 300      # in this room, lights on read ~450 lux; empty and dark reads 0

SITE = {
    "id": "UCI-OFFICE",
    "name": "Office room, Mons, Belgium",
    "study": "Candanedo & Feldheim (2016), Energy and Buildings 112, 28-39",
    "period": "2-18 February 2015",
    "sensors": ["Temperature (°C)", "Relative humidity (%)", "Light (lux)", "CO2 (ppm)"],
    "ground_truth": "Occupied or not, from a time-stamped photo every minute",
    "resampled": "Published every minute; averaged to 5-minute steps here, CampusPulse's interval",
    "not_in_data": "No head count, AC, power or Wi-Fi in this dataset",
    "licence": "CC BY 4.0",
    "doi": "https://doi.org/10.24432/C5X01N",
}

# Starting points worth showing (dataset time), with what happens next.
PRESETS = {
    "arrival": ("2015-02-09 08:15", "Monday morning: people arrive at 08:40, CO2 passes 1,000 ppm by 09:55"),
    "stale": ("2015-02-09 09:30", "Monday mid-morning: occupied, CO2 crosses 1,000 ppm within a minute"),
    "afternoon": ("2015-02-11 14:15", "Wednesday afternoon: people arrive at 14:45, CO2 jumps over 1,000 ppm"),
}


@lru_cache(maxsize=1)
def _all_rows(path: str = str(DATA)) -> tuple[dict, ...]:
    with open(path, encoding="utf-8") as fh:
        return tuple(csv.DictReader(fh))


def load(start: str | None = None) -> list[dict]:
    rows = list(_all_rows())
    if start:
        rows = [r for r in rows if r["ts"] >= start] + [r for r in rows if r["ts"] < start]
    return rows


def reading(row: dict) -> dict:
    """One dataset row, read through CampusPulse's room rules."""
    occupied = row["occupied"] == "1"
    co2, lux = float(row["co2_ppm"]), float(row["light_lux"])
    if occupied and co2 > settings.co2_limit_ppm:
        status = "stale_air"
    elif occupied:
        status = "in_use"
    elif lux >= LIT_LUX:
        status = "lit_empty"
    else:
        status = "idle"
    return {"ts": row["ts"], "occupied": occupied, "co2_ppm": round(co2), "temp_c": round(float(row["temp_c"]), 1),
            "humidity_pct": round(float(row["humidity_pct"])), "light_lux": round(lux), "status": status}


class Replayer:
    def __init__(self):
        self.lock = threading.Lock()
        self.run: dict | None = None

    def start(self, preset: str = "arrival", every_s: float = 3.0, now: datetime | None = None) -> dict:
        if preset not in PRESETS:
            preset = "arrival"
        start, story = PRESETS[preset]
        with self.lock:
            self.run = {"preset": preset, "story": story, "every_s": max(1.0, float(every_s)),
                        "rows": load(start), "started": now or datetime.now(timezone.utc)}
        return self.status(now)

    def stop(self) -> None:
        with self.lock:
            self.run = None

    def status(self, now: datetime | None = None) -> dict:
        base = {"site": SITE, "source": SOURCE, "device": DEVICE,
                "presets": {k: v[1] for k, v in PRESETS.items()}}
        with self.lock:
            r = self.run
            if r is None:
                return base | {"running": False}
            now = now or datetime.now(timezone.utc)
            idx = max(0, int((now - r["started"]).total_seconds() // r["every_s"]))
            rows, n = r["rows"], len(r["rows"])
            shown = [reading(rows[i % n]) for i in range(max(0, idx - HISTORY_ROWS + 1), idx + 1)]
            so_far = [reading(rows[i % n]) for i in range(max(0, idx - n + 1), idx + 1)]
            step_min = 5
            tally = {"minutes": len(so_far) * step_min,
                     "occupied_min": sum(x["occupied"] for x in so_far) * step_min,
                     "stale_air_min": sum(x["status"] == "stale_air" for x in so_far) * step_min,
                     "lit_empty_min": sum(x["status"] == "lit_empty" for x in so_far) * step_min,
                     "max_co2_ppm": max(x["co2_ppm"] for x in so_far)}
            return base | {"running": True, "preset": r["preset"], "story": r["story"], "every_s": r["every_s"],
                           "rows_shown": idx + 1, "rows_total": n, "reading": shown[-1], "history": shown,
                           "tally": tally, "co2_limit_ppm": settings.co2_limit_ppm}


replayer = Replayer()

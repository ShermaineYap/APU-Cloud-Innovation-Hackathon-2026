"""Built-in replay of a published real-measurement dataset, switched on from the dashboard.

The fallback when no board is on stage: the server itself feeds genuinely measured CO2, temperature,
humidity and occupancy (UCI Occupancy Detection, CC BY 4.0; see data/replay/README.md) into one room,
one 5-minute row every few seconds, through the same ingest path as a sensor node. Rows carry the
device id "replay-uci-occupancy", so the dashboard labels the room "real data, replayed", never "live".

There is no background thread: like the simulator on Cloud Run, rows are produced on demand from the
clock (row n is due at started + n * every), so a scale-to-zero instance with CPU only during
requests still replays smoothly while the dashboard is open.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "replay" / "uci_occupancy_5min.csv"
DEVICE = "replay-uci-occupancy"
SOURCE = "UCI Occupancy Detection (Candanedo 2016, CC BY 4.0): an office in Mons, Belgium, February 2015"
MAX_CATCH_UP = 12  # after a long gap, send only the most recent rows instead of a flood

# Starting points worth showing (dataset time), with what happens next.
PRESETS = {
    "arrival": ("2015-02-09 08:15", "Monday morning: the room fills at 08:40, CO2 climbs past 1,000 ppm by 09:55"),
    "stale": ("2015-02-09 09:30", "Monday mid-morning: occupied, CO2 crosses 1,000 ppm within a minute"),
    "afternoon": ("2015-02-11 14:15", "Wednesday afternoon: people arrive at 14:45 and CO2 jumps straight over 1,000 ppm"),
}


def load(start: str | None = None, path: Path = DATA) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if start:
        rows = [r for r in rows if r["ts"] >= start] + [r for r in rows if r["ts"] < start]
    return rows


def payload(row: dict, room: str, ts: datetime | None = None) -> dict:
    body = {"device_id": DEVICE, "room_id": room, "occupied": row["occupied"] == "1",
            "co2_ppm": round(float(row["co2_ppm"]), 1), "temp_c": round(float(row["temp_c"]), 2),
            "humidity_pct": round(float(row["humidity_pct"]), 1), "packets_sent": 1, "packets_acked": 1}
    if ts is not None:
        body["ts"] = ts.isoformat()
    return body


class Replayer:
    def __init__(self):
        self.lock = threading.Lock()
        self.run: dict | None = None

    def start(self, room: str, preset: str = "arrival", every_s: float = 3.0,
              now: datetime | None = None) -> dict:
        start, story = PRESETS.get(preset, PRESETS["arrival"])
        with self.lock:
            self.run = {"room": room, "preset": preset, "story": story, "every_s": max(1.0, float(every_s)),
                        "rows": load(start), "started": now or datetime.now(timezone.utc), "sent": 0,
                        "last": None}
        return self.status()

    def stop(self) -> None:
        with self.lock:
            self.run = None

    def due(self, now: datetime | None = None) -> list[dict]:
        """Readings that are due by now and not yet sent (as ingest payloads)."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            r = self.run
            if r is None:
                return []
            target = int((now - r["started"]).total_seconds() // r["every_s"]) + 1
            if target <= r["sent"]:
                return []
            first = max(r["sent"], target - MAX_CATCH_UP)
            out = []
            for i in range(first, target):
                row = r["rows"][i % len(r["rows"])]
                ts = min(now, r["started"] + timedelta(seconds=i * r["every_s"]))
                out.append(payload(row, r["room"], ts))
                r["last"] = row
            r["sent"] = target
            return out

    def status(self) -> dict:
        with self.lock:
            r = self.run
            if r is None:
                return {"running": False, "presets": {k: v[1] for k, v in PRESETS.items()}, "source": SOURCE}
            last = r["last"]
            return {"running": True, "room": r["room"], "preset": r["preset"], "story": r["story"],
                    "every_s": r["every_s"], "rows_sent": r["sent"], "rows_total": len(r["rows"]),
                    "device": DEVICE, "source": SOURCE,
                    "presets": {k: v[1] for k, v in PRESETS.items()},
                    "dataset_time": last["ts"] if last else None,
                    "reading": None if last is None else {
                        "co2_ppm": round(float(last["co2_ppm"])), "temp_c": round(float(last["temp_c"]), 1),
                        "humidity_pct": round(float(last["humidity_pct"])), "occupied": last["occupied"] == "1"}}


replayer = Replayer()

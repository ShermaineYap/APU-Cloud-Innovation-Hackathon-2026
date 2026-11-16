"""Replay a published real-measurement dataset through CampusPulse, as a sensor node would.

The fallback when no real board is available: genuine measured CO2, temperature, humidity and
occupancy (UCI Occupancy Detection, CC BY 4.0; see campuspulse/data/replay/README.md) arrive at
/api/ingest every few seconds. The dashboard labels the room "real data, replayed" and the status
bar says so; nothing claims the readings are from APU.

    python scripts/replay_dataset.py                                  # local app on port 8000
    python scripts/replay_dataset.py --room AUD-1 --every 2           # another room, faster
    python scripts/replay_dataset.py --server https://<service>.run.app --key <CP_INGEST_KEY>

Each dataset row is 5 minutes of real time; --every sets how many seconds a row takes on screen.
The default start is a weekday morning, so occupancy and the CO2 climb show up in the first minute.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parents[1] / "campuspulse" / "data" / "replay" / "uci_occupancy_5min.csv"


def load(path: Path, start: str | None) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if start:
        rows = [r for r in rows if r["ts"] >= start] + [r for r in rows if r["ts"] < start]
    return rows


def payload(row: dict, device: str, room: str) -> dict:
    return {"device_id": device, "room_id": room, "occupied": row["occupied"] == "1",
            "co2_ppm": float(row["co2_ppm"]), "temp_c": float(row["temp_c"]),
            "humidity_pct": float(row["humidity_pct"]), "packets_sent": 1, "packets_acked": 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default=os.getenv("CP_SERVER", "http://localhost:8000"))
    ap.add_argument("--key", default=os.getenv("CP_INGEST_KEY", "dev-ingest-key"), help="X-Device-Key")
    ap.add_argument("--room", default=os.getenv("CP_LIVE_ROOM", "B-08-05"))
    ap.add_argument("--every", type=float, default=3.0, help="seconds per dataset row (each row = 5 real minutes)")
    ap.add_argument("--start", default="2015-02-09 07:30", help="dataset time to start from (YYYY-MM-DD HH:MM)")
    ap.add_argument("--count", type=int, default=0, help="stop after N rows (0 = loop forever)")
    ap.add_argument("--file", type=Path, default=DATA)
    args = ap.parse_args()

    rows = load(args.file, args.start)
    device = "replay-uci-occupancy"
    print(f"Replaying {len(rows)} real measurements ({args.file.name}) into {args.room} at {args.server}")
    print("Source: UCI Occupancy Detection (Candanedo 2016), CC BY 4.0. Ctrl+C to stop.\n")
    client = httpx.Client(base_url=args.server, headers={"X-Device-Key": args.key}, timeout=15)
    n = 0
    try:
        while True:
            for row in rows:
                body = payload(row, device, args.room)
                try:
                    r = client.post("/api/ingest", json=body)
                    status = r.status_code
                except httpx.HTTPError as e:
                    status = f"no connection ({type(e).__name__})"
                print(f"{row['ts']}  {'occupied' if body['occupied'] else 'empty   '}  CO2 {body['co2_ppm']:7.1f} ppm  "
                      f"{body['temp_c']:5.2f} C  {body['humidity_pct']:5.1f} %  -> {status}")
                if status == 401:
                    print("The server rejected the key: pass --key <CP_INGEST_KEY>.")
                    return 1
                n += 1
                if args.count and n >= args.count:
                    return 0
                time.sleep(args.every)
    except KeyboardInterrupt:
        print(f"\nStopped after {n} readings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

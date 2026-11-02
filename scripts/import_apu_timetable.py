"""Import one teaching week from APU's public timetable feed.

The feed is the open JSON behind APSpace's timetable view. We keep only what the
demo needs: room, day, start and end time, module, and how many intakes share the
session. Lecturer names and accounts are dropped.

    python scripts/import_apu_timetable.py                  # week starting next Monday, or the latest full week
    python scripts/import_apu_timetable.py --week 2026-11-02

Writes campuspulse/data/apu_timetable.json (committed, so the app runs offline).
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta

FEED = "https://s3-ap-southeast-1.amazonaws.com/open-ws/weektimetable"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "campuspulse", "data",
                   "apu_timetable.json")


def fetch(path: str | None) -> list[dict]:
    if path:
        raw = open(path, "rb").read()
    else:
        with urllib.request.urlopen(FEED, timeout=60) as r:
            raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _minutes(iso: str) -> int:
    t = datetime.fromisoformat(iso)
    return t.hour * 60 + t.minute


def pick_week(rows: list[dict]) -> date:
    """The Monday of the week with the most on-campus sessions."""
    per_week = collections.Counter()
    for r in rows:
        d = date.fromisoformat(r["DATESTAMP_ISO"])
        per_week[d - timedelta(days=d.weekday())] += 1
    return per_week.most_common(1)[0][0]


def build(rows: list[dict], monday: date) -> dict:
    days = {monday + timedelta(days=i) for i in range(7)}
    sessions: dict[tuple, dict] = {}
    for r in rows:
        if r.get("LOCATION") != "APU CAMPUS":
            continue
        d = date.fromisoformat(r["DATESTAMP_ISO"])
        if d not in days:
            continue
        room = re.sub(r"[^\x20-\x7E]", "", r["ROOM"]).strip()
        key = (room, d.weekday(), _minutes(r["TIME_FROM_ISO"]), _minutes(r["TIME_TO_ISO"]))
        s = sessions.setdefault(key, {"modules": collections.Counter(), "intakes": set()})
        code = r["MODID"].split("-")[0]
        s["modules"][f"{code} {r['MODULE_NAME'].strip()}"] += 1
        s["intakes"].add(r["INTAKE"])

    by_room: dict[str, list[dict]] = collections.defaultdict(list)
    for (room, wd, start, end), s in sorted(sessions.items()):
        by_room[room].append({
            "weekday": wd, "start_min": start, "end_min": end,
            "module": s["modules"].most_common(1)[0][0],
            "modules_sharing": len(s["modules"]), "intakes": len(s["intakes"]),
        })
    return {
        "source": FEED,
        "note": "APU public timetable feed. Lecturer names and accounts removed.",
        "week_of": monday.isoformat(),
        "imported": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rooms": dict(sorted(by_room.items())),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--week", help="Monday of the week to import, YYYY-MM-DD")
    ap.add_argument("--file", help="read a saved copy of the feed instead of downloading")
    args = ap.parse_args()
    rows = fetch(args.file)
    monday = date.fromisoformat(args.week) if args.week else pick_week(rows)
    data = build(rows, monday)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    n = sum(len(v) for v in data["rooms"].values())
    print(f"Week of {monday}: {n} sessions in {len(data['rooms'])} rooms -> {os.path.relpath(OUT)}")


if __name__ == "__main__":
    main()

"""The whole campus from APU's public timetable: every room and every class, no sensors.

CampusPulse senses and plans 20 modelled rooms (campus.py). This view covers all of APU's
timetabled rooms for the same real week, so the floor plan sits inside the whole campus:
which rooms have a class now, which are free, how many intakes each class has.

Online sessions (room names starting "ONL") are counted separately: they use no room.
"""
from __future__ import annotations

import re
from datetime import datetime

from .campus import APU_TIMETABLE, ROOMS, WEEK_OF

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BLOCK_ORDER = ["Auditoriums", "Tech Labs", "Block A", "Block B", "Block C", "Block D", "Block E", "Block S",
               "Special rooms"]
MODELLED = {r.apu_room: r.room_id for r in ROOMS}


def block_of(name: str) -> str:
    if name.startswith("Auditorium"):
        return "Auditoriums"
    if name.startswith("Tech Lab"):
        return "Tech Labs"
    m = re.match(r"^([A-Z])-\d", name)
    if m:
        return f"Block {m.group(1)}"
    return "Special rooms"


def short_name(name: str) -> str:
    """'Auditorium 3 @ Block C Level 9' -> 'Auditorium 3'; 'Tech Lab 5-02 Cisco Lab' -> 'Tech Lab 5-02'."""
    name = name.split(" @ ")[0]
    m = re.match(r"^(Tech Lab \d-\d+)", name)
    return m.group(1) if m else re.sub(r"\s*\(.*\)$", "", name)


def _fmt(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _all_rooms() -> dict[str, list[dict]]:
    return {name: s for name, s in APU_TIMETABLE["rooms"].items() if not name.upper().startswith("ONL")}


ROOMS_ALL = _all_rooms()
ONLINE = {name: s for name, s in APU_TIMETABLE["rooms"].items() if name.upper().startswith("ONL")}


def campus_day(at: datetime) -> dict:
    """Every physical room's classes on `at`'s weekday, and what is happening at `at`."""
    wd, now = at.weekday(), at.hour * 60 + at.minute
    blocks: dict[str, list[dict]] = {}
    total = in_class = intakes_now = 0
    for name, sessions in ROOMS_ALL.items():
        today = sorted((s for s in sessions if s["weekday"] == wd), key=lambda s: s["start_min"])
        cur = next((s for s in today if s["start_min"] <= now < s["end_min"]), None)
        nxt = next((s for s in today if s["start_min"] > now), None)
        total += len(today)
        if cur:
            in_class += 1
            intakes_now += cur.get("intakes", 1)
        blocks.setdefault(block_of(name), []).append({
            "name": short_name(name), "full_name": name, "modelled": MODELLED.get(name),
            "status": "in_class" if cur else ("free" if today else "no_classes"),
            "now": {"module": cur["module"], "time": f"{_fmt(cur['start_min'])}–{_fmt(cur['end_min'])}",
                    "intakes": cur.get("intakes", 1)} if cur else None,
            "next": {"module": nxt["module"], "time": _fmt(nxt["start_min"])} if nxt else None,
            "classes": [{"time": f"{_fmt(s['start_min'])}–{_fmt(s['end_min'])}", "module": s["module"],
                         "intakes": s.get("intakes", 1)} for s in today],
        })
    out = []
    for b in sorted(blocks, key=lambda b: BLOCK_ORDER.index(b) if b in BLOCK_ORDER else 99):
        rooms = sorted(blocks[b], key=lambda r: r["name"])
        out.append({"block": b, "rooms": rooms, "in_class": sum(r["status"] == "in_class" for r in rooms),
                    "classes": sum(len(r["classes"]) for r in rooms)})
    online_today = sum(1 for s in ONLINE.values() for x in s if x["weekday"] == wd)
    return {"at": at.isoformat(timespec="minutes"), "weekday": DAYS[wd], "week_of": WEEK_OF,
            "rooms": len(ROOMS_ALL), "classes_today": total, "rooms_in_class": in_class,
            "rooms_free": len(ROOMS_ALL) - in_class, "intakes_in_class": intakes_now,
            "modelled": len(MODELLED), "online_classes_today": online_today,
            "classes_week": sum(len(s) for s in ROOMS_ALL.values()), "blocks": out,
            "source": "APU public timetable feed (APSpace), lecturer data removed"}

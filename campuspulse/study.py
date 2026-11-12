"""Where can I study? Free rooms for students, best first.

A room qualifies when no class is booked in it for at least the next 30 minutes,
its sensor is reporting, and nobody's class is running. Rooms that are already
being cooled while empty come first: studying there uses cooling the campus is
paying for anyway, so the student-facing view and the energy story point the
same way. Then fresher air, cooler rooms and longer free time.
"""
from __future__ import annotations

from datetime import datetime

from .campus import TIMETABLE
from .config import settings

OPEN_FROM, OPEN_UNTIL = 7.5, 21.0     # teaching building hours (local time)
MIN_FREE_MIN = 30


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def free_rooms(states: list[dict], at: datetime, limit: int = 6) -> dict:
    """`states` from analytics.room_states (same moment); `at` is local wall-clock time."""
    wd, now_min = at.weekday(), at.hour * 60 + at.minute
    close_min = int(OPEN_UNTIL * 60)
    if wd >= 5 or not OPEN_FROM * 60 <= now_min < close_min:
        return {"at": at.isoformat(), "open": False, "rooms": [],
                "message": "Teaching blocks are closed now (open 07:30–21:00 on weekdays)."}
    out = []
    for r in states:
        if r.get("status") in ("offline", "in_use", "stale_air"):
            continue
        slots = sorted((s for s in TIMETABLE if s.room_id == r["room_id"] and s.weekday == wd),
                       key=lambda s: s.start_min)
        if any(s.start_min <= now_min < s.end_min for s in slots):
            continue  # booked now, even if the class has not turned up
        nxt = next((s for s in slots if s.start_min > now_min), None)
        until = min(nxt.start_min, close_min) if nxt else close_min
        free_min = until - now_min
        if free_min < MIN_FREE_MIN:
            continue
        cooled = bool(r.get("ac_on"))
        co2, temp = r.get("co2_ppm") or 0, r.get("temp_c") or 0
        reasons = []
        if cooled:
            reasons.append("already cooled, so studying here costs no extra energy")
        reasons.append("fresh air" if co2 < 800 else "air getting stuffy")
        reasons.append(f"{temp:.0f}°C")
        score = (100 if cooled else 0) + max(0, 1000 - co2) / 20 + max(0, 30 - temp) * 3 + min(free_min, 180) / 6
        out.append({
            "room_id": r["room_id"], "name": r["name"], "block": r["block"], "floor": r["floor"],
            "seats": r["capacity"], "free_until": _fmt(until),
            "free_until_why": f"next class {nxt.module}" if nxt and until == nxt.start_min else "building closes",
            "free_minutes": free_min, "cooled": cooled, "co2_ppm": round(co2), "temp_c": round(temp, 1),
            "reasons": reasons, "score": round(score, 1),
        })
    out.sort(key=lambda x: -x["score"])
    cooled_n = sum(x["cooled"] for x in out)
    msg = (f"{len(out)} free room{'s' if len(out) != 1 else ''} for at least {MIN_FREE_MIN} minutes"
           + (f"; {cooled_n} already cooled" if cooled_n else "") + ".") if out else \
        "No room is free for the next 30 minutes. Try again after the current classes."
    return {"at": at.isoformat(), "open": True, "rooms": out[:limit], "total_free": len(out), "message": msg,
            "tz": str(settings.timezone)}

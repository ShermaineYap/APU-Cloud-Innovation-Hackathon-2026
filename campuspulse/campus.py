"""The demo campus: twenty real APU rooms and their real teaching week.

Real, from APU's public timetable feed (campuspulse/data/apu_timetable.json,
refreshed with scripts/import_apu_timetable.py):
  * room names and codes (B-08-05 is Block B, level 8, room 05)
  * which module is taught in each room, on which day, from when to when
  * how many intakes share each session

Assumed, because APU does not publish them:
  * seat capacity and air-conditioning load of each room
  * class size, estimated as STUDENTS_PER_INTAKE per intake sharing the session,
    capped at the room's seats

Sensor readings are simulated (see simulator.py) except the one live ESP32 node.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

LIVE_ROOM = os.getenv("CP_LIVE_ROOM", "B-08-05")  # the room wired with the real ESP32 node
STUDENTS_PER_INTAKE = 12  # assumption: average students per intake attending a shared session

_DATA = os.path.join(os.path.dirname(__file__), "data", "apu_timetable.json")
with open(_DATA, encoding="utf-8") as _fh:
    APU_TIMETABLE = json.load(_fh)
WEEK_OF: str = APU_TIMETABLE["week_of"]


@dataclass(frozen=True)
class Room:
    room_id: str
    name: str
    block: str
    floor: int
    kind: str  # lecture | tutorial | lab
    capacity: int  # assumed
    ac_kw: float  # assumed nominal cooling electrical load
    gateway_id: str
    col: int  # position on the floor-plan grid
    row: int
    apu_room: str = field(default="", compare=False)  # exact name in APU's timetable

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("apu_room")
        return d


BLOCK_NAMES = {"AUD": "Auditoriums", "B": "Block B · Classrooms", "TL": "Tech Labs"}
BLOCK_LABEL = {"AUD": "auditorium", "B": "Block B", "TL": "Tech Lab"}  # used in sentences

# (room_id, display name, block, floor, kind, seats, AC kW, APU timetable name)
_SPEC = [
    ("AUD-1", "Auditorium 1 · Level 7", "AUD", 7, "lecture", 200, 16.0, "Auditorium 1 @ Level 7"),
    ("AUD-2", "Auditorium 2 · Level 6", "AUD", 6, "lecture", 160, 13.5, "Auditorium 2 @ Level 6"),
    ("AUD-3", "Auditorium 3 · Block C L9", "AUD", 9, "lecture", 180, 15.0, "Auditorium 3 @ Block C Level 9"),
    ("AUD-4", "Auditorium 4 · Level 3", "AUD", 3, "lecture", 120, 10.5, "Auditorium 4 @ Level 3"),
    ("AUD-5", "Auditorium 5 · Level 3", "AUD", 3, "lecture", 120, 10.5, "Auditorium 5 @ Level 3"),
    ("AUD-6", "Auditorium 6 · Block C L3", "AUD", 3, "lecture", 160, 13.5, "Auditorium 6 @ Block C Level 3"),
    ("B-06-03", "Classroom B-06-03", "B", 6, "tutorial", 50, 4.5, "B-06-03"),
    ("B-06-10", "Classroom B-06-10", "B", 6, "tutorial", 50, 4.5, "B-06-10"),
    ("B-07-02", "Classroom B-07-02", "B", 7, "tutorial", 50, 4.5, "B-07-02"),
    ("B-07-04", "Classroom B-07-04", "B", 7, "tutorial", 50, 4.5, "B-07-04"),
    ("B-08-03", "Classroom B-08-03", "B", 8, "tutorial", 50, 4.5, "B-08-03"),
    ("B-08-05", "Classroom B-08-05", "B", 8, "tutorial", 50, 4.5, "B-08-05"),
    ("B-08-09", "Classroom B-08-09", "B", 8, "tutorial", 50, 4.5, "B-08-09"),
    ("B-09-06", "Classroom B-09-06", "B", 9, "tutorial", 50, 4.5, "B-09-06"),
    ("TL4-02", "Tech Lab 4-02", "TL", 4, "lab", 40, 5.0, "Tech Lab 4-02"),
    ("TL4-07", "Tech Lab 4-07", "TL", 4, "lab", 40, 5.0, "Tech Lab 4-07"),
    ("TL6-02", "Tech Lab 6-02", "TL", 6, "lab", 40, 5.0, "Tech Lab 6-02"),
    ("TL6-11", "Tech Lab 6-11", "TL", 6, "lab", 40, 5.0, "Tech Lab 6-11"),
    ("TL6-14", "Tech Lab 6-14", "TL", 6, "lab", 40, 5.0, "Tech Lab 6-14"),
    ("TL7-01", "Tech Lab 7-01", "TL", 7, "lab", 40, 5.0, "Tech Lab 7-01"),
]


def _overrides() -> dict[str, dict]:
    """Optional real figures from Facilities: campuspulse/data/room_overrides.csv
    with columns room_id,capacity,ac_kw (either value may be blank)."""
    path = os.getenv("CP_ROOM_OVERRIDES", os.path.join(os.path.dirname(__file__), "data", "room_overrides.csv"))
    if not os.path.isfile(path):
        return {}
    import csv
    with open(path, encoding="utf-8") as fh:
        return {r["room_id"].strip(): r for r in csv.DictReader(fh) if r.get("room_id")}


def _rooms() -> list[Room]:
    out, pos, real = [], {}, _overrides()
    for rid, name, block, floor, kind, cap, ac, apu in _SPEC:
        o = real.get(rid, {})
        cap = int(o["capacity"]) if (o.get("capacity") or "").strip() else cap
        ac = float(o["ac_kw"]) if (o.get("ac_kw") or "").strip() else ac
        i = pos.get(block, 0)
        pos[block] = i + 1
        out.append(Room(rid, name, block, floor, kind, cap, ac, f"gw-{block.lower()}", i % 4, i // 4, apu))
    return out


ROOMS: list[Room] = _rooms()
ROOM_BY_ID = {r.room_id: r for r in ROOMS}


@dataclass(frozen=True)
class Slot:
    room_id: str
    weekday: int  # 0 = Monday
    start_hour: int  # nearest hour to the start (08:30 -> 9, 10:45 -> 11)
    end_hour: int  # nearest hour to the end, exclusive (10:30 -> 11, 12:45 -> 13)
    module: str
    expected: int  # estimated students (see STUDENTS_PER_INTAKE)
    start_min: int = field(default=0, compare=False)  # exact times, minutes after midnight
    end_min: int = field(default=0, compare=False)
    intakes: int = field(default=1, compare=False)

    def as_row(self) -> dict:
        return {k: getattr(self, k) for k in ("room_id", "weekday", "start_hour", "end_hour", "module", "expected")}

    @property
    def time_label(self) -> str:
        f = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
        return f"{f(self.start_min)}–{f(self.end_min)}"


def _hour(minutes: int) -> int:
    return int(minutes / 60 + 0.5)


def _timetable() -> list[Slot]:
    slots = []
    for room in ROOMS:
        for s in APU_TIMETABLE["rooms"].get(room.apu_room, []):
            expected = min(room.capacity, max(8, s["intakes"] * STUDENTS_PER_INTAKE))
            start, end = _hour(s["start_min"]), max(_hour(s["start_min"]) + 1, _hour(s["end_min"]))
            slots.append(Slot(room.room_id, s["weekday"], start, end,
                              s["module"], expected, s["start_min"], s["end_min"], s["intakes"]))
    return slots


TIMETABLE: list[Slot] = _timetable()


def slots_for(room_id: str, weekday: int) -> list[Slot]:
    return [x for x in TIMETABLE if x.room_id == room_id and x.weekday == weekday]

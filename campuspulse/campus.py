"""The demo campus: three blocks, twenty rooms, one weekly timetable.

Room ids, capacities and AC loads are illustrative, not APU's real estate data.
Swap this module for a CSV export from the timetabling office to go live.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

LIVE_ROOM = "B-03"  # the room wired with the real ESP32 node


@dataclass(frozen=True)
class Room:
    room_id: str
    name: str
    block: str
    floor: int
    kind: str  # lecture | lab | tutorial | study
    capacity: int
    ac_kw: float  # nominal cooling electrical load
    gateway_id: str
    col: int  # position on the floor-plan grid
    row: int

    def as_row(self) -> dict:
        return asdict(self)


def _r(room_id, name, kind, cap, ac, col, row, floor=1):
    block = room_id[0]
    return Room(room_id, name, block, floor, kind, cap, ac, f"gw-{block.lower()}", col, row)


ROOMS: list[Room] = [
    # Block A: big lecture theatres
    _r("A-01", "Auditorium 1", "lecture", 220, 18.0, 0, 0),
    _r("A-02", "Auditorium 2", "lecture", 180, 15.0, 2, 0),
    _r("A-03", "Lecture Hall 3", "lecture", 120, 10.5, 0, 1),
    _r("A-04", "Lecture Hall 4", "lecture", 120, 10.5, 1, 1),
    _r("A-05", "Lecture Hall 5", "lecture", 90, 8.0, 2, 1),
    _r("A-06", "Lecture Hall 6", "lecture", 90, 8.0, 3, 1),
    # Block B: tutorial rooms and labs
    _r("B-01", "Tutorial Room 1", "tutorial", 40, 3.5, 0, 0),
    _r("B-02", "Tutorial Room 2", "tutorial", 40, 3.5, 1, 0),
    _r("B-03", "Tutorial Room 3", "tutorial", 40, 3.5, 2, 0),
    _r("B-04", "Tutorial Room 4", "tutorial", 30, 3.0, 3, 0),
    _r("B-05", "Networking Lab", "lab", 36, 5.5, 0, 1),
    _r("B-06", "IoT Lab", "lab", 30, 5.0, 1, 1),
    _r("B-07", "Cloud Lab", "lab", 36, 5.5, 2, 1),
    _r("B-08", "Data Lab", "lab", 36, 5.5, 3, 1),
    # Block C: study spaces
    _r("C-01", "Library Study Zone", "study", 80, 7.0, 0, 0),
    _r("C-02", "Discussion Room 1", "study", 12, 1.5, 1, 0),
    _r("C-03", "Discussion Room 2", "study", 12, 1.5, 2, 0),
    _r("C-04", "Discussion Room 3", "study", 12, 1.5, 3, 0),
    _r("C-05", "Quiet Room", "study", 24, 2.5, 0, 1),
    _r("C-06", "Postgrad Room", "study", 20, 2.0, 1, 1),
]

ROOM_BY_ID = {r.room_id: r for r in ROOMS}

BLOCK_NAMES = {"A": "Block A · Lecture theatres", "B": "Block B · Tutorials and labs", "C": "Block C · Study spaces"}


@dataclass(frozen=True)
class Slot:
    room_id: str
    weekday: int  # 0 = Monday
    start_hour: int
    end_hour: int
    module: str
    expected: int

    def as_row(self) -> dict:
        return asdict(self)


def _timetable() -> list[Slot]:
    """A plausible teaching week with a few deliberate mismatches.

    The mismatches are what the decision engine should find:
      * CT118 is booked into Auditorium 1 (220 seats) for 60 students.
      * Friday afternoon lectures in Block A are thinly attended.
      * DSA tutorials in B-01 are oversubscribed for a 40-seat room.
    """
    s: list[Slot] = []
    add = lambda *a: s.append(Slot(*a))  # noqa: E731
    for d in range(5):
        add("A-01", d, 9, 11, "CT046 Cloud Infrastructure", 190)
        add("A-01", d, 14, 16, "CT118 Optimisation and Deep Learning", 60)
        add("A-02", d, 10, 12, "CT124 Network Security", 150)
        add("A-03", d, 8, 10, "CT088 Data Management", 100)
        add("A-03", d, 13, 15, "CT112 IoT Systems", 95)
        add("A-04", d, 11, 13, "CT097 Software Engineering", 110)
        add("A-05", d, 9, 11, "CT069 Statistics", 70)
        add("A-06", d, 15, 17, "CT075 Business Analytics", 80)
        add("B-01", d, 10, 12, "CT050 DSA Tutorial", 52)
        add("B-02", d, 14, 16, "CT046 Cloud Tutorial", 32)
        add("B-03", d, 9, 11, "CT112 IoT Tutorial", 34)
        add("B-03", d, 15, 17, "CT124 Security Tutorial", 30)
        add("B-04", d, 11, 13, "CT069 Statistics Tutorial", 26)
        add("B-05", d, 13, 16, "CT124 Networking Practical", 32)
        add("B-06", d, 9, 12, "CT112 IoT Practical", 28)
        add("B-07", d, 14, 17, "CT046 Cloud Practical", 34)
        add("B-08", d, 10, 13, "CT088 Data Practical", 30)
    return s


TIMETABLE: list[Slot] = _timetable()


def slots_for(room_id: str, weekday: int) -> list[Slot]:
    return [x for x in TIMETABLE if x.room_id == room_id and x.weekday == weekday]

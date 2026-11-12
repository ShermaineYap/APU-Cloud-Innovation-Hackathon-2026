"""Synthetic sensor fleet for the 20 real APU rooms in campus.py.

Every row is labelled simulated=True. Only rows posted by the physical ESP32
node carry simulated=False. The physics is deliberately simple but grounded:

  CO2   single-zone mass balance: dC/dt = people * G / V - ACH * (C - outdoor)
  temp  first-order approach to a set point, plus body heat
  power AC load * duty cycle + lighting + plug load

Classes follow APU's real timetable for the week (exact start and end times).
What emerges from that timetable, not planted:
  * rooms cooled on the building schedule between and after classes
  * small classes booked into auditoriums
  * many intakes sharing a classroom, pushing CO2 above 1000 ppm

Planted problems (the decision engine must find them on its own):
  * Friday afternoon auditorium lectures with thin attendance
  * Auditorium 2 AC left running over the last two weekends
  * Tech Lab 7-01 node on a weak link (high packet loss)
  * Tech Lab gateway outage for four hours, five days ago
  * Tech Lab 4-02 CO2 sensor stuck for the last three days
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from .campus import ROOMS, Room, slots_for
from .config import settings

OUTDOOR_CO2 = 420.0
CO2_PER_PERSON_PPM_M3_H = 18_000.0  # 0.018 m3/h exhaled CO2, times 1e6 ppm
STEP = timedelta(minutes=5)

WEAK_LINK_ROOM = "TL7-01"
STUCK_SENSOR_ROOM = "TL4-02"
WEEKEND_AC_ROOM = "AUD-2"
OUTAGE_GATEWAY = "gw-tl"


@dataclass
class RoomState:
    co2: float = OUTDOOR_CO2
    temp: float = 28.5
    attendance: dict | None = None  # (date, start_hour) -> factor


def _volume(room: Room) -> float:
    return room.capacity * 8.0


def _rssi_base(room: Room) -> float:
    if room.room_id == WEAK_LINK_ROOM:
        return -88.0
    return -52.0 - (room.col + room.row) * 4.5


class Fleet:
    """Steps every simulated room forward in time."""

    def __init__(self, seed: int = 7, end: datetime | None = None):
        self.rng = np.random.default_rng(seed)
        self.state = {r.room_id: RoomState(attendance={}) for r in ROOMS}
        self.end = end or datetime.now(timezone.utc)
        self.history_start = self.end - timedelta(days=settings.history_days)
        # room_id -> AC mode set by approved changes (see control.py); shared with the control plane.
        self.policies: dict[str, str] = {}

    # -- behaviour ---------------------------------------------------------
    def _people(self, room: Room, local: datetime, st: RoomState) -> int:
        wd, h = local.weekday(), local.hour + local.minute / 60
        if room.kind == "study":
            if not 8 <= h < 22:
                return 0
            peak = math.exp(-((h - 15.5) ** 2) / 10.0)
            load = (0.15 + 0.7 * peak) * (0.45 if wd >= 5 else 1.0)
            return int(min(room.capacity, self.rng.poisson(room.capacity * load)))
        for slot in slots_for(room.room_id, wd):
            if slot.start_min / 60 <= h < slot.end_min / 60:
                key = (local.date(), slot.start_hour)
                if key not in st.attendance:
                    f = float(np.clip(self.rng.normal(0.84, 0.07), 0.55, 1.0))
                    if self.rng.random() < 0.07:
                        f = 0.0  # cancelled class, room booked and cooled anyway
                    if wd == 4 and slot.start_hour >= 13 and room.block == "AUD":
                        f *= 0.45  # Friday afternoon effect
                    st.attendance[key] = f
                n = slot.expected * st.attendance[key]
                if h - slot.start_min / 60 < 1 / 6:
                    n *= 0.6  # people still arriving
                return int(max(0, self.rng.normal(n, max(1.0, n * 0.04))))
        return 0

    def _ac_on(self, room: Room, local: datetime, people: int = 0) -> bool:
        wd, h = local.weekday(), local.hour + local.minute / 60
        mode = self.policies.get(room.room_id, "schedule")
        if mode == "occupancy":
            # Cool while people are in, while a class is booked, and 1 hour before it starts.
            return people > 0 or any(s.start_min / 60 - 1 <= h < s.end_min / 60 for s in slots_for(room.room_id, wd))
        if mode == "after_hours_off" and (wd >= 5 or not 7.5 <= h < 21):
            return False
        days_ago = (self.end.astimezone(settings.timezone).date() - local.date()).days
        if room.room_id == WEEKEND_AC_ROOM and wd >= 5 and days_ago <= 9:
            return True  # left on by mistake
        if room.kind == "study":
            return 8 <= h < 22
        # Fixed building schedule: cooled all teaching hours, booked or not.
        return wd < 5 and 7.5 <= h < 21

    # -- one row -----------------------------------------------------------
    def step(self, room: Room, ts: datetime, dt_h: float) -> dict | None:
        st = self.state[room.room_id]
        local = ts.astimezone(settings.timezone)
        people = self._people(room, local, st)
        ac = self._ac_on(room, local, people)

        vol = _volume(room)
        ach = 3.0 if ac else 0.6
        st.co2 += dt_h * (people * CO2_PER_PERSON_PPM_M3_H / vol - ach * (st.co2 - OUTDOOR_CO2))
        st.co2 = max(OUTDOOR_CO2 - 5, st.co2)
        target = (23.0 if ac else 30.5) + 2.5 * people / room.capacity
        st.temp += (1 - math.exp(-1.4 * dt_h)) * (target - st.temp)

        load = people / room.capacity
        watts = 60.0 + (room.capacity * 4.0 if (people or ac) else 0.0)
        if ac:
            watts += room.ac_kw * 1000 * float(np.clip(0.55 + 0.4 * load + self.rng.normal(0, 0.03), 0.4, 1.0))

        co2 = st.co2 + self.rng.normal(0, 8)
        days_ago = (self.end - ts).total_seconds() / 86400
        if room.room_id == STUCK_SENSOR_ROOM and days_ago <= 3:
            co2 = 612.0  # frozen sensor: same value forever

        # Network: lost reports never arrive, which is itself a signal.
        if room.gateway_id == OUTAGE_GATEWAY and 4.9 <= days_ago <= 5.1:
            return None
        rssi = _rssi_base(room) + self.rng.normal(0, 2.5)
        loss = 0.22 if room.room_id == WEAK_LINK_ROOM else max(0.0, (-rssi - 70) / 60)
        sent = 10
        acked = int(self.rng.binomial(sent, 1 - min(loss, 0.9)))
        if acked == 0:
            return None

        return {
            "ts": ts,
            "device_id": f"sim-{room.room_id.lower()}",
            "room_id": room.room_id,
            "gateway_id": room.gateway_id,
            "people": people,
            "occupied": people > 0,
            "co2_ppm": round(float(co2), 1),
            "temp_c": round(float(st.temp + self.rng.normal(0, 0.15)), 2),
            "humidity_pct": round(float((55 if ac else 72) + self.rng.normal(0, 2)), 1),
            "watts": round(float(watts), 1),
            "ac_on": ac,
            "rssi_dbm": round(float(rssi), 1),
            "packets_sent": sent,
            "packets_acked": acked,
            "simulated": True,
        }

    # -- batches -----------------------------------------------------------
    def history(self) -> list[dict]:
        rows: list[dict] = []
        start = self.history_start.replace(second=0, microsecond=0)
        start -= timedelta(minutes=start.minute % 5)
        ts = start
        dt_h = STEP.total_seconds() / 3600
        while ts <= self.end:
            for room in ROOMS:
                row = self.step(room, ts, dt_h)
                if row:
                    rows.append(row)
            ts += STEP
        self.last_ts = ts - STEP
        return rows

    def tick(self, now: datetime) -> list[dict]:
        """Rows for the live stream, one per room, at wall-clock time."""
        dt_h = max(1 / 120, (now - getattr(self, "last_ts", now - STEP)).total_seconds() / 3600)
        self.last_ts = now
        self.end = now
        return [row for room in ROOMS if (row := self.step(room, now, dt_h))]

"""Autopilot: CampusPulse plans the next teaching day and decides, not just advises.

  1. Predict   each class's head count from the sensors (same class, past weeks)
               and the hourly outdoor temperature from the real KL forecast.
  2. Optimise  re-assign every class to a room with a mixed-integer program
               (scipy / HiGHS): least cooling energy, every class fits with headroom,
               no double booking, labs stay labs, a cost for every move.
  3. Schedule  each room's AC: on only for its classes, with a pre-cool that is
               longer on hotter mornings, off otherwise.
  4. Guard     CO2 must stay under the limit; trusted, small, same-block changes are
               applied automatically, risky ones go to a person.
  5. Prove     a digital twin (the simulator's physics) runs the day three ways:
               today's setup, AC schedule only, and the full plan.
  6. Act       approval sends the AC modes to the rooms (control.py) and drafts notices.
  7. Learn     the forecast is checked against last week's measured attendance.

People set goals in plain English ("don't move Friday classes"); Gemini turns them
into the constraints below. The solver decides. No figure comes from the language model.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .campus import ROOM_BY_ID, ROOMS, TIMETABLE, Slot
from .config import settings
from .network import link_health, room_confidence
from .store import Store
from .weather import weather

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CO2_GEN = 18_000.0         # ppm·m³/h per person (as the simulator)
ACH_COOLED = 3.0           # air changes per hour with the AC running
TURNOVER_MIN = 15          # gap between two classes in one room
OPEN_MIN, CLOSE_MIN = 7 * 60 + 30, 21 * 60   # the building schedule the AC follows today


@dataclass
class Constraints:
    no_moves: bool = False
    same_block: bool = False   # cross-block moves allowed, but they cost more and go to a person
    max_moves: int | None = None
    headroom_pct: int = 15
    frozen_modules: list[str] = field(default_factory=list)   # substrings of module codes or names
    frozen_rooms: list[str] = field(default_factory=list)     # classes in these rooms stay put
    frozen_days: list[str] = field(default_factory=list)      # weekdays (Mon..Fri) whose classes stay put
    comfort_first: bool = False
    notes: list[str] = field(default_factory=list)            # what was understood, for the UI

    @classmethod
    def from_dict(cls, d: dict | None) -> "Constraints":
        d = dict(d or {})
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and d[k] is not None}
        c = cls(**known)
        c.headroom_pct = int(min(60, max(0, c.headroom_pct)))
        days = {d.lower(): d for d in DAYS}
        for r in list(c.frozen_rooms):           # a model may put a weekday in the wrong list
            if r[:3].lower() in days:
                c.frozen_days.append(r)
        c.frozen_days = sorted({days[d[:3].lower()] for d in c.frozen_days if d[:3].lower() in days})
        c.frozen_rooms = [r.upper() for r in c.frozen_rooms if r.upper() in ROOM_BY_ID]
        if c.max_moves is not None:
            c.max_moves = max(0, int(c.max_moves))
        return c

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# --- plain-English goals without Gemini ------------------------------------------

def parse_instructions_offline(text: str) -> Constraints:
    """A small rule-based reader for the demo's typical goals (Gemini does the general case)."""
    t = (text or "").lower()
    c, notes = Constraints(), []
    for name in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        if re.search(r"\b" + name + r"s?\b", t) and re.search(r"don'?t|do not|\bno\b|keep|leave|freeze", t):
            c.frozen_days.append(name[:3].title())
            notes.append(f"no moves on {name.title()}")
    if (re.search(r"\b(don'?t|do not|no)\b.*\b(move|moves|moving|relocat)", t)
            and not re.search(r"\b(friday|monday|tuesday|wednesday|thursday|module|room)\b|\b[a-z]{2,5}\d{3}", t)):
        c.no_moves, notes = True, notes + ["no room moves"]
    if re.search(r"same block|own block|stay in (?:their|the same|its) block|walk|nearby", t):
        c.same_block, notes = True, notes + ["moves stay within the same block"]
    m = re.search(r"(?:at most|max(?:imum)?|no more than|up to)\s+(\d+)\s+moves?", t)
    if m:
        c.max_moves, notes = int(m.group(1)), notes + [f"at most {m.group(1)} moves"]
    m = re.search(r"(\d+)\s*%\s*(?:head ?room|spare|buffer)", t)
    if m:
        c.headroom_pct, notes = int(m.group(1)), notes + [f"{m.group(1)}% spare seats"]
    if re.search(r"comfort|cooler|students first|air quality", t):
        c.comfort_first, notes = True, notes + ["comfort first: longer pre-cool, more spare seats"]
    for code in re.findall(r"\b([a-z]{2,5}\d{3,4}[a-z]?)\b", t):
        c.frozen_modules.append(code.upper())
        notes.append(f"keep {code.upper()} where it is")
    for rid in ROOM_BY_ID:
        if rid.lower() in t and re.search(r"keep|leave|don'?t|do not|freeze", t):
            c.frozen_rooms.append(rid)
            notes.append(f"leave classes in {rid}")
    if re.search(r"\b(lab|labs)\b.*\b(keep|leave|stay|don'?t|do not)\b|\b(keep|leave|don'?t|do not)\b.*\b(lab|labs)\b", t):
        c.frozen_rooms += [r.room_id for r in ROOMS if r.kind == "lab" and r.room_id not in c.frozen_rooms]
        notes.append("leave the Tech Lab classes in place")
    if re.search(r"auditorium", t) and re.search(r"keep|leave|stay|don'?t|do not", t):
        c.frozen_rooms += [r.room_id for r in ROOMS if r.block == "AUD" and r.room_id not in c.frozen_rooms]
        notes.append("leave the auditorium classes in place")
    c.notes = notes or (["no special goals found; using the defaults"] if t.strip() else [])
    return c


# --- 1. predict ------------------------------------------------------------------

def next_teaching_day(now_local: datetime | None = None) -> date:
    d = (now_local or datetime.now(settings.timezone)).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class History:
    """Measured peak head count of every class session, indexed by (room, weekday, start hour)."""

    def __init__(self, store: Store):
        df = store.query("""SELECT room_id, weekday, start_hour, day, max_people FROM v_slot_attendance""")
        df["day"] = pd.to_datetime(df["day"]).dt.date
        df = df[df["max_people"].notna() & (df["max_people"] > 0)]  # a cancelled class says nothing about size
        self.df = df
        self.by_key = {k: g[["day", "max_people"]].to_numpy() for k, g in df.groupby(["room_id", "weekday", "start_hour"])}

    def values(self, slot: Slot, before: date | None = None) -> np.ndarray:
        a = self.by_key.get((slot.room_id, slot.weekday, slot.start_hour))
        if a is None:
            return np.array([])
        if before is not None:
            a = a[a[:, 0] < before]
        return a[:, 1].astype(float)


def _history(store: Store) -> History:
    return History(store)


def _predict(slot: Slot, hist: History, before: date | None = None) -> tuple[float, float, int]:
    """(median, 90th percentile, weeks of evidence) of the class's measured peak head count."""
    v = hist.values(slot, before)
    if len(v) == 0:
        return float(slot.expected), float(slot.expected), 0
    return float(np.median(v)), float(np.percentile(v, 90)), int(len(v))


# --- physics shared with the simulator -----------------------------------------

def _duty(load: float, outdoor: float) -> float:
    return float(np.clip(0.55 + 0.4 * load + 0.035 * (outdoor - 30.0), 0.35, 1.0))


def _steady_co2(people: float, capacity: int) -> float:
    return 420.0 + people * CO2_GEN / (capacity * 8.0 * ACH_COOLED)


def _precool_min(outdoor: float, comfort_first: bool) -> int:
    if comfort_first:
        return 60
    return int(np.clip(30 + (outdoor - 28.0) * 10, 30, 60))


# --- 2. optimise -----------------------------------------------------------------

def _compatible(orig_kind: str, room_kind: str) -> bool:
    if orig_kind == "lab":
        return room_kind == "lab"
    return room_kind in ("lecture", "tutorial")


def _optimise(classes: list[dict], c: Constraints, profile: list[float]) -> tuple[list[str], dict]:
    from scipy.optimize import Bounds, LinearConstraint, milp

    rooms = ROOMS
    R, C = len(rooms), len(classes)
    ridx = {r.room_id: i for i, r in enumerate(rooms)}
    cost = np.full((C, R), np.inf)
    co2_pen = 60.0 if c.comfort_first else 25.0
    for i, k in enumerate(classes):
        orig = ROOM_BY_ID[k["room_id"]]
        frozen = (c.no_moves or k["room_id"] in c.frozen_rooms
                  or any(f.lower() in k["module"].lower() for f in c.frozen_modules))
        need = math.ceil(k["p90"] * (1 + c.headroom_pct / 100))
        mid = (k["start_min"] + k["end_min"]) / 2 / 60
        outdoor = profile[min(23, int(mid))]
        for j, r in enumerate(rooms):
            is_orig = r.room_id == k["room_id"]
            if frozen and not is_orig:
                continue
            if not is_orig:
                if not _compatible(orig.kind, r.kind) or r.capacity < need:
                    continue
                if c.same_block and r.block != orig.block:
                    continue
            hours = (k["end_min"] - k["start_min"]) / 60
            kwh = r.ac_kw * _duty(k["people"] / r.capacity, outdoor) * hours + r.capacity * 4 / 1000 * hours
            pen = 0.0 if is_orig else 1.5 + (2.5 if r.block != orig.block else 0.0)
            if _steady_co2(k["p90"], r.capacity) > settings.co2_limit_ppm:
                pen += co2_pen
            cost[i, j] = kwh + pen
    allowed = np.isfinite(cost)
    var = [(i, j) for i in range(C) for j in range(R) if allowed[i, j]]
    col = {v: n for n, v in enumerate(var)}
    obj = np.array([cost[i, j] for i, j in var])

    rows, lo, hi = [], [], []
    # each class gets exactly one room
    for i in range(C):
        row = np.zeros(len(var))
        for j in range(R):
            if (i, j) in col:
                row[col[(i, j)]] = 1
        rows.append(row); lo.append(1); hi.append(1)
    # no room holds two classes at once (with turnover)
    starts = sorted({k["start_min"] for k in classes})
    for j in range(R):
        for t in starts:
            active = [i for i, k in enumerate(classes)
                      if k["start_min"] <= t < k["end_min"] + TURNOVER_MIN and (i, j) in col]
            if len(active) > 1:
                row = np.zeros(len(var))
                for i in active:
                    row[col[(i, j)]] = 1
                rows.append(row); lo.append(0); hi.append(1)
    if c.max_moves is not None:
        row = np.zeros(len(var))
        for i, k in enumerate(classes):
            j = ridx[k["room_id"]]
            if (i, j) in col:
                row[col[(i, j)]] = 1
        rows.append(row); lo.append(C - c.max_moves); hi.append(C)

    t0 = time.perf_counter()
    res = milp(obj, integrality=np.ones(len(var)), bounds=Bounds(0, 1),
               constraints=LinearConstraint(np.array(rows), lo, hi), options={"time_limit": 10})
    info = {"solver": "mixed-integer program (SciPy HiGHS)", "variables": len(var), "constraints": len(rows),
            "status": res.message, "ms": round((time.perf_counter() - t0) * 1000)}
    if res.x is None:  # infeasible: keep today's rooms
        return [k["room_id"] for k in classes], info | {"fallback": "kept today's rooms"}
    pick = [None] * C
    for (i, j), n in col.items():
        if res.x[n] > 0.5:
            pick[i] = rooms[j].room_id
    return [p or classes[i]["room_id"] for i, p in enumerate(pick)], info


# --- 3 + 5. AC schedule and the digital twin -----------------------------------------

def _twin(classes: list[dict], rooms_for: list[str], mode: str, profile: list[float], comfort_first: bool) -> dict:
    """Energy and comfort for one day, 5-minute steps, with the simulator's physics.
    mode: 'schedule' (AC 07:30-21:00 in every room) or 'occupancy' (classes + pre-cool only)."""
    by_room: dict[str, list[dict]] = {}
    for k, rid in zip(classes, rooms_for):
        by_room.setdefault(rid, []).append(k)
    hourly = np.zeros(24)
    windows: dict[str, list[tuple[int, int]]] = {}
    co2_breach_hours = 0.0
    for r in ROOMS:
        ks = sorted(by_room.get(r.room_id, []), key=lambda k: k["start_min"])
        on = []
        if mode == "schedule":
            on = [(OPEN_MIN, CLOSE_MIN)]
        else:
            last_end = -10_000
            for k in ks:
                pre = _precool_min(profile[min(23, k["start_min"] // 60)], comfort_first)
                start = k["start_min"] - (0 if k["start_min"] - last_end <= pre else pre)
                if on and start <= on[-1][1]:
                    on[-1] = (on[-1][0], max(on[-1][1], k["end_min"]))
                else:
                    on.append((start, k["end_min"]))
                last_end = k["end_min"]
        windows[r.room_id] = on
        for m in range(0, 24 * 60, 5):
            people = next((k["people"] for k in ks if k["start_min"] <= m < k["end_min"]), 0.0)
            ac = any(a <= m < b for a, b in on)
            w = 60.0 + (r.capacity * 4.0 if (people or ac) else 0.0)
            if ac:
                w += r.ac_kw * 1000 * _duty(people / r.capacity, profile[m // 60])
            hourly[m // 60] += w / 1000 * (5 / 60)
        for k in ks:
            if _steady_co2(k["people"], r.capacity) > settings.co2_limit_ppm:
                co2_breach_hours += (k["end_min"] - k["start_min"]) / 60
    kwh = float(hourly.sum())
    return {"kwh": round(kwh, 1), "rm": round(kwh * settings.tariff_rm_per_kwh, 2),
            "kg_co2e": round(kwh * settings.grid_kg_co2e_per_kwh, 1),
            "co2_breach_hours": round(co2_breach_hours, 1), "hourly_kwh": [round(v, 2) for v in hourly],
            "windows": windows}


def _fmt(m: int) -> str:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


# --- the plan ----------------------------------------------------------------------

_PLANS: dict[str, dict] = {}


def plan(store: Store, day: date | None = None, constraints: dict | Constraints | None = None) -> dict:
    day = day or next_teaching_day()
    c = constraints if isinstance(constraints, Constraints) else Constraints.from_dict(constraints)
    wd = day.weekday()
    if DAYS[wd] in c.frozen_days:
        c.no_moves = True
    weather.refresh()
    profile = weather.day_profile(day)
    hist = _history(store)
    conf = room_confidence(link_health(store, hours=24))

    classes = []
    for s in sorted((s for s in TIMETABLE if s.weekday == wd), key=lambda s: (s.start_min, s.room_id)):
        med, p90, weeks = _predict(s, hist)
        classes.append({"module": s.module, "room_id": s.room_id, "start_min": s.start_min, "end_min": s.end_min,
                        "intakes": s.intakes, "people": med, "p90": p90, "weeks": weeks, "expected": s.expected})
    if not classes:
        return {"error": f"No classes are timetabled on {day:%A}.", "day": day.isoformat()}

    new_rooms, solver = _optimise(classes, c, profile)
    orig_rooms = [k["room_id"] for k in classes]
    base = _twin(classes, orig_rooms, "schedule", profile, c.comfort_first)
    ac_only = _twin(classes, orig_rooms, "occupancy", profile, c.comfort_first)
    full = _twin(classes, new_rooms, "occupancy", profile, c.comfort_first)

    moves = []
    for k, new in zip(classes, new_rooms):
        if new == k["room_id"]:
            continue
        a, b = ROOM_BY_ID[k["room_id"]], ROOM_BY_ID[new]
        hours = (k["end_min"] - k["start_min"]) / 60
        outdoor = profile[min(23, k["start_min"] // 60)]
        saved = (a.ac_kw * _duty(k["people"] / a.capacity, outdoor) - b.ac_kw * _duty(k["people"] / b.capacity, outdoor)) * hours
        fill = k["p90"] / b.capacity
        c_from, c_to = (conf.get(k["room_id"]), conf.get(new))
        trusted = all(x is not None and x.confidence >= 80 for x in (c_from, c_to))
        co2_fix = _steady_co2(k["p90"], a.capacity) > settings.co2_limit_ppm >= _steady_co2(k["p90"], b.capacity)
        reasons = []
        if b.block != a.block:
            reasons.append("changes block")
        if fill > 0.85:
            reasons.append(f"would be {fill:.0%} full on a busy week")
        if k["weeks"] < 2:
            reasons.append("fewer than 2 weeks of measured attendance")
        if not trusted:
            reasons.append("a sensor behind this room is not trusted yet")
        if saved <= 0.05 and not co2_fix:
            gain = f"frees {a.room_id} for another class's move"
        elif co2_fix:
            gain = (f"fixes stale air: {round(k['p90'])} people on a busy week push CO2 over "
                    f"{settings.co2_limit_ppm} ppm in {a.room_id}; {b.room_id} has {b.capacity} seats")
        else:
            gain = f"{round(k['people'])} expected in {a.capacity} seats; {b.room_id} has {b.capacity}"
        moves.append({
            "id": hashlib.sha1(f"{day}{k['module']}{k['start_min']}{new}".encode()).hexdigest()[:8],
            "module": k["module"], "day": DAYS[wd], "time": f"{_fmt(k['start_min'])}–{_fmt(k['end_min'])}",
            "from": a.room_id, "to": new, "from_name": a.name, "to_name": b.name,
            "people": round(k["people"]), "busy_week_people": round(k["p90"]), "from_seats": a.capacity,
            "to_seats": b.capacity, "kwh_saved": round(saved, 1), "co2_fix": co2_fix,
            "decision": "auto" if not reasons else "review", "reasons": reasons, "why": gain,
        })
    moves.sort(key=lambda m: (m["decision"] != "auto", -m["kwh_saved"]))

    rooms_used = sorted({r for r in new_rooms})
    schedule = []
    for r in ROOMS:
        w = full["windows"][r.room_id]
        cr = conf.get(r.room_id)
        schedule.append({"room_id": r.room_id, "windows": [f"{_fmt(a)}–{_fmt(b)}" for a, b in w],
                         "hours_on": round(sum(b - a for a, b in w) / 60, 1),
                         "hours_before": round((CLOSE_MIN - OPEN_MIN) / 60, 1),
                         "decision": "auto" if cr is not None and cr.confidence >= 80 else "review",
                         "confidence": cr.confidence if cr else 0})

    saved = base["kwh"] - full["kwh"]
    pid = hashlib.sha1(f"{day}{c.as_dict()}{time.time()}".encode()).hexdigest()[:10]
    out = {
        "id": pid, "day": day.isoformat(), "day_label": f"{day:%A} {day.day} {day:%B}",
        "constraints": c.as_dict(),
        "weather": {"hourly_c": profile, "max_c": max(profile[7:21]), "source": weather.source},
        "solver": solver,
        "classes": len(classes),
        "baseline": {k: v for k, v in base.items() if k != "windows"},
        "ac_only": {k: v for k, v in ac_only.items() if k != "windows"},
        "plan": {k: v for k, v in full.items() if k != "windows"},
        "saving": {"kwh": round(saved, 1), "rm": round(saved * settings.tariff_rm_per_kwh, 2),
                   "kg_co2e": round(saved * settings.grid_kg_co2e_per_kwh, 1),
                   "pct": round(saved / base["kwh"], 3) if base["kwh"] else 0,
                   "from_ac_schedule_kwh": round(base["kwh"] - ac_only["kwh"], 1),
                   "from_room_moves_kwh": round(ac_only["kwh"] - full["kwh"], 1)},
        "moves": moves,
        "auto_moves": sum(m["decision"] == "auto" for m in moves),
        "review_moves": sum(m["decision"] == "review" for m in moves),
        "ac_schedule": schedule,
        "rooms_used": rooms_used,
        "accuracy": backtest(store, hist),
    }
    _PLANS[pid] = out
    if len(_PLANS) > 20:
        _PLANS.pop(next(iter(_PLANS)))
    return out


def get_plan(pid: str) -> dict | None:
    return _PLANS.get(pid)


# --- 7. learn -------------------------------------------------------------------

def backtest(store: Store, hist: History | None = None) -> dict:
    """How good is the head-count forecast? Predict last week's classes from the weeks before."""
    hist = _history(store) if hist is None else hist
    if hist.df.empty:
        return {"classes": 0}
    cut = hist.df["day"].max() - timedelta(days=6)
    week = hist.df[hist.df["day"] >= cut]
    slots = {(s.room_id, s.weekday, s.start_hour): s for s in TIMETABLE}
    errs, within = [], 0
    for r in week.itertuples():
        slot = slots.get((r.room_id, r.weekday, r.start_hour))
        if slot is None:
            continue
        med, _, weeks = _predict(slot, hist, before=cut)
        if weeks == 0:
            continue
        e = abs(med - r.max_people)
        errs.append(e)
        within += e <= max(5, 0.15 * r.max_people)
    if not errs:
        return {"classes": 0}
    return {"classes": len(errs), "mean_abs_error_people": round(float(np.mean(errs)), 1),
            "within_tolerance_pct": round(within / len(errs), 3),
            "note": "forecast for last week's classes from the weeks before, against measured head counts"}

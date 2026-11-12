"""Turn a week of readings into ranked, costed actions with their evidence.

Every number in an action is computed here from the warehouse; Gemini only
explains and ranks what this module found. That keeps the brief honest: each
claim can be traced back to the SQL and rows attached to it.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

import pandas as pd

from .campus import BLOCK_LABEL, ROOM_BY_ID, ROOMS, TIMETABLE
from .config import settings
from .network import gateway_outages, link_health, room_confidence
from .store import Store

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK = "hour >= DATETIME_SUB((SELECT MAX(hour) FROM v_room_hourly), INTERVAL 7 DAY)"
AC_DUTY = 0.75  # typical compressor duty while a room is in use


@dataclass
class Action:
    kind: str
    title: str
    summary: str
    rooms: list[str]
    kwh_per_week: float = 0.0
    comfort: str = ""
    confidence: int = 100
    evidence_sql: str = ""
    evidence_rows: list[dict] = field(default_factory=list)
    chart: dict = field(default_factory=dict)
    score: float = 0.0

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}:{','.join(self.rooms)}:{self.title}".encode()).hexdigest()[:10]

    @property
    def rm_per_week(self) -> float:
        return round(self.kwh_per_week * settings.tariff_rm_per_kwh, 2)

    @property
    def kg_co2e_per_week(self) -> float:
        return round(self.kwh_per_week * settings.grid_kg_co2e_per_kwh, 1)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(id=self.id, rm_per_week=self.rm_per_week, kwh_per_week=round(self.kwh_per_week, 1),
                 kg_co2e_per_week=self.kg_co2e_per_week,
                 confidence_level="high" if self.confidence >= 80 else "medium" if self.confidence >= 55 else "low")
        return d


def _records(df: pd.DataFrame, n: int = 12) -> list[dict]:
    out = df.head(n).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].astype(str)
        elif pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].round(2)
    return out.astype(object).where(out.notna(), None).to_dict("records")


def _busy(room_id: str, weekday: int, start: int, end: int) -> bool:
    return any(s.room_id == room_id and s.weekday == weekday and s.start_hour < end and start < s.end_hour
               for s in TIMETABLE)


def _smallest_free(weekday: int, start: int, end: int, need: int, exclude: str, kinds: tuple) -> str | None:
    options = [r for r in ROOMS if r.room_id != exclude and r.kind in kinds and r.capacity >= need
               and not _busy(r.room_id, weekday, start, end)]
    return min(options, key=lambda r: (r.capacity, r.ac_kw)).room_id if options else None


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- detectors ---------------------------------------------------------------

def after_hours_cooling(store: Store) -> list[Action]:
    sql = f"""SELECT room_id, room_name, hour, weekday, hour_of_day, kwh
FROM v_empty_cooled
WHERE {WEEK} AND (weekday >= 5 OR hour_of_day < 7 OR hour_of_day >= 21)
ORDER BY hour"""
    df = store.query(sql)
    acts = []
    for rid, g in df.groupby("room_id"):
        kwh = float(g["kwh"].sum())
        if kwh < 20:
            continue
        days = sorted({DAYS[int(w)] for w in g["weekday"]}, key=DAYS.index)
        acts.append(Action(
            kind="after_hours_ac",
            title=f"Switch off the AC left running in {ROOM_BY_ID[rid].name}",
            summary=(f"{rid} was cooled for {len(g)} hours outside teaching time ({', '.join(days)}) "
                     f"with no one inside, using {kwh:.0f} kWh."),
            rooms=[rid], kwh_per_week=kwh,
            evidence_sql=sql.replace("WHERE ", f"WHERE room_id = '{rid}' AND "), evidence_rows=_records(g),
            chart={"type": "bar", "label": "kWh used while empty, outside teaching hours",
                   "x": [str(h)[5:16] for h in g["hour"]], "y": [round(v, 2) for v in g["kwh"]]},
        ))
    return acts


def schedule_driven_cooling(store: Store) -> list[Action]:
    """Weekday empty-cooled hours, minus the hour before a class (allowed pre-cool)."""
    sql = f"""SELECT room_id, room_name, block, hour, weekday, hour_of_day, kwh
FROM v_empty_cooled
WHERE {WEEK} AND weekday < 5 AND hour_of_day >= 7 AND hour_of_day < 21"""
    df = store.query(sql)
    if df.empty:
        return []
    starts = {(s.room_id, s.weekday, s.start_hour) for s in TIMETABLE}
    precool = df.apply(lambda r: (r.room_id, r.weekday, r.hour_of_day + 1) in starts, axis=1)
    df = df[~precool]
    acts = []
    for block, g in df.groupby("block"):
        kwh = float(g["kwh"].sum())
        if kwh < 50:
            continue
        per_room = g.groupby(["room_id", "room_name"], as_index=False).agg(empty_hours=("kwh", "size"),
                                                                           kwh=("kwh", "sum"))
        per_room = per_room.sort_values("kwh", ascending=False)
        acts.append(Action(
            kind="occupancy_linked_ac",
            title=f"Tie {BLOCK_LABEL.get(block, block)} air-conditioning to occupancy, not the building schedule",
            summary=(f"{_cap(BLOCK_LABEL.get(block, block))} rooms were cooled for {len(g)} empty room-hours on weekdays. "
                     f"Cooling only when the PIR sees people, with a 1-hour pre-cool before booked "
                     f"classes, avoids {kwh:.0f} kWh a week."),
            rooms=per_room["room_id"].tolist(), kwh_per_week=kwh,
            evidence_sql=sql + f" AND block = '{block}'", evidence_rows=_records(per_room),
            chart={"type": "bar", "label": "Empty-but-cooled kWh by room (weekdays)",
                   "x": per_room["room_id"].tolist(), "y": [round(v, 1) for v in per_room["kwh"]]},
        ))
    return acts


def _slots(store: Store) -> tuple[str, pd.DataFrame]:
    sql = """SELECT room_id, room_name, capacity, module, weekday, start_hour, expected,
       AVG(avg_people) AS avg_people, MAX(max_people) AS max_people,
       AVG(avg_peak_co2_ppm) AS peak_co2_ppm, SUM(kwh) AS kwh, COUNT(*) AS sessions
FROM v_slot_attendance
WHERE day >= DATETIME_SUB((SELECT MAX(day) FROM v_slot_attendance), INTERVAL 7 DAY)
GROUP BY room_id, room_name, capacity, module, weekday, start_hour, expected"""
    return sql, store.query(sql)


def right_size_rooms(store: Store) -> list[Action]:
    """Small classes in big rooms, grouped into one action per block."""
    sql, df = _slots(store)
    df = df[(df["avg_people"] >= 3) & (df["avg_people"] / df["capacity"] < 0.35)]
    moves: list[dict] = []
    for s in df.itertuples():
        room = ROOM_BY_ID[s.room_id]
        slot = next(t for t in TIMETABLE if t.room_id == s.room_id and t.weekday == s.weekday
                    and t.start_hour == s.start_hour)
        need = int(s.max_people * 1.15) + 1
        alt = _smallest_free(s.weekday, slot.start_hour, slot.end_hour, need, s.room_id,
                             ("lecture", "tutorial") if room.kind == "lecture" else (room.kind,))
        if not alt or ROOM_BY_ID[alt].ac_kw >= room.ac_kw:
            continue
        saved = (room.ac_kw - ROOM_BY_ID[alt].ac_kw) * AC_DUTY * (slot.end_hour - slot.start_hour)
        moves.append({"block": room.block, "module": s.module, "room_id": s.room_id, "day": DAYS[s.weekday],
                      "time": slot.time_label, "seats": room.capacity, "avg_people": round(s.avg_people, 1),
                      "peak_people": int(s.max_people), "move_to": alt,
                      "move_to_seats": ROOM_BY_ID[alt].capacity, "intakes": slot.intakes,
                      "kwh_saved": round(saved, 1)})
    acts = []
    for block in sorted({m["block"] for m in moves}):
        rows = sorted((m for m in moves if m["block"] == block), key=lambda m: -m["kwh_saved"])
        kwh = sum(m["kwh_saved"] for m in rows)
        if kwh < 10:
            continue
        fill = sum(m["avg_people"] for m in rows) / sum(m["seats"] for m in rows)
        top = rows[0]
        label = BLOCK_LABEL.get(block, block)
        acts.append(Action(
            kind="right_size",
            title=(f"Move {len(rows)} small classes out of the auditoriums" if block == "AUD"
                   else f"Move {len(rows)} small classes out of {label} rooms"),
            summary=(f"{len(rows)} timetabled classes a week fill only {fill:.0%} of their {label} seats; "
                     f"{sum(m['intakes'] <= 2 for m in rows)} of them are for one or two intakes in APU's timetable. "
                     f"Each fits a smaller free room with 15% headroom; for example {top['module']} "
                     f"({top['day']} {top['time']}, about {top['avg_people']:.0f} people in {top['room_id']}) "
                     f"fits {top['move_to']}."),
            rooms=sorted({m["room_id"] for m in rows} | {m["move_to"] for m in rows}), kwh_per_week=kwh,
            evidence_sql=sql, evidence_rows=[{k: v for k, v in m.items() if k != "block"} for m in rows],
            chart={"type": "bar", "label": "Seats used (%) by class that could move",
                   "x": [f"{m['module'].split(' ')[0]} {m['day']}" for m in rows[:12]],
                   "y": [round(100 * m["avg_people"] / m["seats"]) for m in rows[:12]], "ref": 35},
        ))
    return acts


def stale_air(store: Store) -> list[Action]:
    sql, df = _slots(store)
    # Act only where the limit is clearly exceeded; marginal rooms show on the map instead.
    df = df[df["peak_co2_ppm"] >= settings.co2_limit_ppm + 100]
    acts = []
    for (rid, module), g in df.groupby(["room_id", "module"]):
        room = ROOM_BY_ID[rid]
        slot = next(t for t in TIMETABLE if t.room_id == rid and t.module == module)
        need = int(max(g["max_people"].max(), slot.expected) * 1.1)
        alt = None
        for wd in sorted(g["weekday"]):
            cand = _smallest_free(int(wd), slot.start_hour, slot.end_hour, need, rid, ("lecture", "tutorial", "lab"))
            alt = alt or cand
        delta = 0.0
        if alt:
            delta = -(ROOM_BY_ID[alt].ac_kw - room.ac_kw) * AC_DUTY * (slot.end_hour - slot.start_hour) * len(g)
        peak = float(g["peak_co2_ppm"].mean())
        fix = (f"Moving it to {alt} ({ROOM_BY_ID[alt].capacity} seats) fixes the air" if alt
               else "No larger room is free; raise fresh-air supply for this slot")
        acts.append(Action(
            kind="air_quality",
            title=f"Fix stale air in {module.split(' ')[0]} ({room.name})",
            summary=(f"{module} averages {g['avg_people'].mean():.0f} people in a {room.capacity}-seat room "
                     f"(timetabled for {slot.expected}); CO2 peaks near {peak:.0f} ppm, above the "
                     f"{settings.co2_limit_ppm} ppm comfort limit. {fix}."),
            rooms=[rid] + ([alt] if alt else []), kwh_per_week=delta,
            comfort=f"{len(g)} sessions a week above {settings.co2_limit_ppm} ppm CO2",
            evidence_sql=sql.replace("GROUP BY", f"AND room_id = '{rid}'\nGROUP BY"), evidence_rows=_records(g),
            chart={"type": "bar", "label": "Average peak CO2 per session (ppm)",
                   "x": [DAYS[int(w)] for w in g["weekday"]], "y": [round(v) for v in g["peak_co2_ppm"]],
                   "ref": settings.co2_limit_ppm},
        ))
    return acts


def data_reliability(store: Store) -> list[Action]:
    health = link_health(store, hours=24)
    acts = []
    for h in health:
        if h.level != "low" or not h.simulated and h.minutes_since_seen > 60 * 24:
            continue
        stuck = h.distinct_co2_values <= 2
        title = (f"Replace the CO2 sensor in {ROOM_BY_ID[h.room_id].name}" if stuck
                 else f"Fix the network link for {ROOM_BY_ID[h.room_id].name}")
        fix = ("its CO2 value has not changed in 24 hours, so air-quality decisions there are blind"
               if stuck else "add a repeater or move the node closer to the gateway")
        acts.append(Action(
            kind="data_reliability", title=title,
            summary=f"Node {h.device_id} on {h.gateway_id}: {'; '.join(h.issues)}. Before acting on {h.room_id}, {fix}.",
            rooms=[h.room_id], confidence=h.confidence,
            evidence_sql=f"""SELECT day, reports, delivery_ratio, avg_rssi_dbm, min_rssi_dbm
FROM v_network_daily WHERE device_id = '{h.device_id}' ORDER BY day DESC""",
            evidence_rows=[h.as_dict()],
            chart={"type": "bar", "label": "Link confidence (0-100)", "x": [h.device_id], "y": [h.confidence],
                   "ref": 80},
        ))
    outages = gateway_outages(store)
    for gw, g in outages.groupby("gateway_id"):
        if len(g) < 2:
            continue
        acts.append(Action(
            kind="data_reliability",
            title=f"Investigate gateway {gw}: {len(g)} silent hours this week",
            summary=(f"Every node behind {gw} stopped reporting from {str(g['hour'].min())[5:16]} to "
                     f"{str(g['hour'].max())[5:16]}. Decisions for those hours rest on gaps, not data."),
            rooms=[r.room_id for r in ROOMS if r.gateway_id == gw], confidence=60,
            evidence_sql="(see gateway_outages in campuspulse/network.py)", evidence_rows=_records(g),
            chart={"type": "bar", "label": "Reports per hour", "x": [str(h)[5:16] for h in g["hour"]],
                   "y": [0] * len(g)},
        ))
    return acts


MAX_PER_KIND = {"air_quality": 3}

DETECTORS = (after_hours_cooling, schedule_driven_cooling, right_size_rooms, stale_air, data_reliability)


def build_actions(store: Store) -> list[dict]:
    conf = room_confidence(link_health(store, hours=24))
    actions: list[Action] = []
    for detect in DETECTORS:
        actions.extend(detect(store))
    for a in actions:
        if a.kind != "data_reliability":
            levels = [conf[r].confidence for r in a.rooms if r in conf]
            a.confidence = min(levels) if levels else 50
        # Comfort and data quality have no tariff, so they are weighted into ringgit-equivalent points.
        comfort_pts = 0.0
        if a.kind == "air_quality":
            peak = max((r.get("peak_co2_ppm") or 0) for r in a.evidence_rows) if a.evidence_rows else 0
            comfort_pts = max(0.0, peak - settings.co2_limit_ppm) * len(a.evidence_rows) / 3
        reliability_pts = 90.0 if a.kind == "data_reliability" else 0.0
        a.score = round((max(a.rm_per_week, 0) + comfort_pts + reliability_pts) * (0.5 + a.confidence / 200), 2)
    actions.sort(key=lambda a: a.score, reverse=True)
    # Keep the list readable: the strongest few of each kind; the rest stay queryable in Ask.
    kept, seen = [], {}
    for a in actions:
        seen[a.kind] = seen.get(a.kind, 0) + 1
        if seen[a.kind] <= MAX_PER_KIND.get(a.kind, 99):
            kept.append(a)
    return [a.as_dict() for a in kept]

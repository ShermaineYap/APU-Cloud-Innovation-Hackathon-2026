"""CampusPulse service: ingest, decision API and the web app, in one Cloud Run container."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, analytics, decisions, network, report, study
from . import autopilot
from .agent import ask, chart_caption, daily_brief, draft_notices, explain_plan, plan_constraints, suggestions
from .campus import LIVE_ROOM, ROOM_BY_ID
from .network import is_camera, is_test_device
from .config import settings
from .control import control
from .weather import weather
from .simulator import Fleet
from .store import get_store

log = logging.getLogger("campuspulse")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB = Path(__file__).resolve().parents[1] / "web" / "static"


# --- cache: decisions are recomputed at most once a minute ---------------------

class Cache:
    def __init__(self):
        self.data: dict[str, tuple[float, object]] = {}
        self.lock = threading.Lock()

    def get(self, key, ttl, fn, force=False):
        with self.lock:
            hit = self.data.get(key)
        if hit and not force and time.time() - hit[0] < ttl:
            return hit[1]
        value = fn()
        with self.lock:
            self.data[key] = (time.time(), value)
        return value

    def clear(self):
        with self.lock:
            self.data.clear()

    def drop(self, prefix: str):
        with self.lock:
            for k in [k for k in self.data if k.startswith(prefix)]:
                del self.data[k]


cache = Cache()
state = {"last_real": None, "real_count": 0, "started": datetime.now(timezone.utc), "real_rooms": {}}


def _real_kinds(room_id: str, within_s: int = 300) -> set[str]:
    now = datetime.now(timezone.utc)
    return {kind for (rid, kind), t in state["real_rooms"].items()
            if rid == room_id and (now - t).total_seconds() < within_s}


def _store_simulated(rows: list[dict]) -> None:
    """Write simulated rows. Where real hardware is reporting, it replaces the simulated twin:
    a live sensor node replaces the whole row, a live camera replaces just the head count."""
    out = []
    for r in rows:
        kinds = _real_kinds(r["room_id"])
        if "sensor" in kinds:
            continue
        if "camera" in kinds:
            r = r | {"people": None}
        out.append(r)
    if out:
        get_store().insert_readings(out)
        cache.drop("overview")


def _live_loop(fleet: Fleet, stop: threading.Event):
    while not stop.wait(30):
        try:
            _sync_control()
            _store_simulated(fleet.tick(datetime.now(timezone.utc)))
        except Exception:  # keep the demo alive
            log.exception("live simulator tick failed")


sim = {"fleet": None, "lock": threading.Lock()}


def _catch_up() -> None:
    """Request mode: produce the readings a background loop would have written since the last request."""
    fleet = sim["fleet"]
    if fleet is None or not sim["lock"].acquire(blocking=False):
        return  # not started, or another request is already catching up
    try:
        try:
            _sync_control()
        except Exception:  # AC modes can catch up later; readings must not wait for them
            log.exception("control sync failed")
        _store_simulated(fleet.catch_up(datetime.now(timezone.utc)))
    except Exception:
        log.exception("simulator catch-up failed")
    finally:
        sim["lock"].release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = get_store()
    if settings.setup_on_start:
        store.setup()
    weather.refresh()  # real KL weather before the simulator runs
    fleet = Fleet()
    fleet.policies = control.policies  # simulated rooms obey approved AC changes
    if store.reading_count() and store.stale_rooms():
        log.info("Campus model changed; clearing old readings")
        store.clear_readings()
    if store.reading_count() == 0:
        log.info("Seeding %d days of simulated history", settings.history_days)
        store.insert_readings(fleet.history())
    else:
        rows = fleet.history()  # also warms the room state so live rows continue smoothly
        # Fill hours when the service was not running, so replay and the 7-day figures never hit a hole.
        have = store.query("SELECT DISTINCT TIMESTAMP_TRUNC(ts, HOUR) AS h FROM readings")["h"]
        have = {pd.Timestamp(h).tz_localize("UTC") if pd.Timestamp(h).tzinfo is None
                else pd.Timestamp(h).tz_convert("UTC") for h in have}
        this_hour = pd.Timestamp.now(tz="UTC").floor("h")
        gap = [r for r in rows
               if (h := pd.Timestamp(r["ts"]).tz_convert("UTC").floor("h")) not in have and h < this_hour]
        if gap:
            log.info("Back-filling %d simulated readings for hours the service was down", len(gap))
            store.insert_readings(gap)
    stop = threading.Event()
    if settings.live_simulator and settings.sim_mode == "request":
        sim["fleet"] = fleet
    elif settings.live_simulator:
        threading.Thread(target=_live_loop, args=(fleet, stop), daemon=True).start()
    yield
    stop.set()
    sim["fleet"] = None


app = FastAPI(title="CampusPulse", version=__version__, lifespan=lifespan)


# --- ingest -------------------------------------------------------------------

class Reading(BaseModel):
    device_id: str = Field(max_length=64)
    room_id: str
    ts: datetime | None = None  # server time is used when the node has no clock
    occupied: bool
    people: int | None = Field(default=None, ge=0, le=500)
    co2_ppm: float | None = Field(default=None, ge=300, le=10000)  # None for camera nodes
    temp_c: float | None = Field(default=None, ge=0, le=60)
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    watts: float | None = Field(default=None, ge=0)
    ac_on: bool | None = None
    rssi_dbm: float | None = Field(default=None, ge=-120, le=0)
    packets_sent: int = Field(default=1, ge=0)
    packets_acked: int = Field(default=1, ge=0)
    gateway_id: str | None = None

    def to_row(self) -> dict:
        room = ROOM_BY_ID.get(self.room_id)
        if room is None:
            raise HTTPException(422, f"unknown room_id {self.room_id}")
        if is_camera(self.device_id):
            # A camera counts people and nothing else; energy and air come from the room's sensor node.
            if self.people is None:
                raise HTTPException(422, "camera readings need a people count")
            return self.model_dump() | {"ts": self.ts or datetime.now(timezone.utc),
                                        "gateway_id": self.gateway_id or room.gateway_id,
                                        "occupied": self.people > 0, "watts": None, "ac_on": None,
                                        "co2_ppm": None, "temp_c": None, "humidity_pct": None,
                                        "simulated": False}
        if self.co2_ppm is None or self.temp_c is None or self.rssi_dbm is None:
            raise HTTPException(422, "sensor readings need co2_ppm, temp_c and rssi_dbm")
        watts = self.watts or 0.0
        return self.model_dump() | {
            "ts": self.ts or datetime.now(timezone.utc),
            "gateway_id": self.gateway_id or room.gateway_id,
            "ac_on": self.ac_on if self.ac_on is not None else watts > room.ac_kw * 1000 * 0.3,
            "watts": watts,
            "simulated": False,
        }


_publisher = {}


def _publish(rows: list[dict]) -> int:
    """Send readings through Pub/Sub; the push subscription delivers them to /api/pubsub/push."""
    from google.cloud import pubsub_v1

    pub = _publisher.get("client") or _publisher.setdefault("client", pubsub_v1.PublisherClient())
    futures = [pub.publish(settings.pubsub_topic, json.dumps(r, default=str).encode()) for r in rows]
    for f in futures:
        f.result(timeout=10)
    return len(futures)


def _accept(rows: list[dict]) -> int:
    n = _publish(rows) if settings.pubsub_topic else get_store().insert_readings(rows)
    cache.drop("overview")
    rows = [r for r in rows if not is_test_device(r["device_id"])]  # test traffic never counts as live hardware
    if not rows:
        return n
    now = datetime.now(timezone.utc)
    state["last_real"] = now
    state["real_count"] += n
    for r in rows:
        kind = "camera" if is_camera(r["device_id"]) else "sensor"
        state["real_rooms"][(r["room_id"], kind)] = now
    return n


@app.post("/api/ingest")
async def ingest(request: Request, x_device_key: str = Header(default="")):
    # Authenticate before parsing, so unknown senders learn nothing about the schema.
    if not hmac.compare_digest(x_device_key, settings.ingest_key):
        raise HTTPException(401, "missing or wrong X-Device-Key header")
    payload = await request.json()
    try:
        items = [Reading.model_validate(p) for p in (payload if isinstance(payload, list) else [payload])]
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json(include_url=False))) from e
    n = _accept([r.to_row() for r in items])
    return {"accepted": n, "via": "pubsub" if settings.pubsub_topic else "direct"}


def _normalise(r: dict) -> dict:
    """Fill fields a Pub/Sub message may lack, so every stored reading is complete."""
    room = ROOM_BY_ID[r["room_id"]]
    return r | {"gateway_id": r.get("gateway_id") or room.gateway_id,
                "packets_sent": r.get("packets_sent") if r.get("packets_sent") is not None else 1,
                "packets_acked": r.get("packets_acked") if r.get("packets_acked") is not None else 1,
                "simulated": bool(r.get("simulated", False))}


@app.post("/api/pubsub/push")
async def pubsub_push(request: Request, key: str = ""):
    """Pub/Sub push subscription target: decode and store.

    The subscription's push endpoint carries the ingest key (?key=...), so only our own
    subscription can write here."""
    if not hmac.compare_digest(key, settings.ingest_key):
        raise HTTPException(401, "missing or wrong key")
    envelope = await request.json()
    try:
        data = json.loads(base64.b64decode(envelope["message"]["data"]))
    except (KeyError, ValueError, TypeError) as e:
        # Acknowledge malformed messages so Pub/Sub does not redeliver them forever.
        log.warning("dropping malformed Pub/Sub message: %s", e)
        return {"stored": 0}
    rows = [_normalise(r) for r in (data if isinstance(data, list) else [data]) if r.get("room_id") in ROOM_BY_ID]
    get_store().insert_readings(rows)
    cache.drop("overview")
    return {"stored": len(rows)}


# --- read API -------------------------------------------------------------------

def _mode() -> dict:
    last = state["last_real"]
    live_seen = bool(last and (datetime.now(timezone.utc) - last).total_seconds() < 300)
    return {"backend": settings.backend, "gemini": settings.gemini_enabled, "model": settings.gemini_model,
            "live_room": LIVE_ROOM, "live_node_online": live_seen,
            "live_node_last_seen": last.isoformat() if last else None,
            "real_readings": state["real_count"], "tariff_rm_per_kwh": settings.tariff_rm_per_kwh,
            "grid_kg_co2e_per_kwh": settings.grid_kg_co2e_per_kwh,
            "co2_limit_ppm": settings.co2_limit_ppm}


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, **_mode()}


def _sync_control():
    """After a restart, rebuild each room's AC mode from the decision log."""
    if not control.synced:
        control.sync(_actions(), _decision_status())


@app.get("/api/overview")
def overview(at: str | None = None):
    """KPIs and room states. `at` (local time, e.g. 2026-11-04T10:00) replays the floor plan as it was then.

    Cached briefly and shared by every viewer (dropped whenever new readings or decisions arrive),
    so an open dashboard costs a few warehouse queries a minute, not a few per viewer."""
    replay = None
    if at:
        try:
            replay = pd.Timestamp(at)
        except ValueError:
            raise HTTPException(422, "at must be a local date-time like 2026-11-04T10:00")
        if replay.tzinfo is not None:
            replay = replay.tz_convert(settings.timezone).tz_localize(None)
        replay = replay.floor("min")
    key = f"overview:{replay.isoformat() if replay is not None else 'live'}"
    return cache.get(key, 600 if replay is not None else 20, lambda: _overview(replay))


def _overview(replay: pd.Timestamp | None) -> dict:
    store = get_store()
    rooms = [r | {"ac_mode": control.mode(r["room_id"])} for r in analytics.room_states(store, at=replay)]
    moment = replay.to_pydatetime() if replay is not None else datetime.now(settings.timezone).replace(tzinfo=None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": cache.get("kpis", 60, lambda: analytics.kpis(store)),
        "rooms": rooms,
        "study": study.free_rooms(rooms, moment),
        "weather": cache.get("weather", 600, weather.now),
        "replay_at": replay.isoformat() if replay is not None else None,
        "replay_range": cache.get("replay_range", 300, lambda: analytics.replay_range(store)),
        "mode": _mode(),
    }


def _actions(force=False):
    return cache.get("actions", 60, lambda: decisions.build_actions(get_store()), force)


@app.get("/api/actions")
def actions():
    return _actions()


@app.get("/api/brief")
def brief(refresh: bool = False):
    store = get_store()

    def make():
        acts = _actions(force=refresh)
        return {"brief": daily_brief(acts, analytics.kpis(store)), "actions": acts,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    return cache.get("brief", 1800, make, force=refresh)


class Decision(BaseModel):
    status: Literal["approved", "dismissed"]
    decided_by: str = Field(default="Estates manager", max_length=80)


@app.post("/api/actions/{action_id}/decide")
def decide(action_id: str, body: Decision):
    act = next((a for a in _actions() if a["id"] == action_id), None)
    if act is None:
        raise HTTPException(404, "action not found; refresh the brief")
    _sync_control()  # before logging, so this decision is what changes the rooms' AC modes
    get_store().insert_decision({
        "decided_at": datetime.now(timezone.utc), "action_id": action_id, "kind": act["kind"],
        "title": act["title"], "status": body.status, "kwh_per_week": act["kwh_per_week"],
        "rm_per_week": act["rm_per_week"], "decided_by": body.decided_by,
    })
    commands = control.apply(act, body.status, body.decided_by)
    cache.drop("overview")  # rooms show their new AC mode straight away
    return {"ok": True, "action_id": action_id, "status": body.status, "commands": commands}


# Gemini helps Autopilot read goals and write words, but the plan never waits long for it.
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout  # noqa: E402

_gemini_pool = ThreadPoolExecutor(max_workers=4)
GEMINI_WAIT_S = 10


def _within(fn, *args, fallback):
    fut = _gemini_pool.submit(fn, *args)
    try:
        return fut.result(timeout=GEMINI_WAIT_S)
    except _Timeout:
        log.warning("Gemini took over %ss; using the fallback", GEMINI_WAIT_S)
        return fallback()


def _goals(text: str):
    from .agent.gemini import plan_constraints as read
    return _within(read, text, fallback=lambda: (autopilot.parse_instructions_offline(text).as_dict(),
                                                 "rule-based reader (Gemini error: 504 slow to answer)"))


def _explain(p: dict) -> dict:
    from .agent.gemini import _fallback_explain
    return _within(explain_plan, p, fallback=lambda: _fallback_explain(p) | {"source": "template (Gemini error: 504 slow to answer)"})


def _notices(moves: list[dict]) -> dict:
    from .agent.gemini import _fallback_notice
    return _within(draft_notices, moves, fallback=lambda: {
        "notices": [{"id": m["id"], "text": _fallback_notice(m)} for m in moves],
        "source": "template (Gemini error: 504 slow to answer)"})


class PlanRequest(BaseModel):
    instructions: str = Field(default="", max_length=500)
    day: str | None = None  # YYYY-MM-DD; default: the next teaching day


@app.post("/api/autopilot/plan")
def autopilot_plan(body: PlanRequest):
    """Plan the next teaching day: forecast, optimise rooms, schedule AC, check comfort, compare in the twin."""
    constraints, reader = _goals(body.instructions)
    day = None
    if body.day:
        try:
            day = datetime.fromisoformat(body.day).date()
        except ValueError:
            raise HTTPException(422, "day must be YYYY-MM-DD")
    p = autopilot.plan(get_store(), day=day, constraints=constraints)
    if "error" in p:
        raise HTTPException(422, p["error"])
    p["instructions"] = body.instructions
    p["constraints_read_by"] = reader
    p["explanation"] = _explain(p)
    state["last_plan"] = p["id"]
    return p


@app.get("/api/autopilot/latest")
def autopilot_latest():
    p = autopilot.get_plan(state.get("last_plan") or "")
    return p or {}


class ApplyRequest(BaseModel):
    plan_id: str
    approve_review: bool = False   # also apply the changes that were sent for a person's approval
    decided_by: str = Field(default="Estates manager", max_length=80)


@app.post("/api/autopilot/apply")
def autopilot_apply(body: ApplyRequest):
    """Carry out a plan: AC modes go to the rooms, moves are logged and notices drafted."""
    p = autopilot.get_plan(body.plan_id)
    if p is None:
        raise HTTPException(404, "plan not found; plan again")
    ok = lambda d: d == "auto" or body.approve_review  # noqa: E731
    rooms = [r["room_id"] for r in p["ac_schedule"] if ok(r["decision"])]
    moves = [m for m in p["moves"] if ok(m["decision"])]
    _sync_control()
    act = {"id": f"autopilot-{p['day']}", "kind": "occupancy_linked_ac", "rooms": rooms,
           "title": f"Autopilot plan for {p['day_label']}"}
    commands = control.apply(act, "approved", body.decided_by)
    kwh = p["saving"]["kwh"]
    get_store().insert_decision({
        "decided_at": datetime.now(timezone.utc), "action_id": p["id"], "kind": "autopilot",
        "title": act["title"] + f" ({len(rooms)} rooms, {len(moves)} moves)", "status": "approved",
        "kwh_per_week": kwh, "rm_per_week": p["saving"]["rm"], "decided_by": body.decided_by,
    })
    held = [m for m in p["moves"] if not ok(m["decision"])]
    return {"ok": True, "commands": commands, "rooms": rooms, "moves_applied": moves, "moves_held": len(held),
            "notices": _notices(moves)}


@app.get("/api/commands")
def command_log():
    """Device commands sent for approved changes, newest first, and each room's current AC mode."""
    _sync_control()
    return {"commands": control.log[:50], "modes": dict(control.policies),
            "transport": "mqtt" if settings.mqtt_host else "https-pull"}


@app.get("/api/rooms/{room_id}/command")
def room_command(room_id: str, x_device_key: str | None = Header(default=None)):
    """HTTPS pull for nodes not on MQTT: the AC mode this room should run."""
    if not x_device_key or not hmac.compare_digest(x_device_key, settings.ingest_key):
        raise HTTPException(401, "missing or wrong X-Device-Key")
    if room_id not in ROOM_BY_ID:
        raise HTTPException(404, "unknown room")
    _sync_control()
    return control.state_for(room_id)


@app.get("/api/decisions")
def decision_log():
    df = get_store().query("SELECT * FROM decisions ORDER BY decided_at DESC LIMIT 50")
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _decision_status() -> dict[str, str]:
    """Latest decision per action id."""
    # BigQuery accepts QUALIFY only alongside WHERE, GROUP BY or HAVING, hence WHERE TRUE.
    df = get_store().query("""SELECT action_id, status FROM decisions WHERE TRUE
        QUALIFY ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY decided_at DESC) = 1""")
    return dict(zip(df["action_id"], df["status"]))


@app.get("/api/actions/{action_id}/caption")
def caption(action_id: str):
    act = next((a for a in _actions() if a["id"] == action_id), None)
    if act is None:
        raise HTTPException(404, "action not found; refresh the brief")
    return cache.get(f"caption:{action_id}", 3600, lambda: chart_caption(act))


@app.get("/api/report.pdf")
def estates_report():
    b = brief()
    pdf = report.build_pdf(b["brief"], b["actions"], analytics.kpis(get_store()), _decision_status(), _mode())
    name = f"campuspulse-estates-report-{datetime.now(settings.timezone):%Y-%m-%d}.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/rooms/{room_id}")
def room(room_id: str):
    if room_id not in ROOM_BY_ID:
        raise HTTPException(404, "unknown room")
    return cache.get(f"room:{room_id}", 120, lambda: analytics.room_detail(get_store(), room_id))


@app.get("/api/network")
def network_health():
    store = get_store()

    def make():
        health = [h.as_dict() for h in network.link_health(store, hours=24)]
        out = network.gateway_outages(store)
        outages = [{"gateway_id": r.gateway_id, "hour": str(r.hour)} for r in out.itertuples()]
        return {"nodes": health, "outages": outages}

    return cache.get("network", 60, make)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.post("/api/ask")
def ask_data(body: Question):
    return ask(get_store(), body.question)


@app.get("/api/suggestions")
def ask_suggestions():
    return suggestions()


# --- web app --------------------------------------------------------------------

@app.middleware("http")
async def catch_up_simulation(request: Request, call_next):
    """Request mode (Cloud Run free tier): bring simulated readings up to now before answering."""
    path = request.url.path
    if (settings.sim_mode == "request" and path.startswith("/api/")
            and not path.startswith(("/api/ingest", "/api/pubsub"))):
        await run_in_threadpool(_catch_up)
    return await call_next(request)


@app.middleware("http")
async def revalidate_web_app(request: Request, call_next):
    """Browsers must revalidate the dashboard files, so a redeploy is never hidden behind a stale cache."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")

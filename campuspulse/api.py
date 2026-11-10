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
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, analytics, decisions, network, report
from .agent import ask, chart_caption, daily_brief, suggestions
from .campus import LIVE_ROOM, ROOM_BY_ID
from .network import is_camera
from .config import settings
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


cache = Cache()
state = {"last_real": None, "real_count": 0, "started": datetime.now(timezone.utc), "real_rooms": {}}


def _real_kinds(room_id: str, within_s: int = 300) -> set[str]:
    now = datetime.now(timezone.utc)
    return {kind for (rid, kind), t in state["real_rooms"].items()
            if rid == room_id and (now - t).total_seconds() < within_s}


def _live_loop(fleet: Fleet, stop: threading.Event):
    store = get_store()
    while not stop.wait(30):
        try:
            rows = fleet.tick(datetime.now(timezone.utc))
            # Where real hardware is reporting, it replaces the simulated twin: a live sensor node
            # replaces the whole row, a live camera replaces just the head count.
            out = []
            for r in rows:
                kinds = _real_kinds(r["room_id"])
                if "sensor" in kinds:
                    continue
                if "camera" in kinds:
                    r = r | {"people": None}
                out.append(r)
            store.insert_readings(out)
        except Exception:  # keep the demo alive
            log.exception("live simulator tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = get_store()
    store.setup()
    fleet = Fleet()
    if store.reading_count() and store.stale_rooms():
        log.info("Campus model changed; clearing old readings")
        store.clear_readings()
    if store.reading_count() == 0:
        log.info("Seeding %d days of simulated history", settings.history_days)
        store.insert_readings(fleet.history())
    else:
        fleet.history()  # warm the room state so live rows continue smoothly
    stop = threading.Event()
    if settings.live_simulator:
        threading.Thread(target=_live_loop, args=(fleet, stop), daemon=True).start()
    yield
    stop.set()


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


def _accept(rows: list[dict]) -> int:
    if settings.pubsub_topic:
        from google.cloud import pubsub_v1

        pub = pubsub_v1.PublisherClient()
        for r in rows:
            pub.publish(settings.pubsub_topic, json.dumps(r, default=str).encode()).result(timeout=10)
        n = len(rows)
    else:
        n = get_store().insert_readings(rows)
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


@app.post("/api/pubsub/push")
async def pubsub_push(request: Request):
    """Pub/Sub push subscription target: decode and store."""
    envelope = await request.json()
    data = json.loads(base64.b64decode(envelope["message"]["data"]))
    get_store().insert_readings([data])
    return {"stored": 1}


# --- read API -------------------------------------------------------------------

def _mode() -> dict:
    last = state["last_real"]
    live_seen = bool(last and (datetime.now(timezone.utc) - last).total_seconds() < 300)
    return {"backend": settings.backend, "gemini": settings.gemini_enabled, "model": settings.gemini_model,
            "live_room": LIVE_ROOM, "live_node_online": live_seen,
            "live_node_last_seen": last.isoformat() if last else None,
            "real_readings": state["real_count"], "tariff_rm_per_kwh": settings.tariff_rm_per_kwh,
            "co2_limit_ppm": settings.co2_limit_ppm}


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, **_mode()}


@app.get("/api/overview")
def overview(at: str | None = None):
    """KPIs and room states. `at` (local time, e.g. 2026-11-04T10:00) replays the floor plan as it was then."""
    store = get_store()
    replay = None
    if at:
        try:
            replay = pd.Timestamp(at)
        except ValueError:
            raise HTTPException(422, "at must be a local date-time like 2026-11-04T10:00")
        if replay.tzinfo is not None:
            replay = replay.tz_convert(settings.timezone).tz_localize(None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": cache.get("kpis", 60, lambda: analytics.kpis(store)),
        "rooms": analytics.room_states(store, at=replay),
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
    get_store().insert_decision({
        "decided_at": datetime.now(timezone.utc), "action_id": action_id, "kind": act["kind"],
        "title": act["title"], "status": body.status, "kwh_per_week": act["kwh_per_week"],
        "rm_per_week": act["rm_per_week"], "decided_by": body.decided_by,
    })
    return {"ok": True, "action_id": action_id, "status": body.status}


@app.get("/api/decisions")
def decision_log():
    df = get_store().query("SELECT * FROM decisions ORDER BY decided_at DESC LIMIT 50")
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _decision_status() -> dict[str, str]:
    """Latest decision per action id."""
    df = get_store().query("""SELECT action_id, status FROM decisions
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
    return analytics.room_detail(get_store(), room_id)


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

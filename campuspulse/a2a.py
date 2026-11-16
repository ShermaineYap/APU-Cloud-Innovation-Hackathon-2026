"""CampusPulse as an A2A agent, ready to register in Gemini Enterprise.

Gemini Enterprise lets an admin register custom agents that speak the open
Agent2Agent (A2A) protocol. This module makes CampusPulse one:

  GET  /.well-known/agent-card.json   the agent card (A2A 0.3; /.well-known/agent.json too)
  POST /a2a                           JSON-RPC 2.0: message/send

Three skills, all READ-ONLY: answer questions about the campus data (the SQL
agent), plan tomorrow (Autopilot proposes a plan; it is never applied from here)
and campus status (the morning brief). Applying a plan or switching AC stays in
the CampusPulse app, behind human approval.

Nothing here needs Gemini Enterprise to work: the endpoint answers any A2A client
(curl included). Set CP_A2A_KEY to require "Authorization: Bearer <key>", and
CP_PUBLIC_URL to the service's https address if the card shows the wrong host.
"""
from __future__ import annotations

import hmac
import re
import uuid

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .config import settings

router = APIRouter()
PROTOCOL_VERSION = "0.3.0"

SKILLS = [
    {"id": "ask_campus_data", "name": "Ask the campus data",
     "description": "Answers questions about APU rooms, cooling energy, air quality and sensor health. "
                    "Gemini writes read-only SQL through a guarded tool and quotes the numbers from the result.",
     "tags": ["energy", "air quality", "rooms", "sql", "bigquery"],
     "examples": ["How many hours was Auditorium 2 cooled while empty on Saturday?",
                  "Which Tech Lab had the highest CO2 this week?"]},
    {"id": "plan_tomorrow", "name": "Plan tomorrow's rooms and AC",
     "description": "Proposes the next teaching day's room assignments and AC schedule with an optimiser, "
                    "honouring goals in plain English, and reports the saving. Proposes only: approval happens "
                    "in the CampusPulse app.",
     "tags": ["planning", "optimisation", "air-conditioning", "autopilot"],
     "examples": ["Plan tomorrow, comfort first, at most 5 room moves",
                  "Plan tomorrow but don't move any classes"]},
    {"id": "campus_status", "name": "Campus status",
     "description": "This week's figures: energy spent cooling empty rooms, stale-air hours, data confidence, "
                    "and the top recommended changes.",
     "tags": ["status", "brief", "kpi"],
     "examples": ["How is the campus doing this week?", "Give me the morning brief"]},
]

PLAN_WORDS = re.compile(r"\b(plan|planning|tomorrow|schedule|autopilot|optimi[sz]e|reassign|move classes)\b", re.I)
STATUS_WORDS = re.compile(r"\b(status|brief|summary|overview|how is|how are|this week overall|kpis?)\b", re.I)


def _base_url(request: Request) -> str:
    if settings.public_url:
        return settings.public_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def agent_card(base_url: str) -> dict:
    card = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "CampusPulse",
        "description": "Plans APU's rooms and air-conditioning for the next teaching day, explains campus "
                       "energy and air quality from sensor data, and proposes changes with the evidence attached.",
        "url": f"{base_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "provider": {"organization": "What's Google?, Asia Pacific University", "url": base_url},
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": SKILLS,
    }
    if settings.a2a_key:
        card["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        card["security"] = [{"bearer": []}]
    return card


@router.get("/.well-known/agent-card.json")
@router.get("/.well-known/agent.json")
def well_known_card(request: Request):
    return agent_card(_base_url(request))


def _error(rpc_id, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}},
                        status_code=status)


def route(text: str, skill_hint: str | None = None) -> str:
    """Which skill a message is for: an explicit hint from the caller wins, then the wording."""
    if skill_hint in {s["id"] for s in SKILLS}:
        return skill_hint
    if PLAN_WORDS.search(text):
        return "plan_tomorrow"
    if STATUS_WORDS.search(text):
        return "campus_status"
    return "ask_campus_data"


def _fmt(v: float, d: int = 0) -> str:
    return f"{v:,.{d}f}"


def run_skill(skill: str, text: str) -> tuple[str, dict]:
    """Run one skill with the app's own agents; returns (reply text, structured data)."""
    from . import analytics, api  # the app's handlers: same agents, guards and fallbacks as the dashboard
    from .agent import ask
    from .store import get_store

    if skill == "plan_tomorrow":
        p = api.autopilot_plan(api.PlanRequest(instructions=text))
        s, ex = p["saving"], p.get("explanation", {})
        moves = [{k: m[k] for k in ("module", "time", "from", "to", "decision", "why")} for m in p["moves"][:5]]
        reply = (f"{ex.get('headline') or 'Plan for ' + p['day_label']}\n\n{ex.get('summary', '')}\n\n"
                 f"Proposed for {p['day_label']}: saves {_fmt(s['kwh'])} kWh ({_fmt(s['pct'] * 100)}%), "
                 f"about RM {_fmt(s['rm'])} and {_fmt(s['kg_co2e'])} kg CO2e; stale-air class-hours "
                 f"{p['baseline']['co2_breach_hours']:g} -> {p['plan']['co2_breach_hours']:g}. "
                 f"{p['auto_moves']} room changes are safe to apply, {p['review_moves']} need a person. "
                 f"Nothing has been applied: approve it in the CampusPulse app (Autopilot tab).")
        sv = p.get("solver", {})
        return reply.strip(), {"skill": skill, "plan_id": p["id"], "day": p["day"], "saving": s,
                               "constraints_understood": p["constraints"].get("notes", []),
                               "moves": moves, "applied": False,
                               "solver": f"{sv.get('solver', 'solver')}: {sv.get('variables', '?')} variables, "
                                         f"{sv.get('constraints', '?')} constraints, {sv.get('ms', '?')} ms",
                               "twin": f"{_fmt(p['baseline']['kwh'])} kWh today's setup vs {_fmt(p['plan']['kwh'])} kWh planned",
                               "explained_by": ex.get("source", "")}
    if skill == "campus_status":
        b = api.brief()
        k = analytics.kpis(get_store())
        top = [a["title"] for a in b["actions"][:3]]
        reply = (f"{b['brief'].get('headline', '')}\n\nLast 7 days: RM {_fmt(k['wasted_rm'])} "
                 f"({_fmt(k['wasted_share'] * 100)}% of cooling) went to empty rooms; {k['stale_air_hours']} "
                 f"occupied room-hours had stale air; data confidence {k['fleet_confidence']}/100.\n\n"
                 "Top changes:\n" + "\n".join(f"- {t}" for t in top))
        return reply, {"skill": skill, "kpis": k, "top_changes": top, "brief_source": b["brief"].get("source", "")}
    r = ask(get_store(), text)
    reply = r.get("answer") or "No answer."
    if r.get("sql"):
        reply += f"\n\nSQL used:\n{r['sql']}"
    return reply, {"skill": "ask_campus_data", "sql": r.get("sql"), "rows": (r.get("rows") or [])[:20],
                   "source": r.get("source"), "attempts": r.get("attempts") or []}


@router.post("/a2a")
async def a2a_rpc(request: Request):
    if settings.a2a_key:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, settings.a2a_key):
            return _error(None, -32001, "unauthorised: send Authorization: Bearer <key>", status=401)
    try:
        body = await request.json()
    except Exception:
        return _error(None, -32700, "parse error: body is not JSON")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return _error(body.get("id") if isinstance(body, dict) else None, -32600, "invalid JSON-RPC 2.0 request")
    rpc_id, method, params = body.get("id"), body["method"], body.get("params") or {}
    if method in ("tasks/get", "tasks/cancel"):
        return _error(rpc_id, -32001, "task not found: CampusPulse answers each message directly")
    if method != "message/send":
        return _error(rpc_id, -32601, f"method not supported: {method} (use message/send)")
    msg = params.get("message") or {}
    text = " ".join(p.get("text", "") for p in msg.get("parts", [])
                    if p.get("kind", p.get("type")) == "text").strip()
    if not text:
        return _error(rpc_id, -32602, "message needs at least one text part")
    hint = (msg.get("metadata") or {}).get("skill") or (params.get("metadata") or {}).get("skill")
    try:
        if hint in {s["id"] for s in SKILLS}:  # the caller named a skill: run it directly
            skill = hint
            reply, data = await run_in_threadpool(run_skill, skill, text[:500])
        else:  # otherwise the coordinator agent decides which specialists to call, and in what order
            from .agent.team import coordinate
            run = await run_in_threadpool(coordinate, text, run_skill, route, "Gemini Enterprise (A2A)")
            skill, reply = run["skill"], run["reply"]
            data = run["data"] | {"skills_called": run["skills"], "coordinated_by": run["source"],
                                  "steps": [{k: s.get(k) for k in ("agent", "did", "ok")} for s in run["steps"]]}
    except Exception as e:  # keep the protocol answer well-formed whatever happens inside
        return _error(rpc_id, -32603, f"internal error: {type(e).__name__}")
    return {"jsonrpc": "2.0", "id": rpc_id, "result": {
        "kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
        "contextId": msg.get("contextId") or str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": reply}, {"kind": "data", "data": data}],
        "metadata": {"skill": skill}}}

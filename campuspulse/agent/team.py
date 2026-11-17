"""The CampusPulse agent team: who does what, how they hand over, and who checks whom.

Patterns, in the vocabulary of Google's multi-agent guide:

  coordinator        one Gemini agent reads a request and decides which specialists to call, in order
  sequential         Autopilot: goal reader -> optimiser -> digital twin -> explainer -> notice writer
  iterative          the SQL analyst reads its own query errors and fixes them (gemini.ask)
  generator + critic every figure Gemini writes is checked against the engine's output; a failed
                     check goes back once with the reason, then falls back to the template
  human in the loop  safe changes apply, risky ones wait for a person (autopilot.py, api.py)

Nothing here produces a number. Agents read numbers from tools and the checker enforces it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone

from ..config import settings

log = logging.getLogger("campuspulse.team")

# --- the checker (critic) -------------------------------------------------------------------

NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b")
FREE_BELOW = 13  # counts and ordinals up to a dozen ("3 sentences", "two rooms" written as 2) pass unchecked


def _numbers(text: str) -> list[float]:
    out = []
    for m in NUM.findall(CLOCK.sub(" ", text or "")):
        try:
            out.append(abs(float(m.replace(",", ""))))
        except ValueError:
            pass
    return out


def _known(payload) -> set[float]:
    """Every number in the facts an agent was given: numeric values and numbers inside strings."""
    vals: set[float] = set()

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            vals.add(abs(float(v)))
        elif isinstance(v, str):
            vals.update(_numbers(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)

    walk(payload)
    return vals


def check_numbers(text: str, facts) -> dict:
    """The critic: is every figure in `text` one the engine supplied (allowing rounding)?

    Returns {"ok": bool, "checked": n, "unknown": [..]}. A figure passes if it equals a known value,
    equals it after rounding to a whole number or one decimal, or is a known fraction shown as a percentage.
    """
    known = _known(facts)
    ok_vals = set(known)
    for k in known:
        ok_vals.update({round(k), round(k, 1), round(k * 100), round(k * 100, 1)})
    unknown, checked = [], 0
    for n in _numbers(text):
        if n < FREE_BELOW:
            continue
        checked += 1
        if n in ok_vals or any(abs(n - k) <= max(0.5, 0.005 * k) for k in known):
            continue
        unknown.append(n)
    return {"ok": not unknown, "checked": checked, "unknown": [f"{u:g}" for u in unknown[:5]]}


def checked_write(generate, facts, texts=lambda out: json.dumps(out, default=str)) -> tuple[dict | None, dict]:
    """Generator + critic loop. `generate(feedback)` returns the agent's draft (a dict); the checker
    reads it; one rejected draft goes back with the reason. Returns (draft or None, verdict)."""
    feedback, verdict = None, {"ok": False, "checked": 0, "unknown": [], "rounds": 0}
    for round_no in (1, 2):
        draft = generate(feedback)
        verdict = check_numbers(texts(draft), facts) | {"rounds": round_no}
        if verdict["ok"]:
            return draft, verdict
        feedback = ("A checker rejected your draft: these figures are not in the input: "
                    + ", ".join(verdict["unknown"]) + ". Rewrite using only figures from the input, copied exactly.")
        log.info("checker sent a draft back: %s", verdict["unknown"])
    return None, verdict


def verdict_note(v: dict) -> str:
    if v.get("ok"):
        again = " after one rewrite" if v.get("rounds", 1) > 1 else ""
        return f"{v['checked']} figures checked against the engine{again}"
    return "checker rejected figures " + ", ".join(v.get("unknown", []))


# --- the orchestrator -----------------------------------------------------------------------

ORCH_SYSTEM = """You are the Orchestrator of CampusPulse, a team of AI agents that runs a university's rooms and
air-conditioning (Asia Pacific University, Kuala Lumpur). You do not do the work yourself: you direct a team of
specialist agents, decide who works on what and in what order, pass each one what the previous one found, and
combine their answers. You never state a figure from memory.

Your agents (call them as tools, with a clear task):
- investigator: investigates one room (live state, today's classes, sensor trust, 7-day history) and recommends a fix.
- sql_analyst: answers any factual question from the data warehouse with SQL.
- brief_writer: this week's key figures and the top recommended changes.
- planner: plans tomorrow's rooms and AC (goal reader, optimiser, digital twin, explainer). Pass only the
  person's goals or limits in their own words, or an empty string. It only proposes.
- controller: the only agent that can change a room's AC mode. Give it the room and an instruction with the
  evidence. A policy guard (rules) applies safe changes at once and holds the rest for a person.
- verifier: after the controller acted, re-checks the room (rules) to see whether it worked.
- reporter: writes the note for the activity log from the facts. Call it last on an event.
Your own tools: recent_incidents (what the team handled), close_incident (finish an event).

A REQUEST from a person: call the agents the request needs, in order, briefing each with the previous result.
Reply in plain English, at most 120 words, leading with the answer.
An EVENT from the watcher: 1) investigator on the room. 2) If its findings show waste that is safe to fix (AC on,
nobody in, no class starting within 15 minutes; a booked class with nobody in is a no-show), controller with a
clear instruction and the evidence; then verifier. If people are in and CO2 is high, or the sensor can't be
trusted, do not change the AC: the reporter's note will flag it for Estates. 3) reporter. 4) close_incident
("acted" while a check is pending, "awaiting_approval" if the guard held it, "escalated" if a person must act,
"no_action" if nothing was needed).
A VERIFICATION: passed -> reporter, then close_incident "resolved". Failed or no data -> reporter, then
close_incident "escalated".
Use only figures that appear in your agents' results, copied exactly. Never claim something an agent did not
confirm. Never mention tool names to people."""

COORD_SYSTEM = ORCH_SYSTEM

_str = {"type": "string"}
COORD_TOOLS = [
    {"name": "investigator", "description": "Investigator agent: investigates one room and recommends the least disruptive fix.",
     "parameters": {"type": "object", "properties": {"room_id": _str, "question": _str}, "required": ["room_id"]}},
    {"name": "sql_analyst", "description": "SQL analyst agent: answers a factual question from the warehouse; returns the answer and its SQL.",
     "parameters": {"type": "object", "properties": {"question": _str}, "required": ["question"]}},
    {"name": "brief_writer", "description": "Brief writer agent: this week's key figures and the top recommended changes.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "planner", "description": "Planner agent: optimises tomorrow's rooms and AC and reports the saving. Proposes only.",
     "parameters": {"type": "object", "properties": {"goals": {"type": "string", "description": "The person's goals, or empty."}}}},
    {"name": "controller", "description": "Controller agent: asks for a room's AC mode to change; the policy guard applies or holds it.",
     "parameters": {"type": "object", "properties": {"room_id": _str, "instruction": {"type": "string", "description": "What to do and why, with the evidence."}},
                    "required": ["room_id", "instruction"]}},
    {"name": "verifier", "description": "Verifier (rules): re-checks the room shortly after an action.",
     "parameters": {"type": "object", "properties": {"room_id": _str, "expect": {"type": "string", "enum": ["ac_off", "co2_below_limit"]}},
                    "required": ["room_id", "expect"]}},
    {"name": "reporter", "description": "Reporter agent: writes the activity-log note from the facts so far.",
     "parameters": {"type": "object", "properties": {"focus": {"type": "string", "description": "Anything the note must cover."}}}},
    {"name": "recent_incidents", "description": "What the team handled recently, newest first.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "close_incident", "description": "Finish the current event.",
     "parameters": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": ["resolved", "escalated", "no_action", "acted", "awaiting_approval"]}},
         "required": ["outcome"]}},
]
# Agents backed by the app's existing skills (a2a.run_skill), under their team names.
SKILL_OF = {"sql_analyst": "ask_campus_data", "brief_writer": "campus_status", "planner": "plan_tomorrow"}
SKILL_ALIASES = {v: k for k, v in SKILL_OF.items()}  # older callers and tests use the skill names
SPECIALIST = {"sql_analyst": "SQL analyst", "brief_writer": "Brief writer", "planner": "Planner",
              "investigator": "Investigator", "controller": "Controller", "reporter": "Reporter", "verifier": "Verifier"}
AGENT_NAMES = set(SPECIALIST)
MAX_STEPS = 8
RUNS: deque = deque(maxlen=20)
ORCH = "Orchestrator"


def _substeps(skill: str, data: dict) -> list[dict]:
    """What happened inside a skill-backed agent, from what it returned (shown in the trace)."""
    out = []
    if skill == "ask_campus_data":
        for a in data.get("attempts") or []:
            out.append({"agent": "SQL analyst", "did": "ran SQL through the read-only guard" if a.get("ok")
                        else "query failed; read the error and rewrote it",
                        "detail": (a.get("sql") or "")[:300], "ok": bool(a.get("ok"))})
    elif skill == "plan_tomorrow":
        out.append({"agent": "Goal reader", "did": "turned the goals into solver constraints",
                    "detail": "; ".join(data.get("constraints_understood") or []) or "no extra limits", "ok": True})
        if data.get("solver"):
            out.append({"agent": "Optimiser (not an LLM)", "did": "solved the room assignment", "detail": data["solver"], "ok": True})
        out.append({"agent": "Digital twin (not an LLM)", "did": "compared today's setup with the plan",
                    "detail": data.get("twin", ""), "ok": True})
        if data.get("explained_by"):
            out.append({"agent": "Plan explainer + checker", "did": "wrote the summary; figures checked",
                        "detail": data["explained_by"], "ok": True})
    elif skill == "campus_status":
        out.append({"agent": "Brief writer + checker", "did": "wrote the brief from the engine's figures",
                    "detail": data.get("brief_source", ""), "ok": True})
    return out


def _record(run: dict) -> dict:
    run["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RUNS.appendleft({k: run[k] for k in ("at", "message", "reply", "steps", "source", "via", "ms")})
    return run


def coordinate(message: str, run_skill, route, via: str = "app", incident: dict | None = None) -> dict:
    """Run the orchestrator on one input: a person's request, or (with `incident`) a watcher event or a
    verification result. It delegates to agents; `run_skill(skill, text) -> (reply, data)` runs the
    skill-backed ones (SQL analyst, brief writer, planner); `route(text)` is the keyword fallback."""
    from ..ops import ops
    from . import agents
    message = (message or "").strip()[:1500]
    t0 = time.time()
    steps: list[dict] = []
    results: list[tuple[str, str, dict]] = []
    closing: dict = {}
    by = f"Orchestrator ({via})"
    record = incident if incident is not None else ops.open_request(message, via)

    def log_step(agent: str, did: str, detail: str = "", ok: bool = True, ms: int | None = None) -> None:
        st = {"agent": agent, "did": did, "detail": str(detail)[:400], "ok": ok}
        if ms is not None:
            st["ms"] = ms
        steps.append(st)
        ops.step(record, agent, did, detail, ok)

    def call(name: str, args: dict) -> str:
        name = SKILL_ALIASES.get(name, name)
        t = time.time()
        room = _room_id(args.get("room_id"), incident)
        if name in SKILL_OF:
            skill = SKILL_OF[name]
            text = str(args.get("question") or args.get("goals") or ("" if name == "planner" else message))
            log_step(ORCH, f"delegated to the {SPECIALIST[name]}", text[:200])
            try:
                reply, data = run_skill(skill, text)
                ok = True
            except Exception as e:  # an agent failing must not take the run down
                reply, data, ok = f"{SPECIALIST[name]} failed: {type(e).__name__}", {}, False
            for sub in _substeps(skill, data):
                log_step(sub["agent"], sub["did"], sub.get("detail", ""), sub.get("ok", True))
            log_step(SPECIALIST[name], "reported back", reply[:300], ok, round((time.time() - t) * 1000))
        elif name == "investigator":
            q = str(args.get("question") or "What is happening in this room, and what should we do?")
            log_step(ORCH, f"delegated to the Investigator: {room}", q[:200])
            reply, data = agents.investigator(room, q, log_step)
        elif name == "controller":
            ins = str(args.get("instruction") or "")
            log_step(ORCH, f"delegated to the Controller: {room}", ins[:200])
            reply, data = agents.controller(room, ins, incident, log_step, by)
        elif name == "verifier":
            log_step(ORCH, f"asked the Verifier to check {room}", str(args.get("expect") or ""))
            data = ops.schedule_check(incident, room, str(args.get("expect") or ""))
            reply = json.dumps(data, default=str)
        elif name == "reporter":
            log_step(ORCH, "asked the Reporter for the log note", str(args.get("focus") or ""))
            facts = {k: (incident or {}).get(k) for k in ("room_id", "room_name", "what", "source", "proposal", "check", "flag")}
            facts["findings"] = [r[1] for r in results if r[0] in ("investigator", "controller")][-2:]
            facts["focus"] = args.get("focus")
            reply = agents.reporter(facts, log_step)
            data = {"note": reply}
            closing["note"] = reply
        elif name == "recent_incidents":
            data = {"incidents": ops.recent_incidents()}
            reply = json.dumps(data, default=str)
            log_step(ORCH, "read the team's recent incidents", f"{len(data['incidents'])} incidents")
        elif name == "close_incident":
            data = {"outcome": args.get("outcome")}
            reply = json.dumps({"ok": incident is not None})
            if incident is not None:
                closing["outcome"] = args.get("outcome")
        else:
            reply, data = f"unknown agent {name}", {}
        results.append((name, reply, data))
        return reply

    def finish(reply: str, source: str) -> dict:
        data = next((r[2] for r in reversed(results) if r[0] in SKILL_OF), results[-1][2] if results else {})
        run = _record({"message": message, "reply": reply, "steps": steps, "source": source, "via": via,
                       "skill": next((SKILL_OF[r[0]] for r in reversed(results) if r[0] in SKILL_OF), None),
                       "skills": [SKILL_OF[r[0]] for r in results if r[0] in SKILL_OF],
                       "tools": [r[0] for r in results], "data": data, "ms": round((time.time() - t0) * 1000),
                       "activity": record["id"]})
        if incident is not None:
            _close_from_run(incident, closing, reply, source)
        else:
            ops.finish_request(record, reply, source)
        return run

    def fallback(why: str) -> dict:
        if incident is not None:
            return finish(_playbook(incident, call), why)
        if not results:  # keyword router: the person's own words go to the agent it picks
            call(SKILL_ALIASES.get(route(message), "brief_writer"), {"question": message, "goals": message})
        return finish("\n\n".join(r[1] for r in results if r[0] in SKILL_OF or r[0] == "investigator"), why)

    if not settings.gemini_enabled:
        return fallback("rule playbook (Gemini is off)" if incident else "keyword router (Gemini is off)")
    try:
        from google.genai import types
        from .gemini import _client, _generate
        cfg = types.GenerateContentConfig(
            system_instruction=ORCH_SYSTEM, temperature=0.1,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
                for t in COORD_TOOLS])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        contents = [types.Content(role="user", parts=[types.Part(text=message)])]
        for _ in range(MAX_STEPS):
            resp = _generate(_client(), retries=2, model=settings.gemini_model, contents=contents, config=cfg)
            calls = resp.function_calls or []
            if not calls:
                text = (resp.text or "").strip() or closing.get("note", "")
                if not results:  # the orchestrator must delegate; it may not answer on its own
                    return fallback("keyword router (orchestrator answered without an agent)")
                verdict = check_numbers(text, [r[1] for r in results] + [r[2] for r in results])
                log_step("Checker", "checked the orchestrator's words against the agents' results",
                         verdict_note(verdict), verdict["ok"])
                if not text or not verdict["ok"]:
                    return finish(closing.get("note") or "\n\n".join(r[1] for r in results if r[0] in SKILL_OF or r[0] == "investigator")
                                  or _plain_summary(incident), f"Gemini orchestrator ({settings.gemini_model}); agents' own words used")
                return finish(text, f"Gemini orchestrator ({settings.gemini_model})")
            contents.append(resp.candidates[0].content)
            parts = []
            for c in calls:
                out = call(c.name, dict(c.args or {}))
                parts.append(types.Part.from_function_response(name=c.name, response={"result": out[:3000]}))
            contents.append(types.Content(role="user", parts=parts))
            if incident is not None and closing.get("outcome"):
                return finish(closing.get("note") or _plain_summary(incident), f"Gemini orchestrator ({settings.gemini_model})")
        return finish(closing.get("note") or "\n\n".join(r[1] for r in results if r[0] in SKILL_OF) or _plain_summary(incident),
                      f"Gemini orchestrator ({settings.gemini_model}); step limit")
    except Exception as e:
        log.warning("orchestrator failed: %s", e)
        from .gemini import _why
        return fallback(f"{'rule playbook' if incident else 'keyword router'} (Gemini error: {_why(e)})")


def _room_id(raw, incident: dict | None) -> str:
    """Accept 'b-08-05', 'Room B-08-05' or a device id like 'sim-b-08-05'; on an event, default to its room."""
    from ..campus import ROOM_BY_ID
    r = str(raw or "").strip().upper().replace("ROOM ", "")
    for prefix in ("SIM-", "ESP32-", "WOKWI-", "VIRTUAL-"):
        if r.startswith(prefix) and r[len(prefix):] in ROOM_BY_ID:
            r = r[len(prefix):]
    if r not in ROOM_BY_ID and incident is not None:
        return incident["room_id"]
    return r


def _plain_summary(incident: dict | None) -> str:
    if not incident:
        return ""
    p = incident.get("proposal") or {}
    if p.get("applied_at"):
        return f"{incident['room_id']}: {incident['what']}. AC set to '{p['mode']}' by the policy guard."
    if p.get("held_because"):
        return f"{incident['room_id']}: {incident['what']}. Change to '{p['mode']}' {p['held_because']}."
    return f"{incident['room_id']}: {incident['what']}."


def _close_from_run(incident: dict, closing: dict, reply: str, source: str) -> None:
    from ..ops import ops
    outcome = closing.get("outcome")
    status = incident["status"]
    pending = (incident.get("check") or {}).get("result", "x") is None
    if outcome in ("resolved", "escalated", "no_action") and not pending:
        final = outcome
    elif status in ("awaiting_approval", "verifying", "acted"):
        final = status  # still in flight: a person or the verifier has the next move
    else:
        final = outcome or "no_action"
    if final == "acted" and incident.get("check") is None:
        final = "resolved"
    with ops.lock:
        incident["note"] = reply
        incident["orchestrated_by"] = source
    ops.step(incident, ORCH, f"closed this round: {final.replace('_', ' ')}", "")
    ops.set_status(incident, final)


def _playbook(incident: dict, call) -> str:
    """No Gemini: the orchestrator's playbook, same agents in a fixed order (so the loop still closes)."""
    room = incident["room_id"]
    check = incident.get("check") or {}
    if check.get("result"):
        note = call("reporter", {})
        call("close_incident", {"outcome": "resolved" if check["result"] == "passed" else "escalated"})
        return note
    call("investigator", {"room_id": room})
    info = incident.get("_info") or {}
    from ..ops import ops
    info = ops.inspect_room(room)
    if incident["event"] == "empty_cooled":
        soon = (not info.get("class_now") and info.get("minutes_to_next_class") is not None
                and info["minutes_to_next_class"] < 15)
        if not soon:
            why = (f"no-show: {info['class_now']} is booked but nobody is in" if info.get("class_now")
                   else "AC running with nobody in and no class right now")
            out = call("controller", {"room_id": room, "instruction": f"Switch to occupancy mode: {why}."})
            if incident.get("proposal", {}).get("applied_at"):
                call("verifier", {"room_id": room, "expect": "ac_off"})
                note = call("reporter", {})
                call("close_incident", {"outcome": "acted"})
                return note
            note = call("reporter", {})
            call("close_incident", {"outcome": "awaiting_approval" if incident["status"] == "awaiting_approval" else "no_action"})
            return note or out
        note = call("reporter", {"focus": "a class starts soon, so the AC was left on for pre-cooling"})
        call("close_incident", {"outcome": "no_action"})
        return note
    ops.flag_for_estates(incident, room, incident["what"])
    note = call("reporter", {"focus": "flag for Estates: people are in and CO2 is high"})
    call("close_incident", {"outcome": "escalated"})
    return note


def event_message(incident: dict, kind: str) -> str:
    """What the orchestrator is told about an event or a verification result."""
    if kind == "verification":
        c = incident.get("check") or {}
        return (f"VERIFICATION for incident {incident['id']} in {incident['room_id']} ({incident['room_name']}).\n"
                f"Original event: {incident['what']}.\nAction taken: {json.dumps(incident.get('proposal'))}.\n"
                f"Verifier result: {c.get('result')} ({c.get('seen', '')}).")
    return (f"EVENT {incident['id']} from the watcher.\nroom_id: {incident['room_id']} "
            f"({incident['room_name']}, {incident['source']}).\nProblem: {incident['what']}.\n"
            f"Direct your agents: investigate, act if it is safe and useful, verify, report, and close.")


def handle_trigger(trigger: dict, run_skill, route) -> dict | None:
    """Entry point for the operations worker: one event or verification -> one orchestrator run."""
    from ..ops import ops
    inc = ops.get(trigger.get("incident", ""))
    if inc is None:
        return None
    kind = trigger.get("kind", "event")
    return coordinate(event_message(inc, kind), run_skill, route,
                      via="Watcher" if kind == "event" else "Verifier", incident=inc)


# --- the roster shown in the app -------------------------------------------------------------

def _agents():
    from . import agents
    return agents


def roster() -> list[dict]:
    from . import gemini as g
    llm = f"Gemini ({settings.gemini_model})" if settings.gemini_enabled else "Gemini (off: templates in use)"
    return [
        {"id": "orchestrator", "name": "Orchestrator", "kind": "llm", "model": llm, "pattern": "Orchestrator (hub)",
         "job": "Directs the team: for every request and every watcher event it decides which agents work, in what "
                "order, briefs each with the last one's result, and closes the loop.",
         "tools": [t["name"] for t in COORD_TOOLS],
         "guardrail": "Must delegate; only the Controller can act, through the policy guard; its words go to the checker.",
         "prompt": ORCH_SYSTEM},
        {"id": "investigator", "name": "Investigator", "kind": "llm", "model": llm, "pattern": "Specialist (tools)",
         "job": "Investigates one room for the orchestrator: live state, today's classes, sensor trust, 7-day history; recommends a fix.",
         "tools": ["inspect_room", "room_history"], "guardrail": "Findings go to the checker; a rule summary stands in on failure.",
         "prompt": _agents().INVESTIGATOR_SYSTEM},
        {"id": "controller", "name": "Controller", "kind": "llm", "model": llm, "pattern": "Specialist (acts)",
         "job": "The only agent that can ask for an AC change; reports exactly what the policy guard decided.",
         "tools": ["set_ac_mode"], "guardrail": "Every request passes the policy guard; it cannot apply anything itself.",
         "prompt": _agents().CONTROLLER_SYSTEM},
        {"id": "reporter", "name": "Reporter", "kind": "llm", "model": llm, "pattern": "Generator + critic",
         "job": "Writes the activity-log note for each incident from the facts.",
         "tools": [], "guardrail": "Checked against the facts; a template stands in on failure.", "prompt": _agents().REPORTER_SYSTEM},
        {"id": "watcher", "name": "Watcher", "kind": "rule", "model": "Rules on every reading (no LLM)", "pattern": "Event trigger",
         "job": "Reads every sensor message and opens an incident when a room looks wrong: AC on with nobody in, "
                "CO2 over the limit while occupied.",
         "tools": [], "guardrail": "Condition must hold (20 s on a live node); one open incident per room; rate-limited.",
         "prompt": None},
        {"id": "guard", "name": "Policy guard", "kind": "rule", "model": "Rules (no LLM)", "pattern": "Human in the loop",
         "job": "Decides whether the orchestrator's change happens now or waits for a person.",
         "tools": [], "guardrail": "Less cooling only when the room is empty, its sensor trusted and under the daily "
                                  "limit; more cooling always allowed.", "prompt": None},
        {"id": "verifier", "name": "Verifier", "kind": "rule", "model": "Rules (no LLM)", "pattern": "Closed loop",
         "job": "Re-reads the room after an action and reports whether it worked, so the orchestrator cannot mark its own homework.",
         "tools": [], "guardrail": "Uses the room's next real reading; gives up and escalates after 15 minutes.", "prompt": None},
        {"id": "goal_reader", "name": "Goal reader", "kind": "llm", "model": llm, "pattern": "Sequential (step 1)",
         "job": "Turns the manager's plain-English goals into solver constraints.",
         "tools": ["set_constraints"], "guardrail": "One function call with a fixed schema; a rule-based reader takes over on failure.",
         "prompt": g.PLAN_SYSTEM},
        {"id": "optimiser", "name": "Optimiser", "kind": "solver", "model": "Mixed-integer program (SciPy HiGHS)",
         "pattern": "Sequential (step 2)", "job": "Assigns every class a room and every room an AC schedule, for least energy within the constraints.",
         "tools": [], "guardrail": "Deterministic and proven optimal; this is where plan figures come from.", "prompt": None},
        {"id": "twin", "name": "Digital twin", "kind": "solver", "model": "Simulator physics (CO2 mass balance, thermal model)",
         "pattern": "Sequential (step 3)", "job": "Runs today's setup and the plan through the same physics and reports kWh, RM, CO2e and stale-air hours.",
         "tools": [], "guardrail": "Same model for both sides, so the comparison is like for like.", "prompt": None},
        {"id": "explainer", "name": "Plan explainer", "kind": "llm", "model": llm, "pattern": "Sequential (step 4) + generator",
         "job": "Explains the plan to the manager: what changes, what is automatic, what needs approval.",
         "tools": [], "guardrail": "Draft goes to the checker; one rewrite, then the template.", "prompt": g.EXPLAIN_SYSTEM},
        {"id": "notices", "name": "Notice writer", "kind": "llm", "model": llm, "pattern": "Sequential (step 5)",
         "job": "Drafts the room-change notices for students and lecturers once a plan is approved.",
         "tools": [], "guardrail": "Uses only the facts of each move; a template stands in on failure.", "prompt": g.NOTICE_SYSTEM},
        {"id": "sql", "name": "SQL analyst", "kind": "llm", "model": llm, "pattern": "Iterative refinement",
         "job": "Answers questions by writing BigQuery SQL, reading errors and fixing its own query.",
         "tools": ["run_sql"], "guardrail": "A read-only guard allows one SELECT over curated views; a query budget stops loops.",
         "prompt": g.ASK_SYSTEM},
        {"id": "brief", "name": "Brief writer", "kind": "llm", "model": llm, "pattern": "Generator",
         "job": "Writes the morning brief from actions the decision engine has already costed.",
         "tools": [], "guardrail": "Draft goes to the checker; one rewrite, then the template.", "prompt": g.BRIEF_SYSTEM},
        {"id": "captions", "name": "Caption writer", "kind": "llm", "model": llm, "pattern": "Generator",
         "job": "Writes a one-line caption for each evidence chart from the chart's own numbers.",
         "tools": [], "guardrail": "Sees only the chart's data.", "prompt": g.CAPTION_SYSTEM},
        {"id": "checker", "name": "Checker", "kind": "rule", "model": "Deterministic check (no LLM)", "pattern": "Critic",
         "job": "Reads everything the writers produce and rejects any figure the engine did not supply.",
         "tools": [], "guardrail": "Cannot be talked round: it compares numbers, it does not read prose.", "prompt": None},
    ]

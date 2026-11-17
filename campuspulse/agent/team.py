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

ORCH_SYSTEM = """You are the orchestrator of CampusPulse, a team of AI agents and tools that runs a university's
rooms and air-conditioning (Asia Pacific University, Kuala Lumpur). Everything goes through you: questions
from people, and events from the sensors. You never answer from memory and never calculate or estimate a
figure: you decide which specialist or tool to call, in what order, and you combine what they return.

Specialist agents (tools):
- ask_campus_data: the SQL analyst. Facts from the warehouse: rooms, energy, CO2, occupancy, sensor health.
- campus_status: the brief writer. This week's key figures and the top recommended changes.
- plan_tomorrow: the planner (goal reader -> optimiser -> digital twin -> explainer). Pass only the person's
  goals or limits in their own words, or an empty string. Never pass a question. It only proposes.
Operations tools:
- inspect_room: one room now: occupancy, CO2, AC, its AC mode, today's classes, sensor trust, last 7 days.
- recent_incidents: what you have handled recently.
- set_ac_mode: ask to change a room's AC mode ("occupancy" = cool only while people are in or a class is
  booked, with pre-cooling; "after_hours_off"; "schedule" = the building's fixed hours). A policy guard
  decides: safe changes apply at once, others wait for a person. Report what the guard said, honestly.
- flag_for_estates: put something in front of a person (a stuffy class, a broken sensor, a held change).
- schedule_check: after you act, the verifier re-reads the room to see whether it worked.
- close_incident: finish an event with an outcome and a one-sentence note.

When the input is a REQUEST from a person: call at least one tool; for a two-part request call them in
order and use the first result to brief the second. Reply in plain English, at most 120 words, lead with
the answer.
When the input is an EVENT: investigate before acting (inspect_room first). Choose the least disruptive
action that fixes it. AC running in an empty room: read class_now and next_class. A class booked now but
nobody there is a no-show: say so, ask for "occupancy" mode, then schedule_check. No class now and none
starting within 15 minutes: ask for "occupancy" mode (it still pre-cools before booked classes), then
schedule_check. A class starting within 15 minutes: leave the AC on (pre-cooling) and close with no_action. CO2 over the limit in an occupied room: you cannot open windows or move a class now;
flag_for_estates with the facts (and, if it repeats, say a room move is worth planning). A sensor you cannot
trust: flag it, do not act on it. Then close_incident.
When the input is a VERIFICATION: read the verifier's result. Passed: close_incident "resolved". Failed or
no data: flag_for_estates and close_incident "escalated".
Always: use only figures that appear in tool results, copied exactly. Never claim something happened that a
tool did not confirm. Never mention tool or function names in your words to people."""

COORD_SYSTEM = ORCH_SYSTEM  # the name the roster and older callers use

_str = {"type": "string"}
COORD_TOOLS = [
    {"name": "ask_campus_data",
     "description": "SQL analyst: answers a factual question about rooms, energy, CO2, occupancy or sensor "
                    "health by querying the warehouse. Returns the answer and the SQL it ran.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string", "description": "One clear question in plain English."}},
         "required": ["question"]}},
    {"name": "campus_status",
     "description": "Brief writer: this week's key figures (energy and RM spent cooling empty rooms, stale-air "
                    "hours, data confidence) and the top recommended changes.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "plan_tomorrow",
     "description": "Planner: optimises the next teaching day's room assignments and AC schedule and reports "
                    "the saving. Proposes only; nothing is applied.",
     "parameters": {"type": "object", "properties": {
         "goals": {"type": "string", "description": "The person's goals or limits in their own words, or empty."}}}},
    {"name": "inspect_room",
     "description": "One room right now: occupancy, CO2, temperature, AC on/off, its AC mode, today's classes "
                    "(now and next), sensor trust, and empty-room cooling over the last 7 days.",
     "parameters": {"type": "object", "properties": {"room_id": {"type": "string", "description": "e.g. B-08-05, AUD-2, TL4-02"}},
                    "required": ["room_id"]}},
    {"name": "recent_incidents", "description": "The incidents you handled most recently, newest first.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "set_ac_mode",
     "description": "Ask for a room's AC mode to change. A policy guard applies safe, reversible changes at once "
                    "and holds the rest for a person. Returns whether it was applied or held, and why.",
     "parameters": {"type": "object", "properties": {
         "room_id": _str, "mode": {"type": "string", "enum": ["occupancy", "after_hours_off", "schedule"]},
         "reason": {"type": "string", "description": "One sentence of evidence for the person reading the log."}},
         "required": ["room_id", "mode", "reason"]}},
    {"name": "flag_for_estates", "description": "Put an issue in front of a person on the Estates team.",
     "parameters": {"type": "object", "properties": {"room_id": _str, "message": _str}, "required": ["room_id", "message"]}},
    {"name": "schedule_check",
     "description": "After acting on an event: the verifier re-reads the room shortly and reports whether it worked.",
     "parameters": {"type": "object", "properties": {
         "room_id": _str, "expect": {"type": "string", "enum": ["ac_off", "co2_below_limit"]}},
         "required": ["room_id", "expect"]}},
    {"name": "close_incident", "description": "Finish the current event.",
     "parameters": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": ["resolved", "escalated", "no_action", "acted", "awaiting_approval"]},
         "note": {"type": "string", "description": "One or two sentences for the activity log, figures from tools only."}},
         "required": ["outcome", "note"]}},
]
SPECIALIST = {"ask_campus_data": "SQL analyst", "campus_status": "Brief writer", "plan_tomorrow": "Planner"}
OPS_TOOLS = {"inspect_room", "recent_incidents", "set_ac_mode", "flag_for_estates", "schedule_check", "close_incident"}
TOOL_LABEL = {"inspect_room": "inspected", "recent_incidents": "read recent incidents", "set_ac_mode": "asked to set the AC mode",
              "flag_for_estates": "flagged for Estates", "schedule_check": "asked the verifier to check",
              "close_incident": "closed the incident"}
MAX_STEPS = 6
RUNS: deque = deque(maxlen=20)
ORCH = "Orchestrator"


def _substeps(skill: str, data: dict) -> list[dict]:
    """What happened inside a specialist, from what it returned (shown in the trace)."""
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
            out.append({"agent": "Optimiser (not an LLM)", "did": "solved the room assignment",
                        "detail": data["solver"], "ok": True})
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


def _ops_tool(name: str, args: dict, incident: dict | None, by: str) -> tuple[str, dict]:
    """Run one operations tool for the orchestrator; every effect goes through ops (guard, verifier)."""
    from ..ops import ops
    room = str(args.get("room_id") or (incident or {}).get("room_id") or "").strip().upper()
    if name == "inspect_room":
        data = ops.inspect_room(room)
        if incident and "error" not in data:
            ops.step(incident, ORCH, f"inspected {room}", ", ".join(
                f"{k.replace('_', ' ')} {v}" for k, v in data.items()
                if k in ("occupied", "ac_on", "co2_ppm", "class_now", "next_class", "data_confidence", "ac_mode",
                         "empty_cooled_hours_7d") and v is not None))
        return json.dumps(data, default=str), data
    if name == "recent_incidents":
        data = {"incidents": ops.recent_incidents()}
        return json.dumps(data, default=str), data
    if name == "set_ac_mode":
        if incident:
            ops.step(incident, ORCH, f"decided: AC in {room} to '{args.get('mode')}'", args.get("reason", ""))
        data = ops.set_ac_mode(incident, room, str(args.get("mode")), str(args.get("reason") or ""), by)
        return json.dumps(data, default=str), data
    if name == "flag_for_estates":
        data = ops.flag_for_estates(incident, room, str(args.get("message") or ""))
        return json.dumps(data), data
    if name == "schedule_check":
        data = ops.schedule_check(incident, room, str(args.get("expect") or ""))
        return json.dumps(data, default=str), data
    if name == "close_incident":
        if incident is None:
            return json.dumps({"closed": False, "why": "no incident is open in this run"}), {}
        return json.dumps({"ok": True}), {"outcome": args.get("outcome"), "note": args.get("note")}
    return json.dumps({"error": f"unknown tool {name}"}), {}


def coordinate(message: str, run_skill, route, via: str = "app", incident: dict | None = None) -> dict:
    """Run the orchestrator on one input: a person's request, or (with `incident`) a sensor event or a
    verification result. `run_skill(skill, text) -> (reply, data)` runs a specialist agent; `route(text)`
    is the keyword fallback used when Gemini is off or fails."""
    message = (message or "").strip()[:1500]
    t0 = time.time()
    steps: list[dict] = []
    results: list[tuple[str, str, dict]] = []
    closing: dict = {}
    by = f"Orchestrator ({via})"

    def call(name: str, args: dict) -> str:
        t = time.time()
        if name in SPECIALIST:
            text = str(args.get("question") or args.get("goals") or ("" if name == "plan_tomorrow" else message))
            try:
                reply, data = run_skill(name, text)
                ok = True
            except Exception as e:  # a specialist failing must not take the run down
                reply, data, ok = f"{SPECIALIST[name]} failed: {type(e).__name__}", {}, False
            steps.append({"agent": ORCH, "did": f"called the {SPECIALIST[name]}",
                          "detail": text[:200], "ok": ok, "ms": round((time.time() - t) * 1000)})
            steps.extend(_substeps(name, data))
            if incident:
                from ..ops import ops
                ops.step(incident, ORCH, f"asked the {SPECIALIST[name]}", text[:200], ok)
        elif name in OPS_TOOLS:
            try:
                reply, data = _ops_tool(name, args, incident, by)
                ok = "error" not in data
            except Exception as e:
                log.exception("ops tool %s failed", name)
                reply, data, ok = json.dumps({"error": type(e).__name__}), {}, False
            steps.append({"agent": ORCH, "did": TOOL_LABEL.get(name, name),
                          "detail": json.dumps({k: v for k, v in args.items() if k != "reason"})[:200], "ok": ok,
                          "ms": round((time.time() - t) * 1000)})
            if name == "close_incident" and data:
                closing.update(data)
        else:
            reply, data = f"unknown tool {name}", {}
        results.append((name, reply, data))
        return reply

    def finish(reply: str, source: str) -> dict:
        data = next((r[2] for r in reversed(results) if r[0] in SPECIALIST), results[-1][2] if results else {})
        run = _record({"message": message, "reply": reply, "steps": steps, "source": source, "via": via,
                       "skill": next((r[0] for r in reversed(results) if r[0] in SPECIALIST), None),
                       "skills": [r[0] for r in results if r[0] in SPECIALIST],
                       "tools": [r[0] for r in results], "data": data, "ms": round((time.time() - t0) * 1000)})
        if incident is not None:
            _close_from_run(incident, closing, reply, source)
        return run

    def fallback(why: str) -> dict:
        if incident is not None:
            return finish(_playbook(incident, call), why)
        if not results:  # keyword router: the person's own words go to the specialist it picks
            call(route(message), {"question": message, "goals": message})
        return finish("\n\n".join(r[1] for r in results), why)

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
                    return fallback("keyword router (orchestrator answered without a tool)")
                verdict = check_numbers(text, [r[1] for r in results] + [r[2] for r in results])
                steps.append({"agent": "Checker", "did": "checked the orchestrator's words against the tools' results",
                              "detail": verdict_note(verdict), "ok": verdict["ok"]})
                if not text or not verdict["ok"]:
                    plain = closing.get("note") if incident and closing.get("note") and check_numbers(
                        closing["note"], [r[1] for r in results])["ok"] else None
                    return finish(plain or "\n\n".join(r[1] for r in results if r[0] in SPECIALIST) or _plain_summary(incident),
                                  f"Gemini orchestrator ({settings.gemini_model}); tools' own words used")
                return finish(text, f"Gemini orchestrator ({settings.gemini_model})")
            contents.append(resp.candidates[0].content)
            parts = []
            for c in calls:
                out = call(c.name, dict(c.args or {}))
                parts.append(types.Part.from_function_response(name=c.name, response={"result": out[:3000]}))
            contents.append(types.Content(role="user", parts=parts))
            if closing and incident is not None:
                text = closing.get("note", "")
                verdict = check_numbers(text, [r[1] for r in results])
                steps.append({"agent": "Checker", "did": "checked the incident note against the tools' results",
                              "detail": verdict_note(verdict), "ok": verdict["ok"]})
                return finish(text if verdict["ok"] else _plain_summary(incident),
                              f"Gemini orchestrator ({settings.gemini_model})")
        return finish("\n\n".join(r[1] for r in results if r[0] in SPECIALIST) or _plain_summary(incident),
                      f"Gemini orchestrator ({settings.gemini_model}); step limit")
    except Exception as e:
        log.warning("orchestrator failed: %s", e)
        from .gemini import _why
        return fallback(f"{'rule playbook' if incident else 'keyword router'} (Gemini error: {_why(e)})")


coordinate_request = coordinate


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
        final = "resolved"  # acted with nothing left to verify
    with ops.lock:
        incident["note"] = reply
        incident["orchestrated_by"] = source
    ops.step(incident, ORCH, f"wrote the log note ({final.replace('_', ' ')})", reply[:300])
    ops.set_status(incident, final)


def _playbook(incident: dict, call) -> str:
    """No Gemini: the same tools, in a fixed order (so the loop still closes)."""
    room = incident["room_id"]
    if incident["status"] in ("verifying", "acted") and incident.get("check", {}).get("result"):
        res = incident["check"]["result"]
        if res != "passed":
            call("flag_for_estates", {"room_id": room, "message": f"Check {res}: {incident['check'].get('seen', '')}"})
        call("close_incident", {"outcome": "resolved" if res == "passed" else "escalated",
                                "note": f"{room}: check {res} ({incident['check'].get('seen', '')})."})
        return f"{room}: verifier check {res}. {incident['check'].get('seen', '')}."
    info = json.loads(call("inspect_room", {"room_id": room}))
    if incident["event"] == "empty_cooled":
        soon = (not info.get("class_now") and info.get("minutes_to_next_class") is not None
                and info["minutes_to_next_class"] < 15)
        if soon:
            call("close_incident", {"outcome": "no_action", "note": f"{room}: class starts soon, AC left on."})
            return f"{room}: AC on while empty, but a class starts in {info['minutes_to_next_class']} min; left as is."
        why = (f"no-show: {info['class_now']} is booked but nobody is in" if info.get("class_now")
               else "AC running with nobody in and no class right now")
        out = json.loads(call("set_ac_mode", {"room_id": room, "mode": "occupancy", "reason": why}))
        if out.get("applied"):
            call("schedule_check", {"room_id": room, "expect": "ac_off"})
            return f"{room}: AC was running with nobody in; switched to occupancy mode. Checking it took effect."
        return f"{room}: AC was running with nobody in. {out.get('why', '')}."
    call("flag_for_estates", {"room_id": room, "message": incident["what"]})
    call("close_incident", {"outcome": "escalated", "note": f"{room}: {incident['what']}; flagged for Estates."})
    return f"{room}: {incident['what']}. Flagged for Estates."


def event_message(incident: dict, kind: str) -> str:
    """What the orchestrator is told about an event or a verification result."""
    if kind == "verification":
        c = incident.get("check") or {}
        return (f"VERIFICATION for incident {incident['id']} in {incident['room_id']} ({incident['room_name']}).\n"
                f"Original event: {incident['what']}.\nAction taken: {json.dumps(incident.get('proposal'))}.\n"
                f"Verifier result: {c.get('result')} ({c.get('seen', '')}).")
    return (f"EVENT {incident['id']} from the watcher, {incident['source']} {incident['device_id']}.\n"
            f"Room {incident['room_id']} ({incident['room_name']}): {incident['what']}.\n"
            f"Condition first seen {incident.get('since')}. Investigate, act if it is safe and useful, verify, and close.")


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

def roster() -> list[dict]:
    from . import gemini as g
    llm = f"Gemini ({settings.gemini_model})" if settings.gemini_enabled else "Gemini (off: templates in use)"
    return [
        {"id": "orchestrator", "name": "Orchestrator", "kind": "llm", "model": llm, "pattern": "Orchestrator (hub)",
         "job": "Handles everything: people's requests and the watcher's events. Decides which specialist or tool to "
                "call, in what order, acts through the policy guard, asks the verifier to check, and closes the loop.",
         "tools": [t["name"] for t in COORD_TOOLS],
         "guardrail": "Must use tools; cannot change a room without the policy guard; its words go to the checker.",
         "prompt": ORCH_SYSTEM},
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

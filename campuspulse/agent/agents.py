"""The orchestrator's specialist agents for campus operations.

Each is a Gemini agent with its own prompt and its own tools; the orchestrator (team.py) delegates a
task to one and reads what it returns. Every agent's words go past the checker (no figure that its
tools did not supply), and each has a rule-based fallback, so the loop still closes without Gemini.

  Investigator  tools: inspect_room, room_history          -> findings about one room
  Controller    tools: set_ac_mode (through the policy guard) -> asks for an AC change, reports the guard
  Reporter      no tools                                   -> the incident note people read
"""
from __future__ import annotations

import json
import logging

from ..config import settings

log = logging.getLogger("campuspulse.agents")

INVESTIGATOR_SYSTEM = """You are the Investigator agent in CampusPulse, a team of agents that runs a university's rooms
and air-conditioning. The orchestrator gives you one room and a question. Use your tools to gather the facts
(inspect_room first; room_history when a pattern matters), then reply in 2-3 plain sentences: what is happening,
whether a class is booked now (if one is booked but nobody is in, call it a no-show), when the next class is,
whether the sensor can be trusted, and whether it has happened before. End with one line starting
"Recommend:" naming the least disruptive fix. Use only figures from your tools, copied exactly."""

CONTROLLER_SYSTEM = """You are the Controller agent in CampusPulse. You are the only agent that can ask for a room's AC
mode to change, and you do it with set_ac_mode. Modes: "occupancy" = cool only while people are in or a class
is booked (pre-cools 1 hour before); "after_hours_off" = schedule but never at night or weekends; "schedule" =
the building's fixed hours. To stop cooling an empty room during teaching hours, use "occupancy":
"after_hours_off" changes nothing between 07:30 and 21:00 on weekdays. To restore comfort, use "schedule".
Follow the orchestrator's instruction when the facts it gives support it; choose
the least disruptive mode; always give a one-sentence reason. A policy guard decides whether the change
happens now or waits for a person: report its answer exactly, in one or two sentences. Never claim a change
was applied unless the tool said applied."""

REPORTER_SYSTEM = """You are the Reporter agent in CampusPulse. You write the one- or two-sentence note that the Estates
team reads in the activity log, from the facts you are given: what was wrong, what the agents did, and how it
ended (applied, held for a person, verified, escalated). Plain words, no hype, no markdown. Use only figures
present in the facts, copied exactly. Never claim something the facts do not show. Changes the policy guard
approved were made by "the agents" automatically; say "a person" or "Estates" only when the facts show a person
approved or must act."""


def _decl(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


INVESTIGATOR_TOOLS = [
    _decl("inspect_room", "One room now: occupancy, CO2, temperature, AC on/off and mode, today's classes (now, next), "
          "sensor trust, and empty-room cooling over the last 7 days.",
          {"room_id": {"type": "string"}}, ["room_id"]),
    _decl("room_history", "The room's last 7 days by hour of day: hours cooled while empty and hours with CO2 over the limit.",
          {"room_id": {"type": "string"}}, ["room_id"]),
]
CONTROLLER_TOOLS = [
    _decl("set_ac_mode", "Ask for a room's AC mode to change. The policy guard applies it or holds it for a person.",
          {"room_id": {"type": "string"}, "mode": {"type": "string", "enum": ["occupancy", "after_hours_off", "schedule"]},
           "reason": {"type": "string"}}, ["room_id", "mode", "reason"]),
]


def run_agent(name: str, system: str, task: str, tools: list[dict], handlers: dict, log_step, max_turns: int = 3):
    """A small function-calling loop for one specialist agent.
    Returns (reply_text, tool_results) where tool_results is a list of (tool, data)."""
    from google.genai import types

    from .gemini import _client, _generate
    from .team import check_numbers, verdict_note
    results: list[tuple[str, dict]] = []
    cfg_kwargs = dict(system_instruction=system, temperature=0.1)
    if tools:
        cfg_kwargs |= dict(tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
            name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    cfg = types.GenerateContentConfig(**cfg_kwargs)
    contents = [types.Content(role="user", parts=[types.Part(text=task)])]
    for _ in range(max_turns + 1):
        resp = _generate(_client(), retries=2, model=settings.gemini_model, contents=contents, config=cfg)
        calls = resp.function_calls or []
        if not calls:
            text = (resp.text or "").strip()
            facts = [task] + [d for _, d in results]
            v = check_numbers(text, facts)
            log_step("Checker", f"checked the {name}'s words", verdict_note(v), v["ok"])
            if not text or not v["ok"]:
                raise ValueError(f"{name} reply failed the checker")
            return text, results
        contents.append(resp.candidates[0].content)
        parts = []
        for c in calls:
            args = dict(c.args or {})
            fn = handlers.get(c.name)
            data = fn(args) if fn else {"error": f"unknown tool {c.name}"}
            results.append((c.name, data))
            parts.append(types.Part.from_function_response(name=c.name, response={"result": json.dumps(data, default=str)[:3000]}))
        contents.append(types.Content(role="user", parts=parts))
    raise ValueError(f"{name} ran out of turns")


# --- the three agents -----------------------------------------------------------------------

def _summary(info: dict) -> str:
    if "error" in info:
        return info["error"]
    bits = [f"{info['room_id']} is {'occupied' if info.get('occupied') else 'empty'}",
            f"AC {'on' if info.get('ac_on') else 'off'} ({info.get('ac_mode_meaning')})"]
    if info.get("co2_ppm") is not None:
        bits.append(f"CO2 {round(info['co2_ppm'])} ppm")
    cls = (f"class booked now ({info['class_now']}) but nobody in: a no-show" if info.get("class_now") and not info.get("occupied")
           else f"class now: {info['class_now']}" if info.get("class_now") else "no class now")
    nxt = f"next class {info['next_class']} in {info['minutes_to_next_class']} min" if info.get("next_class") else "no more classes today"
    trust = f"sensor trust {info.get('data_confidence')}"
    hist = f"cooled while empty {info.get('empty_cooled_hours_7d', '–')} h in the last 7 days"
    fix = ("Recommend: switch the AC to follow occupancy." if not info.get("occupied") and info.get("ac_on")
           else "Recommend: flag for Estates." if info.get("occupied") else "Recommend: no action.")
    return "; ".join(bits) + f". {cls.capitalize()}; {nxt}; {trust}; {hist}. {fix}"


def investigator(room: str, question: str, log_step) -> tuple[str, dict]:
    from ..ops import ops
    room = room.strip().upper()
    seen: dict = {}

    def inspect(args):
        data = ops.inspect_room(_rid(args.get("room_id"), room))
        seen["inspect"] = data
        log_step("Investigator", f"inspected {data.get('room_id', room)}", _brief_facts(data))
        return data

    def history(args):
        data = ops.room_history(_rid(args.get("room_id"), room))
        seen["history"] = data
        log_step("Investigator", f"read 7-day history of {room}", f"empty-cooled hours {data.get('empty_cooled_hours')}, "
                 f"stale-air hours {data.get('stale_air_hours')}")
        return data

    if settings.gemini_enabled:
        try:
            text, _ = run_agent("Investigator", INVESTIGATOR_SYSTEM, f"Room {room}. {question}",
                                INVESTIGATOR_TOOLS, {"inspect_room": inspect, "room_history": history}, log_step)
            log_step("Investigator", "reported findings", text)
            return text, seen
        except Exception as e:
            log.warning("investigator agent fell back: %s", e)
            log_step("Investigator", "Gemini unavailable: rule summary instead", type(e).__name__, False)
    info = seen.get("inspect") or inspect({"room_id": room})
    text = _summary(info)
    log_step("Investigator", "reported findings", text)
    return text, seen


def controller(room: str, instruction: str, incident: dict | None, log_step, by: str) -> tuple[str, dict]:
    from ..ops import ops
    room = room.strip().upper()
    out: dict = {}

    def set_mode(args):
        r = _rid(args.get("room_id"), room)
        mode, reason = str(args.get("mode")), str(args.get("reason") or instruction)
        log_step("Controller", f"asked for AC in {r} to '{mode}'", reason)
        data = ops.set_ac_mode(incident, r, mode, reason, by)
        out.update(data | {"room_id": r, "mode": mode})
        return data

    if settings.gemini_enabled:
        try:
            text, _ = run_agent("Controller", CONTROLLER_SYSTEM, f"Room {room}. Instruction from the orchestrator: {instruction}",
                                CONTROLLER_TOOLS, {"set_ac_mode": set_mode}, log_step)
            if not out:
                text = f"No change requested for {room}: {text}"
            log_step("Controller", "reported back", text)
            return text, out
        except Exception as e:
            log.warning("controller agent fell back: %s", e)
            log_step("Controller", "Gemini unavailable: rule action instead", type(e).__name__, False)
    if not out:
        mode = "schedule" if "schedule" in instruction.lower() else "occupancy"
        set_mode({"room_id": room, "mode": mode, "reason": instruction[:200]})
    text = (f"{room}: AC set to '{out.get('mode')}'. {out.get('why', '')}" if out.get("applied")
            else f"{room}: change to '{out.get('mode')}' not applied. {out.get('why', '')}")
    log_step("Controller", "reported back", text)
    return text, out


def reporter(facts: dict, log_step) -> str:
    def template() -> str:
        p = facts.get("proposal") or {}
        what = f"{facts.get('room_id')}: {facts.get('what')}."
        if p.get("applied_at"):
            end = {"passed": " The verifier confirmed it took effect.", "failed": " The verifier found it did not take effect; escalated.",
                   "no data": " No reading came back to verify; escalated."}.get((facts.get("check") or {}).get("result"), "")
            return f"{what} AC switched to '{p.get('mode')}' after the policy guard approved it.{end}"
        if p.get("held_because"):
            return f"{what} The change to '{p.get('mode')}' is waiting for a person ({p['held_because']})."
        if facts.get("flag"):
            return f"{what} Flagged for Estates: {facts['flag']}"
        return f"{what} No change made."

    if settings.gemini_enabled:
        try:
            text, _ = run_agent("Reporter", REPORTER_SYSTEM, json.dumps(facts, default=str)[:4000], [], {}, log_step, max_turns=0)
            log_step("Reporter", "wrote the log note", text)
            return text
        except Exception as e:
            log.warning("reporter agent fell back: %s", e)
    text = template()
    log_step("Reporter", "wrote the log note (template)", text)
    return text


def _rid(raw, room: str) -> str:
    from .team import _room_id
    return _room_id(raw, {"room_id": room})


def _brief_facts(d: dict) -> str:
    keys = ("occupied", "ac_on", "co2_ppm", "class_now", "next_class", "data_confidence", "ac_mode", "empty_cooled_hours_7d")
    return ", ".join(f"{k.replace('_', ' ')} {d[k]}" for k in keys if d.get(k) is not None)

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


# --- the coordinator ------------------------------------------------------------------------

COORD_SYSTEM = """You are the coordinator of CampusPulse, a team of AI agents that helps a university
Estates office run its rooms and air-conditioning. You do not answer from memory and you never
calculate or estimate a figure. You decide which specialist to call, in what order, and you
combine what they return.

Your specialists (call them as tools):
- ask_campus_data: the SQL analyst. Facts about rooms, energy, air quality (CO2), occupancy and
  sensor health, from the warehouse. Use it for "which", "how many", "when", "why" questions.
- campus_status: the brief writer. This week's key figures and the top recommended changes.
- plan_tomorrow: the planner. Optimises the next teaching day's rooms and AC and reports the saving.
  Pass the manager's goals in their own words. It only proposes; a person approves in the app.

Rules:
- Always call at least one specialist. For a request with two parts (for example "why is X
  wasting energy and what should we do tomorrow?") call the specialists in order and use the
  first result to brief the second.
- Use only figures that appear in the specialists' replies, copied exactly.
- If a specialist fails or the data cannot answer, say so plainly and say what is needed.
- Never claim a plan was applied. Approval happens in the CampusPulse app.
- Reply in plain English, at most 120 words, no markdown tables. Lead with the answer.
- Never mention tool or function names; say "the plan" or "the data", not plan_tomorrow."""

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
         "goals": {"type": "string", "description": "The manager's goals or limits in their own words, or empty."}}}},
]
SPECIALIST = {"ask_campus_data": "SQL analyst", "campus_status": "Brief writer", "plan_tomorrow": "Planner"}
MAX_STEPS = 4
RUNS: deque = deque(maxlen=20)


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


def coordinate(message: str, run_skill, route, via: str = "app") -> dict:
    """Run the team on one request. `run_skill(skill, text) -> (reply, data)` executes a specialist;
    `route(text)` is the keyword fallback used when Gemini is off or fails."""
    message = (message or "").strip()[:500]
    t0 = time.time()
    steps: list[dict] = []
    results: list[tuple[str, str, dict]] = []

    def call(skill: str, text: str) -> str:
        t = time.time()
        try:
            reply, data = run_skill(skill, text)
            ok = True
        except Exception as e:  # a specialist failing must not take the run down
            reply, data, ok = f"{SPECIALIST[skill]} failed: {type(e).__name__}", {}, False
        steps.append({"agent": "Coordinator", "did": f"called the {SPECIALIST[skill]}",
                      "detail": text[:200], "ok": ok, "ms": round((time.time() - t) * 1000)})
        steps.extend(_substeps(skill, data))
        results.append((skill, reply, data))
        return reply

    def finish(reply: str, source: str) -> dict:
        data = results[-1][2] if results else {}
        return _record({"message": message, "reply": reply, "steps": steps, "source": source, "via": via,
                        "skill": results[-1][0] if results else None, "skills": [r[0] for r in results],
                        "data": data, "ms": round((time.time() - t0) * 1000)})

    def fallback(why: str) -> dict:
        if not results:
            call(route(message), message)
        return finish("\n\n".join(r[1] for r in results), why)

    if not settings.gemini_enabled:
        return fallback("keyword router (Gemini is off)")
    try:
        from google.genai import types
        from .gemini import _client, _generate, _why
        cfg = types.GenerateContentConfig(
            system_instruction=COORD_SYSTEM, temperature=0.1,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
                for t in COORD_TOOLS])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        contents = [types.Content(role="user", parts=[types.Part(text=message)])]
        for _ in range(MAX_STEPS):
            resp = _generate(_client(), retries=2, model=settings.gemini_model, contents=contents, config=cfg)
            calls = resp.function_calls or []
            if not calls:
                text = (resp.text or "").strip()
                if not results:  # the coordinator must delegate; it may not answer on its own
                    return fallback(f"keyword router (coordinator answered without a specialist)")
                verdict = check_numbers(text, [r[1] for r in results] + [r[2] for r in results])
                steps.append({"agent": "Checker", "did": "checked the coordinator's reply against the specialists",
                              "detail": verdict_note(verdict), "ok": verdict["ok"]})
                if not text or not verdict["ok"]:
                    return finish("\n\n".join(r[1] for r in results),
                                  f"Gemini coordinator ({settings.gemini_model}); specialists' own words used")
                return finish(text, f"Gemini coordinator ({settings.gemini_model})")
            contents.append(resp.candidates[0].content)
            parts = []
            for c in calls:
                args = c.args or {}
                if c.name not in SPECIALIST:
                    out = f"unknown specialist {c.name}"
                else:
                    out = call(c.name, str(args.get("question") or args.get("goals") or message))
                parts.append(types.Part.from_function_response(name=c.name, response={"reply": out[:3000]}))
            contents.append(types.Content(role="user", parts=parts))
        return finish("\n\n".join(r[1] for r in results), f"Gemini coordinator ({settings.gemini_model}); step limit")
    except Exception as e:
        log.warning("coordinator failed: %s", e)
        from .gemini import _why
        return fallback(f"keyword router (Gemini error: {_why(e)})")


# --- the roster shown in the app -------------------------------------------------------------

def roster() -> list[dict]:
    from . import gemini as g
    llm = f"Gemini ({settings.gemini_model})" if settings.gemini_enabled else "Gemini (off: templates in use)"
    return [
        {"id": "coordinator", "name": "Coordinator", "kind": "llm", "model": llm, "pattern": "Coordinator",
         "job": "Reads a request, decides which specialists to call and in what order, combines their replies.",
         "tools": ["ask_campus_data", "campus_status", "plan_tomorrow"],
         "guardrail": "Must delegate; its reply is checked against the specialists' figures.",
         "prompt": COORD_SYSTEM},
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

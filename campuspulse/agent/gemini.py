"""Gemini: the daily decision brief and the ask-the-data SQL agent.

Gemini never invents figures. The brief is written from actions the decision
engine already computed, and answers come from SQL that Gemini writes, our
guard checks, and the warehouse runs. Without a key the app falls back to
templated briefs and the tested question set, and says so.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache

import pandas as pd

from ..config import settings
from ..store import Store
from .canned import CANNED, match
from .schema_doc import SCHEMA_DOC
from .sqlguard import UnsafeSQL, guard

log = logging.getLogger(__name__)
MAX_TOOL_TURNS = 5


@lru_cache(maxsize=1)
def _client():
    from google import genai
    from google.genai import types

    http = types.HttpOptions(timeout=30_000)  # ms; a slow model must not freeze the page
    if settings.gemini_vertex:
        return genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_location,
                            http_options=http)
    return genai.Client(api_key=settings.gemini_api_key, http_options=http)


def _rows(df: pd.DataFrame, n: int = 200) -> list[dict]:
    df = df.head(n).copy()
    for c in df.columns:
        if not (pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c])):
            df[c] = df[c].astype(str)
        elif pd.api.types.is_float_dtype(df[c]):
            df[c] = df[c].round(3)
    return df.astype(object).where(df.notna(), None).to_dict("records")


def chart_for(rows: list[dict]) -> dict | None:
    """Pick a sensible chart: first text/time column as x, first numeric column as y."""
    if len(rows) < 2:
        return None
    cols = list(rows[0])
    num = [c for c in cols if all(isinstance(r[c], (int, float)) and not isinstance(r[c], bool)
                                   for r in rows if r[c] is not None)]
    cat = [c for c in cols if c not in num]
    if not num:
        return None
    x = cat[0] if cat else cols[0]
    y = next((c for c in reversed(num) if c != x), None)
    if y is None:
        return None
    timeish = any(k in x for k in ("hour", "day", "date", "ts"))
    return {"type": "line" if timeish else "bar", "x_field": x, "y_field": y,
            "x": [str(r[x]) for r in rows[:60]], "y": [r[y] for r in rows[:60]]}


# --- brief -------------------------------------------------------------------

BRIEF_SYSTEM = """You are the facilities analyst for a university campus in Malaysia.
Write a short morning brief for the Estates manager from the JSON you are given.
Rules:
- Use only numbers that appear in the JSON. Never estimate or invent figures.
- Round kWh and RM to whole numbers with thousands separators (3,846 kWh, RM 1,923). Show fractions such as utilisation as percentages (0.413 -> 41%). Do not mention action ids or field names.
- Keep the engine's ranking unless confidence is low; a low-confidence action must say what to check first.
- Plain, direct English. No hype, no emoji.
The headline states the single most important finding as a plain sentence (for example the share of
cooling spent on empty rooms); do not start it with "Morning brief". Copy numbers exactly as written.
Return JSON: {"headline": str (max 14 words), "summary": str (max 60 words),
 "items": [{"id": str, "why_now": str (max 30 words), "first_step": str (max 20 words)}]} for up to 5 actions.
first_step is a concrete thing a named team can do this week (who does what); never restate the action's title."""


def _model_payload(kpis: dict, actions: list[dict]) -> dict:
    """Numbers pre-formatted for reading, so the model copies '3,856 kWh', not '3856.0'."""
    def kwh(v): return f"{round(float(v or 0)):,} kWh"
    def rm(v): return f"RM {round(float(v or 0)):,}"
    def pct(v): return f"{round(float(v or 0) * 100)}%"
    k = {
        "window": kpis.get("window", "last 7 days"),
        "cooling_energy_spent_on_empty_rooms": kwh(kpis.get("wasted_kwh")),
        "cost_of_that_energy": rm(kpis.get("wasted_rm")),
        "share_of_all_cooling_energy_spent_on_empty_rooms": pct(kpis.get("wasted_share")),
        "occupied_room_hours_with_stale_air": kpis.get("stale_air_hours"),
        "average_booked_room_utilisation": pct(kpis.get("avg_utilisation")),
        "fleet_data_confidence": f"{kpis.get('fleet_confidence')}/100",
        "sensor_nodes_with_low_confidence": kpis.get("low_confidence_nodes"),
    }
    acts = []
    for a in actions[:8]:
        acts.append({
            "id": a["id"], "kind": a["kind"], "title": a["title"], "summary": a["summary"],
            "weekly_saving": kwh(a.get("kwh_per_week")) if a.get("kwh_per_week") else None,
            "weekly_saving_rm": rm(a.get("rm_per_week")) if a.get("rm_per_week") else None,
            "confidence": f"{a.get('confidence_level')} ({a.get('confidence')}/100)",
            "rooms": a.get("rooms"),
        })
    return {"kpis": k, "actions": acts}


FIRST_STEP = {
    "after_hours_ac": "Ask the building system team to clear the weekend override today.",
    "occupancy_linked_ac": "Pilot PIR-linked AC control in the two worst rooms this week.",
    "right_size": "Send the room swaps to the timetabling office for next week.",
    "air_quality": "Raise fresh-air supply now; propose the room change to timetabling.",
    "data_reliability": "Book a technician visit before acting on this room's data.",
}


def _repeats(step: str, title: str) -> bool:
    """True when a 'first step' only restates the action's title."""
    a = "".join(ch for ch in (step or "").lower() if ch.isalnum())
    b = "".join(ch for ch in (title or "").lower() if ch.isalnum())
    return not a or a in b or b in a


def _fallback_brief(actions: list[dict], kpis: dict) -> dict:
    top = actions[:5]
    items = []
    for a in top:
        items.append({"id": a["id"], "why_now": a["summary"], "first_step": FIRST_STEP[a["kind"]]})
    lead = top[0] if top else None
    saving = sum(max(a["rm_per_week"], 0) for a in top)
    headline = (f"Empty rooms took {kpis['wasted_share'] * 100:.0f}% of this week's cooling energy"
                if kpis.get("wasted_share") else "No major waste found this week")
    summary = (f"The top {len(top)} changes are worth about RM {saving:,.0f} a week; start with "
               f"“{lead['title']}”." if lead else "")
    if kpis.get("low_confidence_nodes"):
        summary += f" {kpis['low_confidence_nodes']} sensor node(s) need a fix before their rooms' data is trusted."
    return {
        "headline": headline,
        "summary": summary.strip(),
        "items": items,
        "source": "template (Gemini not configured)",
    }



def _parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating ```json fences around it."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    return json.loads(t[start:end + 1] if start >= 0 and end > start else t)


def _why(e: Exception) -> str:
    """Short, key-free reason for a Gemini failure, shown in the UI."""
    code = getattr(e, "code", None)
    status = getattr(e, "status", None)
    hints = {401: "check the API key", 403: "key not allowed for this API",
             404: "model name not found", 429: "quota or rate limit reached"}
    if code:
        detail = (getattr(e, "message", None) or "").strip().replace("\n", " ")[:160]
        hint = detail or hints.get(code, "")
        return f"{code} {status or ''}".strip() + (f": {hint}" if hint else "")
    return type(e).__name__

def daily_brief(actions: list[dict], kpis: dict) -> dict:
    if not settings.gemini_enabled:
        return _fallback_brief(actions, kpis)
    from google.genai import types

    payload = _model_payload(kpis, actions)
    try:
        client = _client()
        try:
            resp = client.models.generate_content(
                model=settings.gemini_model,
                contents=json.dumps(payload, default=str),
                config=types.GenerateContentConfig(system_instruction=BRIEF_SYSTEM, temperature=0.2,
                                                   response_mime_type="application/json"),
            )
        except Exception as first:
            if getattr(first, "code", None) != 400:
                raise
            # Some models reject optional settings; retry with the instructions inline and defaults.
            log.warning("Gemini brief retry without optional settings: %s", first)
            resp = client.models.generate_content(
                model=settings.gemini_model,
                contents=BRIEF_SYSTEM + "\nReturn only the JSON object.\n\n" + json.dumps(payload, default=str),
            )
        brief = _parse_json(resp.text)
        known = {a["id"] for a in actions}
        brief["items"] = [i for i in brief.get("items", []) if i.get("id") in known][:5]
        by_id = {a["id"]: a for a in actions}
        for i in brief["items"]:
            a = by_id[i["id"]]
            if _repeats(i.get("first_step", ""), a["title"]):
                i["first_step"] = FIRST_STEP[a["kind"]]
        brief["source"] = f"Gemini ({settings.gemini_model})"
        return brief
    except Exception as e:  # never let the brief take the page down
        log.warning("Gemini brief failed: %s", e)
        out = _fallback_brief(actions, kpis)
        out["source"] = f"template (Gemini error: {_why(e)})"
        return out


# --- ask the data ------------------------------------------------------------

# --- chart captions ------------------------------------------------------------

CAPTION_SYSTEM = """You write one-line captions for evidence charts in a campus facilities dashboard.
You get the chart's title, its x labels and y values, and an optional reference line.
Write one plain sentence, at most 25 words, stating the single most useful pattern.
Use only numbers that appear in the data (you may round them). Name rooms, days or classes as given.
No hype, no advice, no emoji. Return only the sentence."""


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 10 else f"{v:,.1f}"


def _fallback_caption(chart: dict) -> str:
    x, y = chart.get("x") or [], [float(v) for v in (chart.get("y") or [])]
    if not x or not y:
        return ""
    label = (chart.get("label") or "Value").rstrip(".")
    hi = max(range(len(y)), key=y.__getitem__)
    if len(y) == 1:
        return f"{label}: {x[0]} at {_num(y[0])}."
    lo = min(range(len(y)), key=y.__getitem__)
    text = f"{label}: highest {x[hi]} at {_num(y[hi])}, lowest {x[lo]} at {_num(y[lo])}"
    ref = chart.get("ref")
    if ref is not None:
        above = sum(v > ref for v in y)
        text += f"; {above} of {len(y)} above the {_num(float(ref))} line"
    return text + "."


def chart_caption(action: dict) -> dict:
    """One sentence describing an action's evidence chart, from its numbers only."""
    chart = action.get("chart") or {}
    fallback = {"caption": _fallback_caption(chart), "source": "template"}
    if not settings.gemini_enabled or not chart.get("x"):
        return fallback
    from google.genai import types

    data = {"title": chart.get("label"), "action": action.get("title"),
            "points": [{"x": str(a), "y": b} for a, b in zip(chart["x"][:40], chart["y"][:40])],
            "reference_line": chart.get("ref")}
    try:
        resp = _client().models.generate_content(
            model=settings.gemini_model, contents=json.dumps(data, default=str),
            config=types.GenerateContentConfig(system_instruction=CAPTION_SYSTEM, temperature=0.2),
        )
        text = " ".join((resp.text or "").split()).strip().strip('"')
        if not text:
            return fallback
        return {"caption": text[:240], "source": f"Gemini ({settings.gemini_model})"}
    except Exception as e:
        log.warning("Gemini caption failed: %s", e)
        return fallback | {"source": f"template (Gemini error: {_why(e)})"}


ASK_SYSTEM = """You answer questions about campus rooms, energy, air quality and sensor network health
by querying the warehouse with the run_sql tool. Always query; never answer from memory.
Write one BigQuery SELECT at a time. If a query fails, read the error and fix it.
When you have the result, answer in at most 3 sentences, quoting the numbers from the result.
If the question cannot be answered from these tables, say what data would be needed.
""" + SCHEMA_DOC.replace("{tariff}", str(settings.tariff_rm_per_kwh))


def _run(store: Store, sql: str) -> tuple[str, list[dict]]:
    safe = guard(sql)
    return safe, _rows(store.query(safe))


def _offline_answer(store: Store, question: str) -> dict:
    c = match(question)
    if c is None:
        return {"answer": "Offline mode answers only the suggested questions. Add a Gemini key to ask anything.",
                "sql": None, "rows": [], "chart": None, "source": "offline"}
    sql, rows = _run(store, c["sql"])
    return {"answer": _describe(rows) if rows else "No rows matched that question in the current data.",
            "sql": sql, "rows": rows, "chart": chart_for(rows), "source": "offline (tested query)",
            "matched": c["q"]}


def _describe(rows: list[dict]) -> str:
    """One plain sentence about the first row, for offline mode."""
    first = rows[0]
    text = [k for k, v in first.items() if isinstance(v, str)]
    nums = [k for k, v in first.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    name = first.get("room_name") or first.get("name") or first.get("module") or (
        f"Block {first['block']}" if first.get("block") else None)
    label = f"{name} ({first['room_id']})" if name and first.get("room_id") else (
        name or (first[text[0]] if text else "The result"))
    facts = ", ".join(f"{first[k]:,} {k.replace('_', ' ')}" if isinstance(first[k], int)
                      else f"{first[k]:,.2f} {k.replace('_', ' ')}" for k in nums[:3])
    more = f" {len(rows) - 1} more row(s) below." if len(rows) > 1 else ""
    return f"{label} comes first: {facts}.{more}" if facts else f"{len(rows)} row(s) found.{more}"


def ask(store: Store, question: str) -> dict:
    question = question.strip()[:500]
    if not settings.gemini_enabled:
        return _offline_answer(store, question)
    from google.genai import types

    tool = types.Tool(function_declarations=[types.FunctionDeclaration(
        name="run_sql",
        description="Run one read-only BigQuery Standard SQL SELECT over the campus views and return rows.",
        parameters_json_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "A single SELECT statement using bare table names."}},
            "required": ["query"]},
    )])
    cfg = types.GenerateContentConfig(
        system_instruction=ASK_SYSTEM, tools=[tool], temperature=0.1,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = [types.Content(role="user", parts=[types.Part(text=question)])]
    last_sql, last_rows, trace = None, [], []
    try:
        for _ in range(MAX_TOOL_TURNS):
            resp = _client().models.generate_content(model=settings.gemini_model, contents=contents, config=cfg)
            calls = resp.function_calls or []
            if not calls:
                return {"answer": (resp.text or "").strip(), "sql": last_sql, "rows": last_rows,
                        "chart": chart_for(last_rows), "source": f"Gemini ({settings.gemini_model})",
                        "attempts": trace}
            contents.append(resp.candidates[0].content)
            parts = []
            for call in calls:
                sql = (call.args or {}).get("query", "")
                try:
                    last_sql, last_rows = _run(store, sql)
                    result = {"rows": last_rows[:50], "row_count": len(last_rows)}
                    trace.append({"sql": last_sql, "ok": True})
                except (UnsafeSQL, Exception) as e:  # give the error back so Gemini can fix its SQL
                    result = {"error": str(e)[:400]}
                    trace.append({"sql": sql, "ok": False, "error": str(e)[:200]})
                parts.append(types.Part.from_function_response(name=call.name, response=result))
            contents.append(types.Content(role="user", parts=parts))
        return {"answer": "I could not finish within the query budget. Try a narrower question.",
                "sql": last_sql, "rows": last_rows, "chart": chart_for(last_rows),
                "source": f"Gemini ({settings.gemini_model})", "attempts": trace}
    except Exception as e:
        log.warning("Gemini ask failed: %s", e)
        out = _offline_answer(store, question)
        out["source"] = f"offline fallback (Gemini error: {_why(e)})"
        return out


def suggestions() -> list[str]:
    return [c["q"] for c in CANNED]


# --- Autopilot: plain-English goals -> solver constraints; plan explanation; notices --------

PLAN_SYSTEM = """You turn a facilities manager's goals for tomorrow's room and air-conditioning plan
into constraints for an optimisation solver. Call set_constraints exactly once.
Rules: only set fields the manager asked for; module codes look like CT026 or ABUS003; room ids look like
AUD-1, B-08-05, TL4-02. "Don't move anything" means no_moves. "Keep moves nearby / same block" means same_block.
"Comfort first", "students first" or "cooler rooms" means comfort_first. In notes, list in a few words each
what you understood, so the manager can check it."""

EXPLAIN_SYSTEM = """You explain an optimised plan for tomorrow to a university facilities manager.
You get the solver's results as JSON. Write JSON with "headline" (at most 14 words, lead with the saving)
and "summary" (2-3 sentences: what changes, what is applied automatically, what needs their approval and why,
and any comfort effect). Use only numbers present in the input, rounded as given. Plain words, no hype."""

NOTICE_SYSTEM = """You write short room-change notices for students and lecturers at a university.
For each move in the input, write one notice of at most 40 words: module, day, time, old room -> new room,
and one friendly reason (a better-sized room, fresher air). Return JSON {"notices": [{"id": ..., "text": ...}]}.
Use only facts from the input."""

CONSTRAINT_FN = {
    "name": "set_constraints",
    "description": "Constraints for tomorrow's room and AC optimisation.",
    "parameters": {
        "type": "object",
        "properties": {
            "no_moves": {"type": "boolean", "description": "Keep every class in its timetabled room."},
            "same_block": {"type": "boolean", "description": "Moves must stay in the same building block."},
            "max_moves": {"type": "integer", "description": "Upper limit on room moves."},
            "headroom_pct": {"type": "integer", "description": "Spare seats to keep, percent (default 15)."},
            "frozen_modules": {"type": "array", "items": {"type": "string"},
                               "description": "Module codes or names that must not move."},
            "frozen_rooms": {"type": "array", "items": {"type": "string"},
                             "description": "Room ids whose classes must not move."},
            "frozen_days": {"type": "array", "items": {"type": "string"},
                            "description": "Weekdays (Mon, Tue, Wed, Thu, Fri) whose classes must not move."},
            "comfort_first": {"type": "boolean", "description": "Prefer comfort: longer pre-cool, more spare seats."},
            "notes": {"type": "array", "items": {"type": "string"}, "description": "What was understood."},
        },
    },
}


def plan_constraints(text: str) -> tuple[dict, str]:
    """Plain-English goals -> constraint dict, and who read them."""
    from ..autopilot import parse_instructions_offline
    if not text or not text.strip():
        return {}, "defaults"
    if not settings.gemini_enabled:
        return parse_instructions_offline(text).as_dict(), "rule-based reader (Gemini not configured)"
    from google.genai import types
    try:
        resp = _client().models.generate_content(
            model=settings.gemini_model, contents=text,
            config=types.GenerateContentConfig(
                system_instruction=PLAN_SYSTEM, temperature=0.0,
                tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**CONSTRAINT_FN)])],
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            ))
        for part in resp.candidates[0].content.parts:
            if part.function_call and part.function_call.name == "set_constraints":
                args = dict(part.function_call.args or {})
                for k in ("headroom_pct", "max_moves"):
                    if k in args and args[k] is not None:
                        args[k] = int(args[k])
                for k in ("frozen_modules", "frozen_rooms", "frozen_days", "notes"):
                    if k in args:
                        args[k] = [str(x) for x in args[k]]
                return args, f"Gemini ({settings.gemini_model})"
        raise ValueError("no function call returned")
    except Exception as e:
        log.warning("Gemini constraints failed: %s", e)
        return parse_instructions_offline(text).as_dict(), f"rule-based reader (Gemini error: {_why(e)})"


def _plan_payload(p: dict) -> dict:
    s = p["saving"]
    return {"day": p["day_label"], "classes": p["classes"], "max_outdoor_c": p["weather"]["max_c"],
            "energy_today_setup_kwh": round(p["baseline"]["kwh"]), "energy_plan_kwh": round(p["plan"]["kwh"]),
            "saving_kwh": round(s["kwh"]), "saving_rm": round(s["rm"]), "saving_kg_co2e": round(s["kg_co2e"]),
            "saving_pct": f"{round(s['pct'] * 100)}%", "from_ac_schedule_kwh": round(s["from_ac_schedule_kwh"]),
            "from_room_moves_kwh": round(s["from_room_moves_kwh"]),
            "stale_air_hours_before": p["baseline"]["co2_breach_hours"],
            "stale_air_hours_after": p["plan"]["co2_breach_hours"],
            "moves_auto": p["auto_moves"], "moves_need_approval": p["review_moves"],
            "approval_reasons": sorted({r for m in p["moves"] if m["decision"] == "review" for r in m["reasons"]})[:4],
            "constraints_understood": p["constraints"].get("notes", [])}


def _fallback_explain(p: dict) -> dict:
    s = p["saving"]
    b, a = p["baseline"]["co2_breach_hours"], p["plan"]["co2_breach_hours"]
    comfort = f" Stale-air class-hours fall from {b:g} to {a:g}." if a < b else ""
    return {"headline": f"Plan saves {s['kwh']:,.0f} kWh (RM {s['rm']:,.0f}) on {p['day_label']}",
            "summary": (f"AC runs only for booked classes with a weather-based pre-cool "
                        f"({s['from_ac_schedule_kwh']:,.0f} kWh), and {len(p['moves'])} classes move to better-sized "
                        f"rooms ({s['from_room_moves_kwh']:,.0f} kWh).{comfort} {p['auto_moves']} moves are safe to "
                        f"apply automatically; {p['review_moves']} need your approval."),
            "source": "template"}


def explain_plan(p: dict) -> dict:
    if not settings.gemini_enabled:
        return _fallback_explain(p)
    from google.genai import types
    try:
        resp = _client().models.generate_content(
            model=settings.gemini_model, contents=json.dumps(_plan_payload(p)),
            config=types.GenerateContentConfig(system_instruction=EXPLAIN_SYSTEM, temperature=0.2,
                                               response_mime_type="application/json"))
        out = _parse_json(resp.text)
        return {"headline": str(out["headline"])[:140], "summary": str(out["summary"])[:700],
                "source": f"Gemini ({settings.gemini_model})"}
    except Exception as e:
        log.warning("Gemini plan explanation failed: %s", e)
        return _fallback_explain(p) | {"source": f"template (Gemini error: {_why(e)})"}


def _fallback_notice(m: dict) -> str:
    reason = "fresher air in a larger room" if m.get("co2_fix") else "a room sized for the class"
    return (f"{m['module']} on {m['day']} {m['time']} moves from {m['from']} to {m['to']} "
            f"({m['to_name']}), for {reason}. Same time, new room.")


def draft_notices(moves: list[dict]) -> dict:
    if not moves:
        return {"notices": [], "source": "none"}
    base = [{"id": m["id"], "text": _fallback_notice(m)} for m in moves]
    if not settings.gemini_enabled:
        return {"notices": base, "source": "template"}
    from google.genai import types
    facts = [{k: m[k] for k in ("id", "module", "day", "time", "from", "to", "to_name", "co2_fix")} for m in moves[:20]]
    try:
        resp = _client().models.generate_content(
            model=settings.gemini_model, contents=json.dumps(facts),
            config=types.GenerateContentConfig(system_instruction=NOTICE_SYSTEM, temperature=0.3,
                                               response_mime_type="application/json"))
        got = {n["id"]: str(n["text"])[:300] for n in _parse_json(resp.text).get("notices", []) if n.get("id")}
        return {"notices": [{"id": b["id"], "text": got.get(b["id"], b["text"])} for b in base],
                "source": f"Gemini ({settings.gemini_model})"}
    except Exception as e:
        log.warning("Gemini notices failed: %s", e)
        return {"notices": base, "source": f"template (Gemini error: {_why(e)})"}

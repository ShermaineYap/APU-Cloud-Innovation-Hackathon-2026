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

    if settings.gemini_vertex:
        return genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_location)
    return genai.Client(api_key=settings.gemini_api_key)


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
- Use only numbers that appear in the JSON. Never estimate, round up or invent figures.
- Keep the engine's ranking unless confidence is low; a low-confidence action must say what to check first.
- Plain, direct English. No hype, no emoji.
Return JSON: {"headline": str (max 14 words), "summary": str (max 60 words),
 "items": [{"id": str, "why_now": str (max 30 words), "first_step": str (max 20 words)}]} for up to 5 actions."""


def _fallback_brief(actions: list[dict], kpis: dict) -> dict:
    top = actions[:5]
    items = []
    for a in top:
        first = {"after_hours_ac": "Ask the building system team to clear the weekend override today.",
                 "occupancy_linked_ac": "Pilot PIR-linked AC control in the two worst rooms this week.",
                 "right_size": "Send the swap to the timetabling office for next week.",
                 "air_quality": "Raise fresh-air supply now; propose the room change to timetabling.",
                 "data_reliability": "Book a technician visit before acting on this room's data."}[a["kind"]]
        items.append({"id": a["id"], "why_now": a["summary"], "first_step": first})
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



def _why(e: Exception) -> str:
    """Short, key-free reason for a Gemini failure, shown in the UI."""
    code = getattr(e, "code", None)
    status = getattr(e, "status", None)
    hints = {400: "check the API key", 401: "check the API key", 403: "key not allowed for this API",
             404: "model name not found", 429: "quota or rate limit reached"}
    if code:
        hint = hints.get(code, "")
        return f"{code} {status or ''}".strip() + (f", {hint}" if hint else "")
    return type(e).__name__

def daily_brief(actions: list[dict], kpis: dict) -> dict:
    if not settings.gemini_enabled:
        return _fallback_brief(actions, kpis)
    from google.genai import types

    payload = {"kpis": kpis, "actions": [
        {k: a[k] for k in ("id", "kind", "title", "summary", "kwh_per_week", "rm_per_week", "comfort",
                           "confidence", "confidence_level", "rooms")} for a in actions[:8]]}
    try:
        resp = _client().models.generate_content(
            model=settings.gemini_model,
            contents=json.dumps(payload, default=str),
            config=types.GenerateContentConfig(system_instruction=BRIEF_SYSTEM, temperature=0.2,
                                               response_mime_type="application/json"),
        )
        brief = json.loads(resp.text)
        known = {a["id"] for a in actions}
        brief["items"] = [i for i in brief.get("items", []) if i.get("id") in known][:5]
        brief["source"] = f"Gemini ({settings.gemini_model})"
        return brief
    except Exception as e:  # never let the brief take the page down
        log.warning("Gemini brief failed: %s", e)
        out = _fallback_brief(actions, kpis)
        out["source"] = f"template (Gemini error: {_why(e)})"
        return out


# --- ask the data ------------------------------------------------------------

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

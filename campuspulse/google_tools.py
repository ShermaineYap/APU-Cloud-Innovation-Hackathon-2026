"""Google tools around CampusPulse that need no extra sign-in.

  GET /api/integrations             which Google services this deployment uses, and their state
  GET /api/autopilot/latest.csv     tomorrow's room changes: Google Sheets =IMPORTDATA("<this url>")
  GET /api/autopilot/schedule.csv   tomorrow's AC schedule per room, for Sheets too
  GET /api/autopilot/latest.ics     room changes as calendar events: subscribe in Google Calendar

The exports use the last plan made in the app; if there is none yet they make one
(solver only, no Gemini call), so a spreadsheet or calendar polling them stays cheap.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .config import settings

router = APIRouter()


def _latest_plan() -> dict:
    from . import api, autopilot
    from .store import get_store
    p = autopilot.get_plan(api.state.get("last_plan") or "")
    if not p:
        p = autopilot.plan(get_store())
        if "error" not in p:
            api.state["last_plan"] = p["id"]
    return p


def _csv(rows: list[dict], cols: list[str], name: str) -> Response:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-cache"})


@router.get("/api/autopilot/latest.csv")
def plan_csv():
    p = _latest_plan()
    rows = [{"day": p.get("day"), "time": m["time"], "module": m["module"], "from_room": m["from"], "to_room": m["to"],
             "expected_people": m["people"], "busy_week_people": m["busy_week_people"], "to_seats": m["to_seats"],
             "kwh_saved": m["kwh_saved"], "fixes_stale_air": m["co2_fix"], "decision": m["decision"],
             "why": m["why"], "needs_review_because": "; ".join(m.get("reasons", []))}
            for m in p.get("moves", [])]
    cols = ["day", "time", "module", "from_room", "to_room", "expected_people", "busy_week_people", "to_seats",
            "kwh_saved", "fixes_stale_air", "decision", "why", "needs_review_because"]
    return _csv(rows, cols, f"campuspulse-room-changes-{p.get('day')}.csv")


@router.get("/api/autopilot/schedule.csv")
def schedule_csv():
    p = _latest_plan()
    rows = [{"day": p.get("day"), "room": r["room_id"], "ac_on": "; ".join(r["windows"]) or "off all day",
             "hours_on": r["hours_on"], "hours_on_today": r["hours_before"], "decision": r["decision"],
             "data_confidence": r["confidence"]} for r in p.get("ac_schedule", [])]
    return _csv(rows, ["day", "room", "ac_on", "hours_on", "hours_on_today", "decision", "data_confidence"],
                f"campuspulse-ac-schedule-{p.get('day')}.csv")


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _utc(day: str, hhmm: str) -> str:
    local = datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=settings.timezone)
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@router.get("/api/autopilot/latest.ics")
def plan_ics():
    p = _latest_plan()
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CampusPulse//Autopilot//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:CampusPulse room changes", "X-WR-TIMEZONE:Asia/Kuala_Lumpur"]
    for m in p.get("moves", []):
        start, end = m["time"].replace("\u2013", "-").split("-")
        status = "Confirmed by Autopilot" if m["decision"] == "auto" else "Proposed, waiting for Estates approval"
        lines += ["BEGIN:VEVENT", f"UID:{p.get('day')}-{m['id']}@campuspulse", f"DTSTAMP:{now}",
                  f"DTSTART:{_utc(p['day'], start)}", f"DTEND:{_utc(p['day'], end)}",
                  f"SUMMARY:{_ics_escape(m['module'] + ': ' + m['from'] + ' -> ' + m['to'])}",
                  f"LOCATION:{_ics_escape(m['to_name'])}",
                  f"DESCRIPTION:{_ics_escape(status + '. ' + m['why'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="campuspulse-room-changes.ics"',
                             "Cache-Control": "no-cache"})


_cloud_cache: dict = {"at": 0.0, "value": None}


def _cloud_report() -> dict | None:
    """Running locally with CP_PUBLIC_URL set: ask the Cloud Run deployment what it runs on.

    Returns {"items": {id: item}} from a deployment with this panel, {"health": {...}} from an older
    one, or None when there is no public URL, we ARE the deployment, or it can't be reached."""
    import time

    import httpx
    if os.getenv("K_SERVICE") or not settings.public_url:
        return None
    if time.time() - _cloud_cache["at"] < 60:
        return _cloud_cache["value"]
    base, value = settings.public_url.rstrip("/"), None
    try:
        r = httpx.get(f"{base}/api/integrations", timeout=8)
        if r.status_code == 200:
            value = {"items": {i["id"]: i for i in r.json().get("items", [])}}
        else:
            h = httpx.get(f"{base}/api/health", timeout=8)
            value = {"health": h.json()} if h.status_code == 200 else None
    except Exception:
        value = None
    _cloud_cache.update(at=time.time(), value=value)
    return value


@router.get("/api/integrations")
def integrations(request: Request):
    from .a2a import _base_url
    base = _base_url(request)
    on_cloud_run = bool(os.getenv("K_SERVICE"))
    cloud = _cloud_report()
    gemini = ("Vertex AI" if settings.gemini_vertex and settings.gcp_project
              else "Google AI Studio" if settings.gemini_api_key else None)
    ics = f"{base}/api/autopilot/latest.ics"
    public = base.startswith("https://")
    items = [
        {"id": "gemini", "name": "Gemini", "state": "on" if gemini else "off",
         "detail": f"{settings.gemini_model} via {gemini}: six roles (goal reader, SQL agent, brief, plan "
                   "explainer, notices, chart captions)" if gemini else "No key or Vertex project set: templates "
                   "and tested questions stand in"},
        {"id": "gemini_enterprise", "name": "Gemini Enterprise",
         "state": "on" if settings.gemini_enterprise_app else "ready",
         "detail": (f"Registered in {settings.gemini_enterprise_app} as an A2A agent" if settings.gemini_enterprise_app
                    else "CampusPulse is an A2A agent, ready to register: give the admin the agent card URL"),
         "link": f"{base}/.well-known/agent-card.json"},
        {"id": "cloud_run", "name": "Cloud Run", "state": "on" if on_cloud_run else "off",
         "detail": f"Service {os.getenv('K_SERVICE')}, revision {os.getenv('K_REVISION', '?')}" if on_cloud_run
         else "Running locally"},
        {"id": "bigquery", "name": "BigQuery + BigQuery ML", "state": "on" if settings.backend == "bigquery" else "off",
         "detail": f"Dataset {settings.bq_dataset} in {settings.bq_location}; ARIMA_PLUS forecasts and anomalies"
         if settings.backend == "bigquery" else "Local DuckDB runs the same SQL"},
        {"id": "pubsub", "name": "Pub/Sub", "state": "on" if settings.pubsub_topic else "off",
         "detail": "Sensor readings are published, then pushed to the service" if settings.pubsub_topic
         else "Readings are stored directly"},
        {"id": "sheets", "name": "Google Sheets", "state": "on" if public else "ready",
         "detail": "In any sheet, put this in cell A1 to pull tomorrow's room changes live" if public
         else "Set CP_PUBLIC_URL to the Cloud Run address: Google can't read a laptop's localhost",
         "formula": f'=IMPORTDATA("{base}/api/autopilot/latest.csv")', "link": f"{base}/api/autopilot/latest.csv"},
        {"id": "calendar", "name": "Google Calendar", "state": "on" if public else "ready",
         "detail": "Subscribe to tomorrow's room changes as calendar events",
         "link": "https://calendar.google.com/calendar/r?cid=" + quote(ics.replace("https://", "webcal://", 1)
                                                                        if ics.startswith("https://") else ics, safe=""),
         "file": ics},
        {"id": "looker", "name": "Looker Studio",
         "state": "on" if settings.looker_url else ("ready" if settings.backend == "bigquery" else "off"),
         "detail": "Dashboard on the BigQuery views" if settings.looker_url
         else "Build a dashboard on the BigQuery views v_room_hourly and v_empty_cooled (docs/GOOGLE_TOOLS.md), "
              "then set CP_LOOKER_URL", **({"link": settings.looker_url} if settings.looker_url else {})},
    ]
    source = "this server"
    if cloud and "items" in cloud:  # the deployment has this panel: take its word for its own services
        source = "the Cloud Run deployment"
        for i, item in enumerate(items):
            if item["id"] in ("cloud_run", "bigquery", "pubsub", "looker", "gemini") and item["id"] in cloud["items"]:
                items[i] = cloud["items"][item["id"]] | {"detail": cloud["items"][item["id"]]["detail"] + " (cloud)"}
    elif cloud and "health" in cloud:  # an older deployment: what /api/health tells us
        source = "the Cloud Run deployment"
        h = cloud["health"]
        for i, item in enumerate(items):
            if item["id"] == "cloud_run":
                items[i] |= {"state": "on", "detail": f"Live at {settings.public_url}"}
            elif item["id"] == "bigquery" and h.get("backend") == "bigquery":
                items[i] |= {"state": "on", "detail": "The deployment's warehouse; ARIMA_PLUS forecasts and anomalies"}
            elif item["id"] == "gemini" and h.get("gemini"):
                items[i] |= {"state": "on", "detail": f"{h.get('model')} on the deployment: six roles"}
            elif item["id"] == "pubsub":
                items[i] |= {"detail": "Shown once the deployment runs this version (redeploy)"}
            elif item["id"] == "looker" and h.get("backend") == "bigquery" and item["state"] == "off":
                items[i] |= {"state": "ready"}
    return {"base_url": base, "items": items, "reporting": source}

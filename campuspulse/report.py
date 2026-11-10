"""One-page Estates report (PDF): the week's brief, the ranked changes and what was approved."""
from __future__ import annotations

import io
from datetime import date, datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .campus import WEEK_OF
from .config import settings

INK = colors.HexColor("#1D2A30")
MUTED = colors.HexColor("#5C6B71")
RULE = colors.HexColor("#C3CAC5")
PANEL = colors.HexColor("#F4F5F2")
TEAL = colors.HexColor("#1F7A6D")
AMBER = colors.HexColor("#B87412")
RED = colors.HexColor("#A23B2C")

def _style(name: str, size: float, leading: float, bold: bool = False, color=INK, before: float = 0) -> ParagraphStyle:
    return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
                          leading=leading, textColor=color, alignment=TA_LEFT, spaceBefore=before)


S = {
    "kicker": _style("kicker", 8.5, 11, color=MUTED),
    "title": _style("title", 19, 22, bold=True),
    "body": _style("body", 9.5, 13),
    "small": _style("small", 7.8, 10, color=MUTED),
    "h2": _style("h2", 11, 14, bold=True, before=4),
    "cell": _style("cell", 8.3, 10.4),
}


def _p(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


def _rm(v: float) -> str:
    return f"RM {v:,.0f}"


def build_pdf(brief: dict, actions: list[dict], kpis: dict, status: dict[str, str], mode: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=12 * mm, title="CampusPulse Estates report", author="CampusPulse")
    now = datetime.now(settings.timezone)
    width = A4[0] - 32 * mm
    story = [
        _p(f"CampusPulse · Estates report · APU, Bukit Jalil · {now:%A %d %B %Y, %H:%M}", "kicker"),
        Spacer(1, 3),
        _p(escape(brief.get("headline") or ""), "title"),
        Spacer(1, 5),
        _p(escape(brief.get("summary") or "")),
        Spacer(1, 7),
    ]

    figures = [
        (f"{kpis['wasted_kwh']:,.0f} kWh", f"cooling spent on empty rooms ({_rm(kpis['wasted_rm'])})"),
        (f"{kpis['wasted_share'] * 100:.0f}%", "of all cooling energy went to empty rooms"),
        (f"{kpis['stale_air_hours']:,}", f"occupied room-hours above {settings.co2_limit_ppm} ppm CO2"),
        (f"{kpis['avg_utilisation'] * 100:.0f}%", "average seats used in booked rooms"),
        (f"{kpis['fleet_confidence']}/100", "sensor data confidence"),
    ]
    fig_table = Table([[_p(f"<b>{v}</b>", "body") for v, _ in figures], [_p(t, "small") for _, t in figures]],
                      colWidths=[width / 5] * 5)
    fig_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ("LINEABOVE", (0, 0), (-1, 0), 1.2, INK),
    ]))
    story += [_p("Last 7 days", "small"), Spacer(1, 2), fig_table, Spacer(1, 9), _p("What to change", "h2")]

    why = {i.get("id"): i for i in brief.get("items", [])}
    rows = [[_p("<b>#</b>", "cell"), _p("<b>Change and first step</b>", "cell"), _p("<b>Saving a week</b>", "cell"),
             _p("<b>Confidence</b>", "cell"), _p("<b>Decision</b>", "cell")]]
    shown = actions[:8]
    for n, a in enumerate(shown, 1):
        first = why.get(a["id"], {}).get("first_step")
        detail = escape(first) if first else escape(a["summary"][:170] + ("…" if len(a["summary"]) > 170 else ""))
        saving = (f"{a['kwh_per_week']:,.0f} kWh<br/>{_rm(a['rm_per_week'])}" if a["kwh_per_week"] > 0.5
                  else escape(a.get("comfort") or "Data quality"))
        st = status.get(a["id"], "pending")
        colour = {"approved": "#1F7A6D", "dismissed": "#5C6B71"}.get(st, "#1D2A30")
        rows.append([_p(str(n), "cell"), _p(f"<b>{escape(a['title'])}</b><br/>{detail}", "cell"),
                     _p(saving, "cell"),
                     _p(f"{a['confidence_level']} ({a['confidence']})", "cell"),
                     _p(f'<font color="{colour}"><b>{st.capitalize()}</b></font>', "cell")])
    t = Table(rows, colWidths=[7 * mm, width - 7 * mm - 27 * mm - 22 * mm - 20 * mm, 27 * mm, 22 * mm, 20 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE), ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5), ("LEFTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [t, Spacer(1, 8)]

    approved = [a for a in actions if status.get(a["id"]) == "approved"]
    kwh = sum(max(a["kwh_per_week"], 0) for a in approved)
    total_possible = sum(max(a["kwh_per_week"], 0) for a in actions)
    if approved:
        line = (f"<b>Approved so far:</b> {len(approved)} change(s) worth {kwh:,.0f} kWh a week "
                f"({_rm(kwh * settings.tariff_rm_per_kwh)}), about {_rm(kwh * settings.tariff_rm_per_kwh * 52)} a year "
                f"if sustained. All ranked changes together: {total_possible:,.0f} kWh a week.")
    else:
        line = (f"<b>No changes approved yet.</b> All ranked changes together are worth {total_possible:,.0f} kWh a week "
                f"({_rm(total_possible * settings.tariff_rm_per_kwh)}).")
    story += [_p(line), Spacer(1, 8), _p("About the data", "h2")]

    live = ("Live hardware (sensor node or edge camera) replaces the simulated readings for the rooms it covers; "
            "the other readings") if mode.get("live_node_online") else "Sensor readings"
    story.append(_p(
        f"Rooms and classes are APU's real timetable for the week of {date.fromisoformat(WEEK_OF).day} {date.fromisoformat(WEEK_OF):%B %Y} "
        f"(public APSpace timetable feed). "
        f"{live} are simulated and labelled as such. Seat counts, AC loads and class sizes are estimates "
        f"(about 12 students per intake); energy is priced at RM {settings.tariff_rm_per_kwh:.2f}/kWh. "
        f"Every saving above comes from the SQL and rows shown in the CampusPulse dashboard. "
        f"Brief written by {escape(brief.get('source', 'template'))}.", "small"))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.2)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, 7 * mm, "CampusPulse · APU Cloud Innovation Hackathon 2026 · Track 3")
        canvas.drawRightString(A4[0] - 16 * mm, 7 * mm, "Decisions are logged to BigQuery with who approved them and when.")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()

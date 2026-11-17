# The agent workflow

One **Gemini orchestrator** runs CampusPulse. Everything reaches it: a person's request, an event
from the sensors, and the result of a check after it acted. It decides which specialist agent or
tool to call, in what order, and closes the loop. Three rule-based parts (no LLM) keep it honest:
the **watcher** decides when to wake it, the **policy guard** decides whether its changes happen,
and the **verifier** decides whether they worked.

```
                    ┌──────────────── people ─────────────────┐
                    │ Agents tab · Gemini Enterprise (A2A)    │
                    └───────────────────┬─────────────────────┘
                                        │ request
sensor readings ──► WATCHER (rules) ──event──► ┌──────────────────────────────┐
(ESP32, Wokwi,        AC on, nobody in         │   ORCHESTRATOR (Gemini)      │
 simulator)           CO2 over limit           │   function calling, ≤6 turns │
                                               └──┬───────────────────────────┘
          specialist agents                       │ tools
          ├─ SQL analyst (Gemini, self-correcting SQL)       ├─ inspect_room   (live state, timetable, trust, 7-day history)
          ├─ Brief writer (Gemini + checker)                 ├─ recent_incidents
          └─ Planner: goal reader → optimiser → twin →       ├─ set_ac_mode ──► POLICY GUARD (rules)
             explainer (Gemini + solver)                     │                    ├─ safe → device command (MQTT / HTTPS) → the room obeys
                                                             │                    └─ risky → held: a person approves in the feed
                                                             ├─ schedule_check ─► VERIFIER (rules): re-reads the room
                                                             │                    └─ result goes back to the orchestrator
                                                             ├─ flag_for_estates
                                                             └─ close_incident ─► CHECKER: no figure the tools didn't give
```

## One incident, step by step (what the feed shows)

Measured on the real loop with Gemini, playing the ESP32 in B-08-05:

| Time | Who | What |
|---|---|---|
| 0 s | Presenter | Presses BOOT: B-08-05 is now **empty**, AC still on (building schedule) |
| 20 s | Watcher (rules) | Condition held 20 s → opens an incident, wakes the orchestrator |
| 22 s | Orchestrator | `inspect_room`: empty, AC on, sensor trust 100, *class booked now (10:45–12:45) but nobody in* |
| 24 s | Orchestrator | Decides: a no-show, so set AC to **occupancy** mode, with that reason |
| 24 s | Policy guard | Room empty, sensor trusted, under the daily limit → **approved**, command sent |
| ~29 s | ESP32 | Pulls the command (every 5 s): **the blue LED goes off** |
| 25 s | Orchestrator | `schedule_check`: expects the AC off |
| ~40 s | Verifier | Board's next reading says AC off → **check passed** → back to the orchestrator |
| ~45 s | Orchestrator | Closes the incident: "resolved", note checked by the checker |

Press BOOT again (occupied): in occupancy mode the LED comes straight back on. That's the comfort side.
**Reset B-08-05 AC** in the feed puts the room back on the building schedule, so you can show it again.

## What the guard holds for a person

Less cooling needs all of these: the room is empty, its sensor is trusted (confidence ≥ 80), autonomy
is on, and fewer than 20 automatic changes today. Otherwise the incident shows **Needs you** with
Approve and Dismiss buttons. More cooling (back to the schedule) is always allowed. Untick
**Autonomy on** to show that every change then waits for a person.

## Prompting

- **One system prompt** (`ORCH_SYSTEM` in `campuspulse/agent/team.py`, visible in the Agents tab) with
  separate rules for a REQUEST, an EVENT and a VERIFICATION, so one agent behaves correctly in all three.
- **Tools, not memory**: the orchestrator never states a figure on its own; every number comes from a
  tool result, and the checker rejects anything else (one rewrite, then the tools' own words).
- **Decisions carry evidence**: `set_ac_mode` requires a one-sentence reason, shown in the log.
- **Fixed action menu**: the orchestrator can only call declared tools with enum-checked arguments.
- **Fallback**: with Gemini off or failing, a rule playbook runs the same tools in a fixed order, so the
  loop still closes, and the feed says which one ran.

## Try it

- **Live board**: flash `firmware/campuspulse_board` (it starts "occupied"), then press BOOT.
- **Without a board**: the Wokwi node or `edge/virtual_node.py` trigger the same loop (labelled virtual).
- **Ask about it**: in the Agents tab, "What have you handled on campus today, and is anything waiting for me?"
- **API**: `GET /api/ops` (the feed), `POST /api/ops/autonomy`, `POST /api/ops/incidents/<id>/approve|dismiss`,
  `POST /api/ops/reset`.

Simulated rooms are watched too, rate-limited to one new incident every 10 minutes campus-wide
(`CP_AGENT_WATCH_SIM=false` turns that off), so the feed shows the agents working across campus.

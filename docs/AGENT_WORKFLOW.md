# The agent workflow

CampusPulse is run by **one Gemini orchestrator that directs a team of agents**. The orchestrator never
does the work itself. For every input (a person's request, an event from the watcher, a verifier's result)
it decides which agent works next and briefs each one with the previous one's result. Each agent has its own
Gemini prompt and its own tools. Three rule components (no LLM) keep the team honest:
- the **watcher** decides when to wake the orchestrator;
- the **policy guard** decides whether a change happens;
- the **verifier** decides whether it worked.

Watch it live on the **Orchestrator** tab: a map of the team that lights up as each agent works, a step
ticker, and the activity log with **Replay**.

```
 sensor readings ─► WATCHER (rules) ─event─┐            ┌── people (Agents tab, Orchestrator tab, A2A)
                                           ▼            ▼
                              ┌──────────────────────────────────┐
                              │  ORCHESTRATOR (Gemini)           │  decides who works, in what order
                              └──┬───────┬───────┬───────┬──────┬┘
            ┌────────────────────┘       │       │       │      └──────────────┐
            ▼                            ▼       ▼       ▼                     ▼
   INVESTIGATOR (Gemini)        SQL ANALYST  PLANNER  BRIEF WRITER     REPORTER (Gemini)
   tools: inspect_room,         (Gemini,     (goal reader → optimiser   writes the log note
          room_history          self-fixing   → digital twin →
                                SQL)          explainer)
            ▼
   CONTROLLER (Gemini) ── set_ac_mode ──► POLICY GUARD (rules) ──safe──► ROOM / AC (command)
   the only agent that can act              └── risky ──► held: a person approves
            ▼
   VERIFIER (rules): re-reads the room (live node) or re-runs its digital twin (simulated room)
            └── result goes back to the ORCHESTRATOR → Reporter → close
   CHECKER (rules) sits behind every Gemini agent: no figure that its tools didn't supply.
```

## One incident, as the map shows it (measured live with Gemini)

| ~Time | Who | What |
|---|---|---|
| 0 s | Watcher (rules) | *Scan campus now*: 4 rooms cooled while empty → opens an incident for AUD-4 |
| 2 s | Orchestrator | Delegates to the **Investigator**: "Is the AC running with nobody in, and is it safe to turn off?" |
| 4 s | Investigator | `inspect_room` (empty, AC on, next class 13:30, sensor trust 100) and `room_history` (34 empty-cooled hours in 7 days) → **Checker** ✓ → reports and recommends |
| 8 s | Orchestrator | Delegates to the **Controller**: "Turn off the AC: empty, no class until 13:30" |
| 10 s | Controller | `set_ac_mode` occupancy → **Policy guard** approves (empty, trusted) → command to the room |
| 12 s | Orchestrator | Asks the **Verifier** to check, and the **Reporter** for a note |
| 30 s | Verifier | Digital twin re-run with the new mode: AC off → **passed** → back to the Orchestrator |
| 45 s | Orchestrator | Reporter writes the final note → **Resolved** |

On its own, the watcher also catches problems between scans (at most one new incident every 10 minutes for
simulated rooms; immediately for live or virtual nodes). With high CO2 in a full classroom, the orchestrator
does not touch the AC: the Investigator reports, the Reporter flags it for Estates, and it is marked **Escalated**.

## What the guard holds for a person

Less cooling needs all of these: the room is empty, its sensor is trusted (confidence ≥ 80), autonomy is
on, and fewer than 20 automatic changes today. Otherwise the activity shows **Needs you** with Approve and
Dismiss. More cooling is always allowed.

## Prompting

- **Each agent has its own prompt** (visible in the Agents tab): Orchestrator (`ORCH_SYSTEM`, with separate
  rules for a REQUEST, an EVENT and a VERIFICATION), Investigator, Controller, Reporter, plus the existing SQL
  analyst, goal reader, plan explainer, brief and notice writers.
- **Delegation, not memory**: the orchestrator's tools *are* the agents; it never states a figure itself.
- **Least privilege**: only the Controller can ask for a change, and only through the guard.
- **Evidence travels**: the Investigator's findings brief the Controller; the Controller must give a reason;
  the Reporter writes from the recorded facts.
- **Generator and critic**: every agent's words go to the checker; a failed check falls back to rules.
- **Fallback**: with Gemini off, the orchestrator follows a fixed playbook over the same agents' rule versions.

## API

`GET /api/ops` (activity and steps) · `POST /api/ops/scan` · `POST /api/ops/autonomy` ·
`POST /api/ops/incidents/<id>/approve|dismiss` · `POST /api/agent` (a request to the orchestrator).

# CampusPulse: test cases and demo guide (finals, 17 Nov 2026)

Slot: **7 minutes pitch + live demo, then 3 minutes Q&A.**
Judging: Problem Value 10% · Agentic Workflow 20% · **Prompting & Agent Orchestration 30%** · App Output 20% ·
Business Value 10% · Live Pitch & Q&A 10%.

How to use this guide:
- **Part A** is what to set up.
- **Part B** is the test cases. Run all of them once in the morning and again 30 minutes before your slot.
- **Part C** is the script, with what to do and what to say.
- **Part D** is Q&A.
- **Part E** is what to do when something breaks.

This guide uses **no physical hardware**. The agents work on the campus's simulated rooms, which follow
APU's real timetable and real weather.

Read figures off the screen, never from memory: they change with the data.

---

## Part A: Set up (do this before you leave)

| # | Item | Done |
|---|---|---|
| A1 | Code committed and pushed. If you edited `firmware/campuspulse_board/campuspulse_board.ino` with the real ingest key, leave that file out of the commit | ☐ |
| A2 | Shermaine redeployed Cloud Run (`NIGHTLY=1`, and for today `--no-cpu-throttling --min-instances 1`, so the agents keep working between page refreshes) | ☐ |
| A3 | Laptop charged, screen sleep off, browser zoom 100%, notifications muted, venue Wi-Fi **or** phone hotspot working | ☐ |
| A4 | Browser tabs, in this order: **1** Today · **2** Orchestrator · **3** Agents · **4** Autopilot · **5** the deck | ☐ |
| A5 | Present **during teaching hours (07:30–21:00, weekday)**: outside them no AC runs, so the watcher has nothing to find | ☐ |

Live site: `https://campuspulse-443933554054.asia-southeast1.run.app`
(Orchestrator tab: add `#orchestrator` to the address.)

---

## Part B: Test cases

Mark each one Pass or Fail. If one fails, use its "If it fails" column, or Part E.

### B1. Cloud and AI are up

| ID | Steps | Expected | If it fails |
|---|---|---|---|
| TC-01 | Open `<live site>/api/health` | `"ok":true`, `"backend":"bigquery"`, `"gemini":true` | Ask Shermaine to check the deploy. If `gemini:false`, everything still runs on rules and templates (labelled) |
| TC-02 | Open the Today tab | The headline says *"Brief written by Gemini … figures checked against the engine"*; 4 KPI tiles; 20 room cards; "Whole campus" shows 213 rooms | Press **Refresh** on the brief |
| TC-03 | Header | **KL xx°C** (real weather) | – |

### B2. The Orchestrator tab: agents acting on their own (the headline tests)

| ID | Steps | Expected | If it fails |
|---|---|---|---|
| TC-04 | Open the **Orchestrator** tab | A map: the Orchestrator in the middle; Investigator, SQL analyst, Planner, Brief writer, Controller and Reporter (blue, Gemini agents); Watcher, Policy guard, Verifier and Checker (grey dashed, rules); People; Room / AC. **Autonomy on** is ticked | Reload the page |
| TC-05 | Press **Scan campus now** | Toast "The watcher found <room>: the orchestrator is on it." The map animates: Watcher → Orchestrator → **Investigator** (inspects the room, reads its 7-day history) → **Checker** → Orchestrator → **Controller** → **Policy guard** → **Room / AC** → **Verifier** → **Reporter** | "No room needs attention right now": you're outside teaching hours (A5), or every cooled-while-empty room is already handled. Try **Ask** (TC-09) instead |
| TC-06 | Wait about 45 s, watching the right-hand ticker | The ticker shows each step with its time. The Verifier says *"digital twin re-run with AC mode 'occupancy': AC off"* → **check passed** → the Reporter writes the note → the activity shows **Resolved** | If it says "Escalated", read the Verifier line: the system is working as designed (the action didn't take effect, so a person is told) |
| TC-07 | Scroll to **Activity** → open the incident | The full trace: every agent and what it did, with evidence. The footer says *Run by Gemini orchestrator* | "rule playbook" means Gemini was unavailable; still valid and honest |
| TC-08 | Press **Replay** on that incident | The map plays the whole run again, step by step | – |
| TC-09 | Untick **Autonomy on** → press **Scan campus now** | The Controller asks, and the Policy guard answers *held for a person: autonomy is switched off*. The activity shows **Needs you** with **Approve / Dismiss** | – |
| TC-10 | Press **Approve** on it | The command is sent → Verifying → Resolved. **Tick Autonomy on again afterwards** | – |
| TC-11 | Watch for a few minutes with the tab open | The watcher also opens incidents by itself (at most one every 10 minutes), e.g. CO2 over 1,000 ppm in a full classroom → the Orchestrator does **not** touch the AC → the Reporter flags it → **Escalated** | – |

### B3. The orchestrator answering people

| ID | Steps | Expected | If it fails |
|---|---|---|---|
| TC-12 | Orchestrator tab → **Ask: what have you handled today?** | The map lights People → Orchestrator → (recent incidents) → reply. The activity list shows a **Request**, marked **Answered** | – |
| TC-13 | **Ask: why is Auditorium 2 wasting energy?** | ~20 s: People → Orchestrator → **SQL analyst** (writes SQL) → **Planner** (goal reader → optimiser → digital twin → explainer) → Checker. The Agents tab shows the reply | Slow: keep talking (Part C has the line) |
| TC-14 | Agents tab → type your own question, e.g. *"Check AUD-1 and tell me if anything should change"* | The orchestrator picks the agents itself (here the Investigator); its trace appears on the Orchestrator map too | – |
| TC-15 | Agents tab → *Who is on the team* → open **Orchestrator**, **Investigator**, **Controller** → *Read its prompt* | Each agent's own system prompt is visible | – |

### B4. Other features

| ID | Steps | Expected | If it fails |
|---|---|---|---|
| TC-16 | Data tab → *"Which rooms wasted the most energy cooling while empty this week?"* | An answer, a chart, and the SQL Gemini wrote (read-only guard) | Try another suggested question |
| TC-17 | Autopilot → click *Comfort first…* → **Plan tomorrow** | A saving in kWh and RM, the semester figure, room changes marked AUTO or needs you, a line saying the figures were checked | Re-press if Gemini is busy (the template is labelled) |
| TC-18 | Actions tab → *Show the evidence* on the top card | A chart with a Gemini caption, plus the SQL | – |
| TC-19 | Today → *Real sensor data* → **Published dataset (Mons office)** → **Start**, then **Off** | The study's own office with real readings and a CO2/light chart; then back to normal | – |
| TC-20 | Today → demo clock → a weekday, 10:30 → **▶ Play the day** | Rooms fill and empty across the day | – |
| TC-21 | Students tab | Free rooms, already-cooled ones first | Closed outside teaching hours |
| TC-22 | **Report (PDF)** | A PDF downloads | – |
| TC-23 | Data tab → *Google Cloud and integrations* | Gemini, Cloud Run, BigQuery, Pub/Sub, Sheets, Calendar **ON**; Gemini Enterprise **READY** | – |
| TC-24 | `<live site>/.well-known/agent-card.json` | The A2A agent card with 3 skills | – |

---

## Part C: The 7-minute script

Two speakers. **Y** = Yong Han (drives the laptop), **S** = Shermaine (talks to the judges). Swap as you like.
*Italics* = what to do. Plain text = what to say.

### 0:00–0:50 · The problem (Problem Value)

*Deck: title, then the problem slide.*

**S:** "Every campus in Malaysia runs air-conditioning on a fixed timetable: on at 7:30, off at 9 pm, whether
anyone is in the room or not. Auditoriums get cooled for classes that never turn up, and small rooms get so
crowded the air goes stale."

**S:** "CampusPulse is a team of AI agents, directed by one orchestrator, that watches every room, fixes what's
safe to fix, asks a person about the rest, and checks that its fix worked."

### 0:50–1:40 · What it sees (App Output)

*Tab 1, Today.*

**Y:** "This is the live system on Google Cloud. The brief is written by Gemini, and every figure is checked
against our engine before you see it. *(Point at the tiles.)* Last week, this much went on cooling empty rooms."

**Y:** "These are real APU rooms on APU's real public timetable, with the real KL weather. The sensor readings
are simulated until we install hardware, and the screen says so."

### 1:40–3:50 · The orchestrator directs its agents (Agentic Workflow + Orchestration: the key moment)

*Tab 2, Orchestrator.*

**Y:** "This is the agent team. In the middle is the orchestrator, a Gemini agent. Around it, its specialists:
blue ones are Gemini agents with their own prompts and tools, grey ones are plain rules that keep the AI honest."

*Press **Scan campus now**.*

**Y:** "I've asked the watcher to look at every room right now."

*As the map animates, point at each node as it lights up.*

**S:** "The watcher found an auditorium with the AC running and nobody inside. It doesn't decide anything: it
wakes the orchestrator. The orchestrator doesn't do the work itself either. It sends the Investigator, which has
its own tools: it reads the room, today's timetable, how far the sensor can be trusted, and the last seven days."

**S:** "The Investigator reports back: empty, no class until this afternoon, and this happens every week. The
checker confirms every number it used came from the data."

**Y:** "Now the orchestrator briefs the Controller, the only agent allowed to touch the AC. It asks to switch the
room to follow occupancy. But even the Controller can't just do it. The policy guard, plain code with no AI,
checks the room is empty and the sensor trusted, and approves it. The command goes to the room."

**S:** "Then the verifier re-checks the room, using the room's digital twin, to prove the AC is now off. The AI
doesn't get to mark its own homework. And the Reporter writes the note for Estates."

*Point at **Resolved** in Activity.*

**Y:** "Resolved, in under a minute, and nobody pressed anything but 'scan'. Normally the watcher does this by
itself on every sensor reading."

*Untick **Autonomy on** → **Scan campus now** → wait for **Needs you**.*

**S:** "And when Estates wants control, autonomy off: the guard holds every change for a person. *(Press
Approve.)* One click, and the same loop finishes."

*Tick Autonomy on again.*

### 3:50–4:50 · People ask the same orchestrator (Prompting & Orchestration)

*Press **Ask: why is Auditorium 2 wasting energy?***

**S:** "The same orchestrator answers people. This is a two-part question, so it plans: first the SQL analyst
queries BigQuery, then the Planner, which is itself a pipeline: a goal reader, a real optimiser, a digital twin and
an explainer. Watch them light up."

*While it runs (~20 s):*

**Y:** "Every agent has its own prompt you can read *(Agents tab → open a card → Read its prompt)*, a fixed set of
tools, and a checker behind it: if Gemini writes a number our engine didn't produce, it's rejected."

### 4:50–5:40 · Plan tomorrow, the human stays in charge (App Output + Business Value)

*Tab 4, Autopilot → click a goal chip → **Plan tomorrow**.*

**Y:** "Estates sets goals in plain English. The optimiser re-assigns rooms and schedules the AC around tomorrow's
real weather forecast."

**S:** "That's tomorrow's saving in kilowatt-hours and ringgit, and over a semester it's this. *(Read the
semester figure.)* Safe changes apply automatically; risky ones wait for a person."

### 5:40–6:30 · Built on Google Cloud (Business Value)

*Data tab → integrations.*

**Y:** "Cloud Run, BigQuery and BigQuery ML forecasts, Pub/Sub for sensor data, Gemini on Vertex AI. Tomorrow's
room changes go straight into Google Sheets and Google Calendar. And CampusPulse is an A2A agent, so it can join
Gemini Enterprise and answer staff there."

### 6:30–7:00 · Close

**S:** "One orchestrator, a team of specialist agents, and rules that keep them honest. It finds the waste,
fixes what's safe, asks about the rest, and proves it worked. Our ask: one pilot floor in Block B with real
sensors for one semester. Thank you."

*End on the Orchestrator tab, with the resolved incident visible.*

---

## Part D: Q&A (3 minutes)

| They ask | You say |
|---|---|
| **Is this real data?** | "APU's timetable and KL weather are real. The room sensors are simulated until we install hardware, and every simulated reading is labelled. The agents, the cloud, Gemini and the optimiser are all real and running live." |
| **What makes it an orchestrator, not one chatbot?** | "The orchestrator never does the work. It decides which agent works next and passes each one the last one's result: the Investigator investigates, the Controller acts, the Reporter writes. Each has its own prompt and tools, and you just watched the order on the map." |
| **What stops the AI going wrong?** | "Three rule components. The Policy guard decides whether a change happens. The Verifier checks it worked. The Checker rejects any number Gemini didn't get from the data. With autonomy off, everything waits for a person." |
| **Why Gemini and not plain rules?** | "Rules spot the problem: that's the watcher. Gemini handles judgement: reading the timetable to see a no-show, choosing the least disruptive fix, turning 'comfort first, don't move Friday' into solver constraints, writing SQL for any question." |
| **What if Gemini is down?** | "Every agent has a rule fallback, and the orchestrator has a fixed playbook, so the loop still closes. The screen says which ran." |
| **How much does it save?** | "Read the semester figure on Autopilot. It's modelled on APU's real timetable and weather; a pilot replaces the simulation with measured savings." |
| **Cost?** | "Sensors are tens of ringgit per room. Cloud Run scales down; BigQuery and Gemini Flash-Lite stay in the free or low tiers at campus scale. Gemini runs once per incident, not per reading." |
| **Privacy?** | "Occupancy comes from motion or CO2, not cameras. Lecturer data is removed from the timetable." |
| **Could it switch off AC during a class?** | "No. The guard blocks less cooling in an occupied room, occupancy mode still pre-cools before booked classes, and more cooling is always allowed." |
| **What's next?** | "A one-floor pilot in Block B, measured savings over a semester, then Estates using it through Gemini Enterprise." |

---

## Part E: When something breaks

| # | Problem on stage | Do | Say |
|---|---|---|---|
| F1 | "No room needs attention right now" | Press **Ask: why is Auditorium 2 wasting energy?** instead. Or press **Replay** on an earlier incident | "The campus is behaving right now, so here's the team working on a request." |
| F2 | The map doesn't move after Scan | Reload the tab, press Scan again. Or Replay an earlier incident | "Let me refresh the live view." |
| F3 | The orchestrator is slow (>20 s) | Keep talking about the guard and the verifier | "It's calling Gemini and BigQuery live; each step appears as it lands." |
| F4 | The trace says "rule playbook" or "template" | Carry on | "Gemini was busy, so the fallback ran: same agents' roles, same guard, and it tells you honestly." |
| F5 | An incident ends **Escalated** | Open it, read the Verifier line | "This is the safety net working: the change didn't take effect, so a person is told." |
| F6 | Live site down | Run locally: `.venv/Scripts/python -m uvicorn campuspulse.api:app --port 8000`, then open `http://127.0.0.1:8000/#orchestrator` | "We'll run the same build locally." |
| F7 | Outside teaching hours | Today → demo clock → Play the day; on the Orchestrator tab use **Ask** and **Replay** | "Here's a teaching day replayed from the data." |

---

## Rehearsal checklist (30 minutes before)

- [ ] TC-01, TC-02, TC-05/06 pass on the **live site**
- [ ] Autonomy ticked
- [ ] One **Ask** has run (it warms Gemini and Cloud Run)
- [ ] At least one **Resolved** incident is in Activity, ready for **Replay** if needed
- [ ] You agree on the team name you'll say (the agent card says "What's Google?")
- [ ] You've timed the script once: about 7:00

# CampusPulse demo guide (3 minutes)

**One line:** CampusPulse plans APU's rooms and air-conditioning for the next teaching day, proves the saving,
and switches the AC when Estates approves.

## Before you start (5 minutes)

Three terminals in the project folder:

```bash
# 1. the app (reads .env: Gemini key, MQTT broker and prefix)
.venv/Scripts/python -m uvicorn campuspulse.api:app --port 8000
```

```bash
# 2. the MQTT bridge: public broker -> the app
.venv/Scripts/python -m campuspulse.mqtt_bridge --broker broker.hivemq.com --prefix apu-campuspulse-2026 --api http://localhost:8000
```

```bash
# 3. the camera node (optional; first install: .venv/Scripts/python -m pip install -r edge/requirements.txt)
.venv/Scripts/python edge/people_counter.py --room B-08-05 --server http://localhost:8000 --show
```

Then open <http://localhost:8000> and start the Wokwi project (see [firmware/wokwi/README.md](../firmware/wokwi/README.md)).
No Wokwi? Run `.venv/Scripts/python edge/virtual_node.py --people 30` instead.

Checklist: header shows **KL xx°C** (real weather), B-08-05 shows **live** or **camera count**, footer says **Gemini**.

## The script

| Time | Screen | Say | Do |
| --- | --- | --- | --- |
| 0:00 | **Today** | "APU cools rooms on a fixed timetable. Last week 41% of cooling went to empty rooms." | Point at the four tiles. Press **▶ Play the day**: rooms fill and empty; blue stripes = cooled while empty. |
| 0:30 | Wokwi + Today | "This is a real ESP32 sketch talking MQTT. Sensors on campus look exactly like this." | Click motion on the PIR, turn the CO₂ knob up: B-08-05 turns orange. Wave at the webcam: the head count changes. Frames never leave the laptop. |
| 1:00 | **Autopilot** | "Instead of a list of tips, CampusPulse plans tomorrow itself." | Press **Plan tomorrow**. |
| 1:15 | Autopilot result | "It forecast every class from the sensors, read tomorrow's real KL forecast, and an optimiser re-assigned the rooms. It saves about 40% of tomorrow's cooling and cuts stale-air hours." | Point at the 3-step bar and the chart (hot afternoon = more AC). |
| 1:40 | Autopilot | "Estates sets the goals in plain English. Gemini turns them into solver rules. It never makes up a number." | Type *Don't move Friday classes* or click *Comfort first*; re-plan; the saving changes. |
| 2:05 | Autopilot | "Safe changes apply automatically. Risky ones (a block change, an untrusted sensor) wait for a person." | Point at **auto** vs **needs you**. Press **Apply safe changes**. |
| 2:20 | Wokwi | "And the rooms obey." | Serial monitor: *AC mode -> occupancy*. Stop the motion: the blue AC LED goes off. |
| 2:35 | Autopilot | "Notices for students are drafted, every decision is logged, and the forecast was checked against last week." | Show the notices and the trust line. |
| 2:50 | **Students** | "Students see free rooms that are already cooled, so they use cooling the campus is paying for anyway." | Close. |

## If something goes wrong

- **Gemini busy (503/504):** everything still works; the words come from templates and the screen says so.
  Re-press *Plan tomorrow* later for Gemini's wording.
- **Wokwi will not connect:** use `edge/virtual_node.py`; it sends the same messages.
- **Campus looks empty at night:** use the demo clock, or open `/?at=2026-11-11T14:00`.
- **Port in use:** use `--port 8001` and the same port in the bridge's `--api`.
- **Fresh data:** stop the app, delete `data/campuspulse.duckdb`, start again (28 days are rebuilt in ~10 s).

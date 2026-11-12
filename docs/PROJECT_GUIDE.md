# CampusPulse: project guide

A walkthrough of the whole project: what it does, how the parts fit together, and what every module and main function is for.

APU Cloud Innovation Hackathon 2026 · Track 3: Data-Driven Research & Intelligent Decision Making
Repository: https://github.com/ShermaineYap/APU-Cloud-Innovation-Hackathon-2026

---

## 1. What CampusPulse does

CampusPulse watches APU's teaching rooms, works out where energy and comfort are going wrong, and tells the Estates team what to change, with the evidence attached.

| Feature | What the user sees | Where it lives |
| --- | --- | --- |
| Morning brief | Headline, summary and first steps written by Gemini from the engine's own numbers | `agent/gemini.py` → `daily_brief` |
| What to change | Ranked actions, each costed in kWh and ringgit, with a chart, the rows and the SQL behind it | `decisions.py` |
| Approve / dismiss | Decisions logged to the warehouse with who and when | `api.py` → `decide` |
| What if | Select changes and the week's savings are recalculated instantly | `web/static/app.js` → `renderProjection` |
| Estates report | One-page PDF for Facilities | `report.py` → `build_pdf` |
| Chart captions | One sentence per evidence chart, written from the chart's numbers only | `agent/gemini.py` → `chart_caption` |
| Ask the data | Plain-English question → Gemini writes SQL → read-only guard → warehouse → answer, table, chart | `agent/gemini.py` → `ask`, `agent/sqlguard.py` |
| Floor plan | 20 real APU rooms coloured by state: in use, cooled while empty, CO₂ high, offline | `analytics.py` → `room_states` |
| Room drawer | Last 48 hours, tomorrow's forecast, unusual energy hours | `analytics.py` → `room_detail` |
| Can we trust the data? | 0–100 confidence per sensor node from delivery, signal, freshness and stuck-sensor checks | `network.py` |
| Edge camera | YOLO counts people on the device; only the number is sent | `edge/people_counter.py` |
| Autopilot | Plan the next teaching day: forecast, MILP room assignment, weather-based AC schedule, comfort and trust checks, digital twin, apply | `autopilot.py`, `api.py` → `autopilot_plan` / `autopilot_apply` |
| Goals in plain English | Gemini function calling turns goals into solver constraints; it also explains the plan and drafts notices | `agent/gemini.py` → `plan_constraints`, `explain_plan`, `draft_notices` |
| Real weather | Hourly KL temperature (Open-Meteo) drives the simulator, the planner and the header | `weather.py` |
| Virtual node | Wokwi ESP32 sketch and a Python stand-in on a public MQTT broker | `firmware/wokwi/`, `edge/virtual_node.py` |
| Closed loop | Approve an AC change → each room gets its new AC mode over MQTT (or HTTPS pull); simulated rooms obey | `control.py`, `api.py` → `decide`, firmware relay on GPIO26 |
| Carbon | kg CO₂e beside every kWh and ringgit figure | `config.py` → `grid_kg_co2e_per_kwh` |
| Where can I study? | Free rooms for students, already-cooled rooms first | `study.py` → `free_rooms` |
| Demo clock | Replay the floor plan at any hour of the last 7 days; gaps back-filled on start | `analytics.py` → `room_states(at=)`, `app.js` → `goReplay` |

---

## 2. How it fits together

```
 SENSE                      CARRY                         ANALYSE                          DECIDE / ACT
 ─────                      ─────                         ───────                          ────────────
 ESP32 node (CO2, temp,     MQTT QoS 1 ─► mqtt_bridge ─┐
   PIR, current clamp)      or HTTPS POST ─────────────┤
                                                       ├─► /api/ingest ─► Pub/Sub (cloud) ─► readings table
 Camera + YOLO26            HTTPS POST, queued retries ┤                                      │
   (people count only)                                 │                                      ▼
                                                       │                       curated views (v_room_hourly …)
 Simulator (other rooms)  ─ every 30 s ────────────────┘                                      │
                                                                         ┌────────────────────┼────────────────────┐
 APU timetable feed ─► import script ─► apu_timetable.json ─► campus.py │ decisions.py       analytics.py   network.py
                                                                         │ (5 detectors)      (KPIs, forecast) (confidence)
                                                                         └─────────┬──────────┘
                                                                                   ▼
                                                              Gemini: brief, captions, ask-the-data SQL agent
                                                                                   ▼
                                                     FastAPI ─► dashboard (floor plan, actions, What if, ask, report PDF)
```

Two storage backends share one code path: all SQL is written in BigQuery dialect, and locally it is transpiled to DuckDB by sqlglot. So the same app runs on a laptop with no cloud account, or on Cloud Run with BigQuery.

---

## 3. What is real and what is simulated

| Real | Simulated or assumed |
| --- | --- |
| 20 APU rooms and their codes (Auditoriums 1–6, Block B classrooms, Tech Labs) | Sensor readings for rooms without hardware (labelled `simulated = true`) |
| The teaching week of 2 Nov 2026: module codes and names, exact times, intakes per session (APU public timetable feed) | Seat capacity and AC load per room |
| Your ESP32 node (B-08-05) and camera node, when running | Class size: 12 students per intake, capped at the room's seats |
| | Energy price: RM 0.50/kWh (set `CP_TARIFF_RM_PER_KWH`) |

Planted problems the engine must find on its own (and the tests check it does): weekend AC left on in Auditorium 2, a weak network link in Tech Lab 7-01, a stuck CO₂ sensor in Tech Lab 4-02, a four-hour outage of the Tech Lab gateway, and thin Friday-afternoon auditorium attendance.

---

## 4. Folder map

```
campuspulse/            the service
  api.py                web server: ingest, decision API, PDF, web app
  config.py             settings from environment and .env
  campus.py             the 20 real rooms and their timetable
  simulator.py          synthetic sensors following the real timetable
  analytics.py          room states, KPIs, forecasts, anomalies
  decisions.py          the five detectors and the ranking
  network.py            link health and confidence scores
  report.py             one-page Estates report (PDF)
  mqtt_bridge.py        MQTT broker → /api/ingest
  agent/                Gemini: brief, captions, SQL agent, SQL guard
  store/                BigQuery and DuckDB backends
  data/                 APU timetable snapshot, optional room overrides
bigquery/               schema.sql, views.sql, ml.sql (BigQuery ML ARIMA_PLUS)
web/static/             index.html, styles.css, app.js (the dashboard)
edge/                   YOLO people counter (camera node, optional)
firmware/               ESP32 sketch (MQTT QoS 1 or HTTPS)
scripts/                BigQuery setup, Gemini check, timetable import
deploy/                 Cloud Run deploy script
tests/                  29 tests
docs/                   architecture diagram, dashboard screenshot, this guide
```

---

## 5. Module reference

### `campuspulse/config.py`: settings
Reads every setting once at start-up.

| Function / class | What it does |
| --- | --- |
| `_load_dotenv(path)` | Loads `KEY=value` lines from `.env` into the environment. Variables already set in the shell win, so Cloud Run and CI are unaffected. |
| `_bool(name, default)` | Reads a true/false environment variable. |
| `Settings` | All settings in one place: backend (`local` or `bigquery`), DuckDB path, Google Cloud project, BigQuery dataset and region, Pub/Sub topic, Gemini key / Vertex mode / model, ingest key, live simulator on/off, history days, tariff, CO₂ limit, time zone. |
| `Settings.gemini_enabled` | True when an API key is set, or Vertex AI is on with a project. |

Main environment variables: `GEMINI_API_KEY`, `CP_GEMINI_MODEL` (default `gemini-3.5-flash-lite`), `CP_BACKEND`, `GOOGLE_CLOUD_PROJECT`, `CP_INGEST_KEY`, `CP_TARIFF_RM_PER_KWH`, `CP_LIVE_ROOM`.

### `campuspulse/campus.py`: the real campus
| Function / class | What it does |
| --- | --- |
| `Room` | One room: id, display name, block (`AUD`, `B`, `TL`), floor, kind, seats, AC kW, gateway, floor-plan position, exact APU timetable name. |
| `_overrides()` | Reads optional real seat counts / AC loads from `data/room_overrides.csv`. |
| `_rooms()` | Builds the 20 rooms, applying any overrides. |
| `Slot` | One class session: room, weekday, start/end hour, module, estimated students, exact minutes, intakes. |
| `Slot.time_label` | e.g. `08:30–10:30`. |
| `_hour(minutes)` | Rounds a time to the nearest hour so back-to-back classes never overlap. |
| `_timetable()` | Builds every session for the 20 rooms from `apu_timetable.json`. |
| `slots_for(room_id, weekday)` | Sessions in a room on a given day. |

Constants: `LIVE_ROOM` (default `B-08-05`), `STUDENTS_PER_INTAKE` (12), `BLOCK_NAMES`, `BLOCK_LABEL`, `WEEK_OF`.

### `campuspulse/simulator.py`: synthetic sensors
| Function / class | What it does |
| --- | --- |
| `Fleet` | Steps every simulated room forward in 5-minute intervals. |
| `Fleet._people(room, time)` | People in the room: follows the real timetable, with attendance noise, 7% cancelled classes and the Friday-afternoon effect. |
| `Fleet._ac_on(room, time)` | AC on the fixed building schedule (07:30–21:00 weekdays), plus the planted weekend fault. |
| `Fleet.step(room, ts)` | One reading: CO₂ by mass balance, temperature by a first-order model, power from AC load; plus network effects (lost packets, weak signal, gateway outage, stuck sensor). |
| `Fleet.history()` | 28 days of readings to seed the warehouse. |
| `Fleet.tick(now)` | One live reading per room, every 30 seconds. |

### `campuspulse/store/`: storage
| Function / class | What it does |
| --- | --- |
| `get_store()` | Returns the BigQuery or DuckDB backend, once per process. |
| `enrich(row)` | Adds local-time columns (`ts_local`, `weekday`, `hour_local`). |
| `Store` | The interface: `setup`, `insert_readings`, `query`, `insert_decision`, `reading_count`, `clear_readings`, `stale_rooms`, `static_frames`. |
| `Store.stale_rooms()` | Detects readings from rooms no longer in the campus model, so old data is cleared automatically. |
| `DuckStore` | Local backend. `to_duckdb(sql)` transpiles BigQuery SQL with sqlglot. |
| `BigQueryStore` | Cloud backend. `qualify(sql)` adds the dataset prefix; queries are capped at 2 GB billed. |

### `bigquery/`: warehouse
| File | Contents |
| --- | --- |
| `schema.sql` | Tables `readings` (partitioned by day, clustered by room), `rooms`, `timetable`, `decisions`. |
| `views.sql` | `v_room_hourly` (one row per room per hour), `v_empty_cooled` (AC on, nobody in), `v_network_daily` (delivery and signal per node per day), `v_slot_attendance` (each class joined to what the sensors saw). |
| `ml.sql` | BigQuery ML `ARIMA_PLUS` models for occupancy forecasts and energy anomalies. |

### `campuspulse/analytics.py`: what is happening
| Function | What it does |
| --- | --- |
| `room_states(store)` | Current state of every room for the floor plan: in use, cooled while empty, CO₂ high, idle or offline. A fresh camera count overrides the people estimate; air and energy still come from the room's sensor. |
| `kpis(store)` | Last 7 days: kWh and ringgit spent cooling empty rooms, share of all cooling, stale-air hours, average seat use, fleet confidence. |
| `seasonal_forecast(hist)` | Next 24 hours from the same hour-of-week in past weeks, with a 10th–90th percentile band (the local stand-in for ARIMA_PLUS). |
| `detect_anomalies(hist)` | Unusual energy hours by robust z-score against the same hour-of-week. |
| `room_detail(store, room_id)` | Everything for the room drawer: 48-hour history, forecast, anomalies. |

### `campuspulse/network.py`: can we trust the data?
| Function | What it does |
| --- | --- |
| `is_camera(device_id)` | Camera nodes (`cam-…`) send a people count only. |
| `_score(delivery, rssi, age, distinct)` | 0–100 confidence: penalties for lost reports, weak signal (below −80 dBm), silence over 15 minutes, and a CO₂ value that never changes. |
| `link_health(store)` | Confidence and issues for every node over the last 24 hours. |
| `room_confidence(health)` | The best sensor per room; a fresh real node beats its simulated twin. |
| `gateway_outages(store)` | Hours when a whole gateway went silent. |

### `campuspulse/decisions.py`: what to change
| Function | What it does |
| --- | --- |
| `Action` | One recommendation: kind, title, summary, rooms, kWh/week, ringgit/week, comfort note, confidence, evidence SQL, rows and chart. |
| `after_hours_cooling` | Rooms cooled at night or at weekends with nobody inside. |
| `schedule_driven_cooling` | Per block: empty rooms cooled on weekdays, excluding the allowed 1-hour pre-cool before a class. |
| `right_size_rooms` | Small classes in big rooms, grouped per block, each matched to the smallest free room that fits with 15% headroom. Counts how many are for one or two intakes in APU's timetable. |
| `stale_air` | Classes whose CO₂ peaks at least 100 ppm over the limit, with a bigger free room or a fresh-air fix. |
| `data_reliability` | Weak links, stuck sensors and gateway outages that need fixing before their data is trusted. |
| `_smallest_free(...)` | The smallest room of the right kind that is free at that time. |
| `build_actions(store)` | Runs all detectors, applies confidence, scores and ranks: `(ringgit + comfort points + reliability points) × (0.5 + confidence/200)`. Keeps the top 3 stale-air actions. |

### `campuspulse/agent/`: Gemini
| Function | What it does |
| --- | --- |
| `daily_brief(actions, kpis)` | Gemini writes the headline, summary and per-action "why now" and "first step" in JSON mode, from numbers pre-formatted by `_model_payload`. Unknown action ids are dropped; first steps that only repeat the title are replaced. Falls back to `_fallback_brief` without Gemini. |
| `chart_caption(action)` | One sentence about an evidence chart, from its numbers only. `_fallback_caption` without Gemini. |
| `ask(store, question)` | The SQL agent: Gemini calls a `run_sql` tool; each query passes `guard`; errors go back to Gemini to fix (up to 5 turns). Returns answer, SQL, rows and chart. `_offline_answer` uses the 15 tested questions. |
| `guard(sql)` (`sqlguard.py`) | Allows exactly one read-only SELECT over the curated tables and views, no dataset prefixes, limit 200. Raises `UnsafeSQL` otherwise. |
| `SCHEMA_DOC` (`schema_doc.py`) | The data dictionary Gemini sees. |
| `CANNED`, `match` (`canned.py`) | 15 tested questions with their SQL, used as suggestions and in offline mode. |
| `chart_for(rows)` | Picks a sensible chart for a result table. |
| `_why(e)` | Turns a Gemini error into a short, key-free reason for the UI. |

### `campuspulse/report.py`: Estates report
| Function | What it does |
| --- | --- |
| `build_pdf(brief, actions, kpis, status, mode)` | One A4 page: headline and summary, five key figures, the 8 ranked changes with first step, saving, confidence and decision, the approved total, and a note on the data. |

### `campuspulse/api.py`: web server
| Endpoint | Function | What it does |
| --- | --- | --- |
| `POST /api/ingest` | `ingest` | Readings from sensor or camera nodes. Checks `X-Device-Key` first, then validates. Cameras may send people only; sensors must send CO₂, temperature and signal. |
| `POST /api/pubsub/push` | `pubsub_push` | Pub/Sub push target in the cloud. |
| `GET /api/health` | `health` | Version and mode (backend, Gemini, live node). |
| `GET /api/overview` | `overview` | KPIs and every room's current state. |
| `GET /api/actions` | `actions` | Ranked actions (cached 60 s). |
| `GET /api/brief` | `brief` | Gemini brief plus actions (`?refresh=true` to regenerate). |
| `POST /api/actions/{id}/decide` | `decide` | Approve or dismiss; logged to `decisions`. |
| `GET /api/decisions` | `decision_log` | The latest 50 decisions. |
| `GET /api/actions/{id}/caption` | `caption` | One-line chart caption (cached 1 h). |
| `GET /api/report.pdf` | `estates_report` | Downloads the Estates report. |
| `GET /api/rooms/{id}` | `room` | Room drawer data. |
| `GET /api/network` | `network_health` | Node confidence and gateway outages. |
| `POST /api/ask` | `ask_data` | Ask the data. |
| `GET /api/suggestions` | `ask_suggestions` | Suggested questions. |
| `GET /` | `index` | The dashboard. |

Internals: `Reading.to_row` validates and normalises a reading; `_accept` stores it (or publishes to Pub/Sub) and remembers which rooms have live hardware; `_live_loop` adds simulated readings every 30 s, skipping rooms where real hardware is reporting; `lifespan` sets up the store, clears stale data and seeds 28 days of history on first start; `Cache` keeps expensive results for a short time.

### `campuspulse/autopilot.py`: plan the next teaching day
| Function / class | What it does |
| --- | --- |
| `Constraints` | Goals the solver must respect: `no_moves`, `same_block`, `max_moves`, `headroom_pct`, `frozen_modules`, `frozen_rooms`, `frozen_days`, `comfort_first`. |
| `parse_instructions_offline(text)` | Rule-based reader for typical goals, used without Gemini (or when it is slow). |
| `History`, `_predict(slot)` | Median and 90th percentile of each class's measured peak head count. |
| `_optimise(classes, c, profile)` | The mixed-integer program: one room per class, no overlaps (15-minute turnover), capacity with headroom, kind and block rules, CO₂ penalty, move cost. |
| `_twin(classes, rooms, mode, profile)` | 5-minute energy and comfort for a day, with the simulator's physics. |
| `plan(store, day, constraints)` | Runs it all and classifies every change as `auto` or `review`. |
| `backtest(store)` | Forecasts last week's classes from the weeks before and scores the error. |

### `campuspulse/weather.py`
`Weather.outdoor_c(ts)` interpolates hourly Open-Meteo temperatures (31 days past, 3 days ahead) with a disk cache and a typical KL curve offline (`CP_WEATHER=off` forces it). `day_profile(date)` gives the 24 hourly values the planner uses.

### `campuspulse/control.py`: closing the loop
| Function / class | What it does |
| --- | --- |
| `ControlPlane.apply(action, status)` | On approve or dismiss of an AC action, recomputes each room's mode (strongest wins: `schedule` < `after_hours_off` < `occupancy`) and sends a command to every room that changed. |
| `ControlPlane.sync(actions, status)` | Rebuilds modes from the decision log after a restart, without re-sending. |
| `ControlPlane._publish(cmd)` | MQTT QoS 1, retained, on `campuspulse/<room>/command` when `CP_MQTT_HOST` is set. |
| `ControlPlane.state_for(room)` | What `GET /api/rooms/{id}/command` returns to a polling node. |

`Fleet._ac_on(room, time, people)` obeys the same modes, so the next simulated readings carry the saving.

### `campuspulse/study.py`: where can I study?
`free_rooms(states, at)`: rooms with no class booked for at least 30 minutes, sensor reporting and nobody's class running, ranked by already-cooled first, then fresher air, cooler room and longer free time. Closed outside 07:30–21:00 on weekdays.

### `campuspulse/mqtt_bridge.py`
`main()` subscribes to `campuspulse/+/telemetry` at QoS 1 and forwards each message to `/api/ingest`.

### `web/static/app.js`: dashboard
| Function | What it does |
| --- | --- |
| `loadBrief`, `loadOverview` | Fetch the brief, actions, KPIs and rooms; refresh rooms every 30 s. |
| `renderStatus`, `renderLead` | Status line and the headline section. |
| `renderPlan`, `roomCell` | The floor plan; camera-counted rooms show "camera count". |
| `renderActions`, `actionItem`, `impactLine` | The ranked list with Approve, Dismiss and What if. |
| `renderProjection` | What if: totals the selected and approved changes, shows empty-room cooling now vs after, outlines the rooms involved. |
| `drawEvidence`, `loadCaption`, `rowsTable` | Evidence chart, Gemini caption, rows and SQL. |
| `decide`, `restoreDecisions` | Saves a decision; restores earlier decisions on reload. |
| `openRoom`, `drawRoomCharts` | The room drawer with people, CO₂ and energy charts. |
| `askQuestion`, `loadSuggestions` | Ask the data. |
| `loadNetwork` | "Can we trust the data?" panel. |

### `edge/people_counter.py`: camera node
| Function / class | What it does |
| --- | --- |
| `load_model(name)` | Loads YOLO (default `yolo26n.pt`, downloaded on first run). |
| `count_people(model, frame)` | People in one frame (COCO class 0). |
| `draw(frame, boxes, n, room)` | Boxes for the local preview window only. |
| `Reporter.report(people)` | POSTs the count to `/api/ingest`; failed reports are queued and retried; each report says how many attempts it took, which feeds the camera's delivery score. |
| `run_camera(args)` | Webcam or phone stream → median of recent frames → report every 5 s. |
| `run_image(args)` | Count people in one photo (for testing). |

Privacy: frames are never saved or sent; only `{room, people}` leaves the device.

### `firmware/campuspulse_node/`: ESP32
`campuspulse_node.ino` reads the SCD40 (CO₂, temperature, humidity), a PIR on GPIO27 and an optional current clamp, and publishes over MQTT QoS 1, counting acknowledgements (or HTTPS with `TRANSPORT_MQTT 0`). Wi-Fi and keys go in `secrets.h` (copy `secrets.h.example`). Not yet compile-tested on hardware.

### `scripts/`
| Script | What it does |
| --- | --- |
| `import_apu_timetable.py` | Downloads APU's public timetable feed and keeps room, day, times, module and intakes for one week (`--week YYYY-MM-DD`). Lecturer data is dropped. |
| `check_gemini.py` | Tests the Gemini key and model from `.env` without printing the key; lists available models if something fails. |
| `bq_setup.py` | Creates the BigQuery dataset, loads history (`--seed`) and trains BigQuery ML (`--ml`). |

### `deploy/deploy.sh`
Enables the Google Cloud APIs, creates a service account, builds BigQuery, and deploys to Cloud Run with Gemini on Vertex AI: `PROJECT_ID=your-project ./deploy/deploy.sh`.

### `tests/` (29 tests)
| File | Checks |
| --- | --- |
| `test_engine.py` | The engine finds every planted problem; every action carries evidence; ranking order; network flags; gateway outage; KPIs; all tested questions run; the campus uses the real APU timetable with no lecturer data. |
| `test_api.py` | Ingest authentication and validation; real readings marked real; decisions logged; camera counts override people but keep sensor data; captions; PDF report. |
| `test_gemini_loop.py` | The SQL agent recovers from a rejected query (fake Gemini); the brief drops invented action ids. |
| `test_sqlguard.py` | Only safe single SELECTs pass the guard. |

---

## 6. Running it

```bash
# app
python -m pip install -r requirements.txt
python -m uvicorn campuspulse.api:app --port 8080          # open http://localhost:8080

# Gemini: put GEMINI_API_KEY=... in .env, then check
python scripts/check_gemini.py

# camera node (second terminal)
python -m pip install -r edge/requirements.txt
python edge/people_counter.py --room B-08-05 --show

# tests
python -m pytest -q

# refresh the APU timetable to another week
python scripts/import_apu_timetable.py --week 2026-11-09

# deploy to Google Cloud
PROJECT_ID=your-project ./deploy/deploy.sh
```

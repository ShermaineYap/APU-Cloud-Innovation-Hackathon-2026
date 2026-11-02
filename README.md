# CampusPulse

**CampusPulse senses every room, trusts the network, and tells facilities what to change tomorrow, with the evidence attached.**

APU Cloud Innovation Hackathon 2026 · Track 3: Data-Driven Research & Intelligent Decision Making

| | |
| --- | --- |
| Live demo | _added after deployment_ |
| 3-minute video | _added after recording_ |
| Pitch deck | `deck/CampusPulse.pdf` (added before submission) |

![CampusPulse dashboard](docs/dashboard.png)

## The problem

University rooms are cooled on a fixed building schedule, not by who is in them. Classes get booked into rooms three times too big, some tutorials squeeze more students than seats into a small room, and nobody has the evidence to change the timetable or the AC with confidence. When the data does exist, nobody knows whether the sensor behind it is reliable.

CampusPulse runs on **APU's real teaching week**: 20 real rooms (Auditoriums 1–6, Block B classrooms, Tech Labs) and their actual classes, taken from APU's public timetable feed. In the demo, **empty rooms take about 43% of the week's cooling energy**, and dozens of classes with one or two intakes are booked into 120–200-seat auditoriums.

## What it does

1. **Morning brief.** Gemini writes a short brief from actions the decision engine has already costed: what to change, why now, the first step. It may use only the engine's numbers.
2. **What to change.** Five detectors find after-hours cooling, schedule-driven cooling of empty rooms, oversized room bookings, stale air (CO₂ over 1000 ppm) and unreliable data. Every action carries its kWh and ringgit impact, a confidence score, a chart, the rows and the exact SQL behind it. Approve or dismiss, and the decision is logged to BigQuery.
3. **Ask the data.** Plain-English questions become BigQuery SQL through Gemini function calling. A read-only guard allows one SELECT over curated views; errors go back to Gemini to fix. The answer shows the SQL, the table and a chart.
4. **Network-aware confidence.** Each node's delivery ratio (MQTT QoS 1 acknowledgements), signal strength, freshness and a stuck-sensor check become a 0–100 score. Every recommendation inherits the confidence of the rooms it touches, so a decision resting on a weak link says so.

## Cloud, IoT and network, converged

![Architecture](docs/architecture.svg)

| Layer | What we used | Why |
| --- | --- | --- |
| IoT | ESP32 + SCD40 (CO₂, temp, RH) + PIR + optional current clamp | True NDIR CO₂ and motion for about RM 200 a room |
| Network | MQTT QoS 1 → broker → bridge → HTTPS ingest (or direct HTTPS) | Acknowledged delivery we can measure; lost reports become a signal |
| Ingest | Cloud Run, Pub/Sub push | Serverless, one container, scales to zero outside judging week |
| Warehouse | BigQuery: partitioned `readings`, curated views | One SQL source of truth for the app, the engine and Gemini |
| Models | BigQuery ML `ARIMA_PLUS` (occupancy forecast, energy anomalies) | Forecasting in SQL, no training pipeline to maintain |
| AI | Gemini on Vertex AI: JSON-mode brief, function-calling SQL agent | Explains and explores; never invents figures |
| App | FastAPI + vanilla JS + Chart.js on Cloud Run | Fast to load, nothing to build |

Locally the same BigQuery SQL runs on DuckDB, transpiled by sqlglot, so the whole system works on a laptop with no cloud account. The local forecast is an hour-of-week seasonal baseline, the same weekly pattern ARIMA_PLUS captures.

## What is real and what is simulated

- **Real, from APU:** the 20 rooms and their timetable for the week of 2 November 2026: room codes (`B-08-05` is Block B, level 8, room 05), auditorium names, module codes and names, exact class times, and how many intakes share each session. It comes from APU's public timetable feed (the data behind APSpace's timetable) via `scripts/import_apu_timetable.py`; lecturer names and accounts are dropped. The snapshot is in `campuspulse/data/apu_timetable.json`, so the app runs offline.
- **Real, our sensor:** one ESP32 node in B-08-05 (`device_id esp32-b0805`, change with `CP_LIVE_ROOM`), its readings stored with `simulated = false`. When it reports, it replaces its simulated twin on the floor plan, marked "live".
- **Simulated:** sensor readings for the other rooms, 28 days at 5-minute intervals, labelled `simulated = true` everywhere. The simulator follows the real timetable and uses a CO₂ mass balance, a thermal model and AC load. On top of what the real timetable produces (small classes in auditoriums, crowded shared sessions, cooling between classes), it plants Friday-afternoon no-shows, weekend AC left on in Auditorium 2, a weak link in Tech Lab 7-01, a stuck CO₂ sensor in Tech Lab 4-02 and a four-hour Tech Lab gateway outage. The tests check that the engine finds every one of them without being told.
- **Assumptions:** APU does not publish room capacities, AC loads or enrolment, so seats (auditoriums 120–200, classrooms 50, Tech Labs 40) and AC loads are estimates, and class size is estimated as 12 students per intake sharing a session, capped at the room's seats. Energy price `CP_TARIFF_RM_PER_KWH` defaults to RM 0.50/kWh; set it from the current TNB commercial tariff before presenting. All of these live in `campuspulse/campus.py` and `.env`, ready to replace with Estates data.

## How robust is the auditorium finding?

Class size is the one estimate the room moves depend on, so we stress-tested it against APU's real timetable for the week (91 auditorium classes):

| Assumed students per intake | Auditorium classes under 35% full |
| --- | --- |
| 12 (our default) | 46 |
| 20 | 36 |
| 30 | 23 |
| 40 | 14 |

The finding does not depend on the estimate: **34 of the 91 auditorium classes are for one or two intakes in APU's own timetable, and 14 are for a single intake.** When Facilities shares real seat counts or AC loads, put them in `campuspulse/data/room_overrides.csv` (copy the `.example.csv`) and restart; nothing else changes.

## Run it locally (2 minutes)

```bash
python -m pip install -r requirements.txt
python -m uvicorn campuspulse.api:app --port 8080
# open http://localhost:8080
```

The first start seeds 28 days of history (about 5 seconds). Without a Gemini key the brief is templated and Ask works on the 15 tested questions. To switch Gemini on, copy `.env.example` to `.env` and set `GEMINI_API_KEY` (AI Studio), or `CP_GEMINI_VERTEX=true` and `GOOGLE_CLOUD_PROJECT` for Vertex AI. The app reads `.env` on start; it is git-ignored. Run `python scripts/check_gemini.py` to test the key and model; it prints Gemini's own error if something is wrong.

Send a reading as the physical node would:

```bash
curl -X POST localhost:8080/api/ingest -H 'X-Device-Key: dev-ingest-key' -H 'Content-Type: application/json' \
  -d '{"device_id":"esp32-b0805","room_id":"B-08-05","occupied":true,"co2_ppm":812,"temp_c":24.6,"rssi_dbm":-61,"watts":2100}'
```

## Deploy to Google Cloud

```bash
PROJECT_ID=your-project ./deploy/deploy.sh
```

This enables the APIs, creates a service account, builds the BigQuery dataset, views and history, and deploys Cloud Run with Gemini on Vertex AI. Train the BigQuery ML models afterwards with `python scripts/bq_setup.py --ml`. Refresh the timetable to the current week with `python scripts/import_apu_timetable.py --week YYYY-MM-DD` (a Monday). Firmware setup is in [firmware/README.md](firmware/README.md).

## Repository

```
campuspulse/        service: simulator, store (BigQuery | DuckDB), analytics, network, decisions
campuspulse/agent/  Gemini brief, SQL agent, read-only SQL guard, tested questions
campuspulse/data/   APU timetable snapshot (public feed, no lecturer data)
bigquery/           schema, curated views, BigQuery ML
web/static/         dashboard
firmware/           ESP32 node (MQTT QoS 1 or HTTPS)
deploy/, scripts/   Cloud Run deploy, BigQuery setup
tests/              27 tests: guard, engine finds planted problems, API, Gemini loop with a fake model
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/ingest` | Node readings (header `X-Device-Key`) |
| POST | `/api/pubsub/push` | Pub/Sub push target |
| GET | `/api/overview` | KPIs, room states, mode |
| GET | `/api/brief` | Gemini brief + ranked actions (`?refresh=true`) |
| POST | `/api/actions/{id}/decide` | Approve or dismiss; logged to `decisions` |
| GET | `/api/rooms/{id}` | 48 h history, 24 h forecast, anomalies |
| GET | `/api/network` | Node confidence and gateway outages |
| POST | `/api/ask` | Ask the data |

## Tests

```bash
python -m pytest -q
```

## Team

Shermaine Yap Shi Min, Asia Pacific University of Technology & Innovation.

MIT licensed.

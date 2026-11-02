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

In the demo data, **empty rooms take about 61% of the week's cooling energy**, and two of 21 sensor nodes are not trustworthy.

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

- **Real:** one ESP32 node in room B-03 (`device_id esp32-b03`), its readings stored with `simulated = false`. When it reports, it replaces its simulated twin on the floor plan, marked "live".
- **Simulated:** 20 rooms across three blocks, 28 days of 5-minute readings, labelled `simulated = true` everywhere. The simulator uses a CO₂ mass balance, a thermal model and AC load, and plants realistic problems: weekend AC left on, an oversized booking, an oversubscribed tutorial, Friday-afternoon no-shows, a weak link, a stuck CO₂ sensor and a four-hour gateway outage. The tests check that the engine finds every one of them without being told.
- **Assumptions:** energy price `CP_TARIFF_RM_PER_KWH` (default RM 0.50/kWh, set from the current TNB commercial tariff before presenting); room capacities and AC loads are illustrative, not APU's estate data.

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
  -d '{"device_id":"esp32-b03","room_id":"B-03","occupied":true,"co2_ppm":812,"temp_c":24.6,"rssi_dbm":-61,"watts":2100}'
```

## Deploy to Google Cloud

```bash
PROJECT_ID=your-project ./deploy/deploy.sh
```

This enables the APIs, creates a service account, builds the BigQuery dataset, views and history, and deploys Cloud Run with Gemini on Vertex AI. Train the BigQuery ML models afterwards with `python scripts/bq_setup.py --ml`. Firmware setup is in [firmware/README.md](firmware/README.md).

## Repository

```
campuspulse/        service: simulator, store (BigQuery | DuckDB), analytics, network, decisions
campuspulse/agent/  Gemini brief, SQL agent, read-only SQL guard, tested questions
bigquery/           schema, curated views, BigQuery ML
web/static/         dashboard
firmware/           ESP32 node (MQTT QoS 1 or HTTPS)
deploy/, scripts/   Cloud Run deploy, BigQuery setup
tests/              26 tests: guard, engine finds planted problems, API, Gemini loop with a fake model
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

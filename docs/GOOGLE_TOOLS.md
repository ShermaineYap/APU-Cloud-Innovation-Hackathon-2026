# Google tools in CampusPulse

What CampusPulse uses from Google, what is ready to switch on, and how. Everything here
is optional: without it the app behaves exactly as before. The **Data** tab's *Google
Cloud and integrations* panel shows the live state of each one.

| Service | State | What it does |
| --- | --- | --- |
| Cloud Run | Live | Hosts the app, the ingest API and the A2A agent |
| BigQuery + BigQuery ML | Live | The warehouse; ARIMA_PLUS forecasts and anomalies in the room drawer |
| Pub/Sub | Live | Sensor readings are published, then pushed to the service |
| Gemini | Live | Six roles: goal reader, SQL agent, brief, plan explainer, notices, chart captions |
| Gemini Enterprise | Ready | CampusPulse is an A2A agent; an admin registers it (below) |
| Vertex AI | Ready | Switch Gemini from an AI Studio key to Vertex AI (below) |
| Google Sheets | Ready | `=IMPORTDATA` pulls tomorrow's room changes into any sheet |
| Google Calendar | Ready | Subscribe to tomorrow's room changes as events |
| Looker Studio | Ready | A no-code dashboard on the BigQuery views |

## Gemini Enterprise: register CampusPulse as an A2A agent

Gemini Enterprise lets an admin add custom agents that speak the open Agent2Agent (A2A)
protocol. CampusPulse already is one; nothing needs rebuilding.

- **Agent card:** `https://<service>/.well-known/agent-card.json`
  (live: <https://campuspulse-443933554054.asia-southeast1.run.app/.well-known/agent-card.json>)
- **Endpoint:** `POST https://<service>/a2a`, JSON-RPC 2.0, method `message/send`
- **Skills (all read-only):** `ask_campus_data`, `plan_tomorrow` (proposes, never applies), `campus_status`

Steps, once you have Gemini Enterprise access:

1. In the Google Cloud console, open **Gemini Enterprise** and the app (or create one) in the
   same project as the Cloud Run service.
2. Go to **Agents → Add agent → Custom agent via A2A** and paste the agent card URL.
   Your account needs the Discovery Engine Admin role.
3. If you set `CP_A2A_KEY`, choose bearer-token auth and paste the same key.
4. Set `CP_GEMINI_ENTERPRISE_APP=<app name>` on the Cloud Run service so the panel shows
   *Registered*, then redeploy.
5. In Gemini Enterprise, ask: *"How many hours was Auditorium 2 cooled while empty on Saturday?"*
   or *"Plan tomorrow, comfort first, at most 5 moves"*.

Official guides: [Register and manage A2A and ADK agents](https://discuss.google.dev/t/new-official-docs-register-and-manage-adk-and-a2a-agents-in-gemini-enterprise/290762)
· [ADK agents on Agent Engine](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent).
Menu names can differ slightly; follow the console.

Test the agent without Gemini Enterprise:

```bash
curl -s https://<service>/.well-known/agent-card.json
```

```bash
curl -s -X POST https://<service>/a2a -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","messageId":"1","parts":[{"kind":"text","text":"Give me the morning brief"}]}}}'
```

Settings: `CP_A2A_KEY` (optional bearer key), `CP_PUBLIC_URL` (the https address, if the card
shows the wrong host), `CP_GEMINI_ENTERPRISE_APP` (shown once registered).

## Vertex AI instead of an AI Studio key

On Cloud Run set `CP_GEMINI_VERTEX=true` and `GOOGLE_CLOUD_PROJECT=<project>`, remove
`GEMINI_API_KEY`, and give the service account the **Vertex AI User** role. The deploy script
does this when no AI Studio key is set. Gemini then runs inside the Google Cloud project,
with no key to leak.

## Google Sheets

In any Google Sheet, cell A1:

```
=IMPORTDATA("https://campuspulse-443933554054.asia-southeast1.run.app/api/autopilot/latest.csv")
```

Columns: day, time, module, from/to room, expected people, kWh saved, automatic or needs review,
and why. The AC schedule per room is at `/api/autopilot/schedule.csv`. Sheets refreshes
`IMPORTDATA` about every hour.

## Google Calendar

Use **Add to Google Calendar** under the Autopilot plan, or in Google Calendar choose
**Other calendars → From URL** and paste
`https://campuspulse-443933554054.asia-southeast1.run.app/api/autopilot/latest.ics`.
Each room change becomes an event at its class time, in its new room.

## Looker Studio (about 15 minutes, no code)

1. Open <https://lookerstudio.google.com> → **Create → Report → BigQuery**.
2. Pick the project, dataset `campuspulse`, table `v_room_hourly`.
3. Add a time-series chart: dimension `hour`, metric `kwh`, breakdown `block`.
4. Add a second data source, `v_empty_cooled`, and a bar chart: dimension `room_name`, metric
   `SUM(kwh)`. That's the cooling spent on empty rooms per room.
5. **Share → Anyone with the link can view**, and paste the link in the README.

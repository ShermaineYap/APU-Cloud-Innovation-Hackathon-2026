# Virtual sensor node on Wokwi (no hardware)

A simulated ESP32 in your browser that talks to CampusPulse over real MQTT: readings go up,
AC commands come back down. Same messages as the real node in `firmware/campuspulse_node`.

```
Wokwi ESP32 ──MQTT──► broker.hivemq.com ──► mqtt_bridge ──► CampusPulse /api/ingest
     ▲                                                              │
     └──────── <prefix>/B-08-05/command (QoS 1, retained) ◄─────────┘  (Approve / Autopilot)
```

| Part | Pin | In the demo |
| --- | --- | --- |
| DHT22 | GPIO15 | click it to change temperature and humidity |
| PIR motion sensor | GPIO27 | click **Simulate motion**: the room becomes occupied |
| Potentiometer | GPIO34 | CO₂ 400 → 2000 ppm (Wokwi has no SCD40, so the knob stands in) |
| Blue LED | GPIO26 | the AC relay: lit = AC allowed to run |
| Green LED | GPIO2 | MQTT connected |

## Open the ready-made project

**<https://wokwi.com/projects/480507881128421377>**: everything below is already done there (sketch, wiring,
libraries). Open it and press ▶. To change it, sign in and use *Save a copy*.

## Or build it yourself (5 minutes)

1. Go to <https://wokwi.com/projects/new/esp32> (sign in to save the project).
2. Replace `sketch.ino` with [`sketch.ino`](sketch.ino).
3. Open the `diagram.json` tab and replace it with [`diagram.json`](diagram.json).
4. Library Manager (the books icon) → add **PubSubClient**, **ArduinoJson**, **DHT sensor library for ESPx**
   (or create a `libraries.txt` tab with [`libraries.txt`](libraries.txt)).
5. If several teams use the public broker, change `PREFIX` in the sketch to something only you use,
   and use the same value below.

## It is a virtual node, and the dashboard says so

The sketch reports as `wokwi-b0805`. Its readings come from Wokwi's sliders and knob, not from a
measurement, so the dashboard tags B-08-05 **"virtual node (Wokwi)"** and the status bar says
"hand-set values". Only the physical board (`esp32-b0805`) can ever show as **live**.

**The saved Wokwi project still has the old id.** Open it, find `DEVICE_ID` near the top of `sketch.ino`,
change `"esp32-b0805"` to `"wokwi-b0805"`, and press *Save*. Otherwise it would pose as the real board.

## Run it: feed the live cloud site (or your laptop)

The bridge runs on your laptop and works with **any** CampusPulse server, the Cloud Run site included.
It carries readings up (MQTT → HTTPS `/api/ingest`) and AC commands back down (it checks
`/api/rooms/<room>/command` every 5 s and publishes changes to MQTT). So you don't need MQTT on the server.

```
Wokwi ESP32 ──MQTT──► broker.hivemq.com ──► bridge (your laptop) ──HTTPS──► CampusPulse (Cloud Run or laptop)
     ▲                                          │   ◄── AC mode, every 5 s ──┘
     └──── <prefix>/B-08-05/command (retained) ─┘
```

**To the live cloud site** (needs the cloud ingest key, `INGEST_KEY` in Shermaine's `.deploy.env`):

```bash
.venv/Scripts/python -m campuspulse.mqtt_bridge --broker broker.hivemq.com --prefix apu-campuspulse-2026 --api https://campuspulse-443933554054.asia-southeast1.run.app --key <cloud ingest key>
```

**To your laptop server** (the key is `CP_INGEST_KEY` in `.env`; use `127.0.0.1`, not `localhost`,
because on Windows `localhost` tries IPv6 first and the server only listens on IPv4):

```bash
.venv/Scripts/python -m uvicorn campuspulse.api:app --port 8000
```

```bash
.venv/Scripts/python -m campuspulse.mqtt_bridge --broker broker.hivemq.com --prefix apu-campuspulse-2026 --api http://127.0.0.1:8000 --key <CP_INGEST_KEY from .env>
```

Then open Wokwi and press the green ▶. Keep the Wokwi tab **visible in front**: browsers slow down
background tabs, and the speed meter (top right) should read close to 100%. The serial monitor shows
`Wi-Fi ok` and `MQTT ... connected`, and the bridge terminal prints `-> 200` for each reading. Within
about 30 s, B-08-05 shows **virtual node (Wokwi)** with your slider values.

| Bridge prints | Meaning |
|---|---|
| `-> 200 {"accepted":1...}` | Working |
| `-> 401` | Wrong `--key` for that server |
| `refused` / `10061` | Wrong `--api`, or the laptop server isn't running (use `127.0.0.1`) |
| `command for B-08-05 -> AC mode occupancy` | An approval went down to the node |

**Don't run Wokwi and the real board at the same time** in the same room: the dashboard shows the
latest message, so the readings would jump between the two.

## What to show

1. **Sense:** click *Simulate motion* on the PIR and turn the CO₂ knob past the middle: B-08-05 turns
   orange (CO₂ over limit) on the floor plan.
2. **Decide and act:** in CampusPulse, approve *Tie Block B air-conditioning to occupancy*, or run
   **Autopilot** and apply the plan. The serial monitor prints `Command from CampusPulse: AC mode -> occupancy`.
3. **Obey:** stop the motion. A minute later the blue AC LED goes off, because the room is empty.
   Click motion again and it comes back on.

No Wokwi? `.venv/Scripts/python edge/virtual_node.py --people 30` sends the same messages from a terminal (as `virtual-b0805`, also labelled virtual) and prints the commands it receives. Run the bridge the same way.

Note: a public broker is fine for a demo. For real use, run your own broker (Mosquitto, HiveMQ Cloud)
with a username, password and TLS (`CP_MQTT_USER`, `CP_MQTT_PASS`, `CP_MQTT_TLS=true`).

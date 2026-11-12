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

## Set up (5 minutes)

1. Go to <https://wokwi.com/projects/new/esp32> (sign in to save the project).
2. Replace `sketch.ino` with [`sketch.ino`](sketch.ino).
3. Open the `diagram.json` tab and replace it with [`diagram.json`](diagram.json).
4. Library Manager (the books icon) → add **PubSubClient**, **ArduinoJson**, **DHT sensor library for ESPx**
   (or create a `libraries.txt` tab with [`libraries.txt`](libraries.txt)).
5. If several teams use the public broker, change `PREFIX` in the sketch to something only you use,
   and use the same value below.

## Run it

Terminal 1, the app (reads `CP_MQTT_HOST` and `CP_MQTT_PREFIX` from `.env`):

```bash
.venv/Scripts/python -m uvicorn campuspulse.api:app --port 8000
```

Terminal 2, the bridge:

```bash
.venv/Scripts/python -m campuspulse.mqtt_bridge --broker broker.hivemq.com --prefix apu-campuspulse-2026 --api http://localhost:8000
```

Keep the Wokwi tab **visible in front** while it runs: browsers throttle background tabs, and the
speed meter (top right of the simulation, next to the clock) should read close to 100%. At a few percent the
simulated board is too slow for MQTT and keeps reconnecting.

Then press the green ▶ in Wokwi. The serial monitor shows `Wi-Fi ok` and `MQTT ... connected`, and
B-08-05 on the dashboard turns **live** within about 30 seconds.

## What to show

1. **Sense:** click *Simulate motion* on the PIR and turn the CO₂ knob past the middle: B-08-05 turns
   orange (CO₂ over limit) on the floor plan.
2. **Decide and act:** in CampusPulse, approve *Tie Block B air-conditioning to occupancy*, or run
   **Autopilot** and apply the plan. The serial monitor prints `Command from CampusPulse: AC mode -> occupancy`.
3. **Obey:** stop the motion. A minute later the blue AC LED goes off, because the room is empty.
   Click motion again and it comes back on.

No Wokwi? `python edge/virtual_node.py` sends the same messages from a terminal and prints the commands it receives.

Note: a public broker is fine for a demo. For real use, run your own broker (Mosquitto, HiveMQ Cloud)
with a username, password and TLS (`CP_MQTT_USER`, `CP_MQTT_PASS`, `CP_MQTT_TLS=true`).

# Real hardware on stage: three ways

This guide has three paths. Pick the one that matches what you have on the day.

| What you have | Path | What is real |
|---|---|---|
| A bare ESP32 board | **A** | Wi-Fi signal, message delivery, occupancy (from the BOOT button), the AC "relay" (the blue LED) |
| An ESP32 plus sensors | **B** | Everything in A, plus temperature and humidity (DHT22), motion (PIR) and CO2 (SCD40/41) |
| No board, or it fails | **C: replay** | Real measured CO2, temperature, humidity and occupancy from a published dataset (UCI, Belgium 2015) |

Every path uses the same sketch, [`campuspulse_board.ino`](campuspulse_board.ino). Only the three `HAS_...` switches at the top change.

---

## 0. One-time setup on the laptop (about 10 minutes, do it before the event)

1. Install **Arduino IDE 2**.
2. Add the ESP32 boards:
   - Go to *File → Preferences → Additional boards manager URLs* and paste
     `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
   - Go to *Tools → Board → Boards Manager*, search for **esp32** (by Espressif) and install it.
3. Install the libraries from *Tools → Manage Libraries*:
   - **PubSubClient** (Nick O'Leary)
   - **ArduinoJson** (Benoit Blanchon, v7)
   - Path B only, if you have the sensors: **DHT sensor library for ESPx** (beegee_tokyo) and **Sensirion I2C SCD4x**
4. Plug the board in. If no COM port appears, install the USB driver. The chip is printed near the USB socket: **CP210x** (Silicon Labs) or **CH340** (WCH).
5. Under *Tools*, set the board to **ESP32 Dev Module** and pick the new COM port.

## 1. Edit the settings block (top of the sketch)

```cpp
#define HAS_DHT22 0   // Path A: leave all three at 0
#define HAS_PIR   0
#define HAS_SCD4X 0

const char *WIFI_SSID = "YOUR_HOTSPOT_NAME";
const char *WIFI_PASS = "YOUR_HOTSPOT_PASSWORD";
```

- **Use a phone hotspot set to 2.4 GHz.** The ESP32 can't join 5 GHz networks or enterprise networks that ask for a username and password, so APU Wi-Fi will most likely fail. On an iPhone, turn on *Maximise Compatibility*.
- Connect the laptop to the same hotspot, or to any network with internet. The board and the laptop only need to reach the internet, not each other.
- Leave `PREFIX`, `ROOM_ID` (`B-08-05`) and `DEVICE_ID` as they are.

## 2. Wiring

### Path A: nothing to wire
- The **BOOT** button (GPIO0) toggles the room between occupied and empty.
- The onboard **blue LED** (GPIO2) stands in for the AC relay. When it's on, the AC is allowed to run.

### Path B: wire whatever you have, then set its switch to 1

| Part | Pin on the part | ESP32 pin | Switch |
|---|---|---|---|
| DHT22 / AM2302 | + / VCC | 3V3 | `HAS_DHT22 1` |
| | DATA / OUT | **GPIO15** | |
| | − / GND | GND | |
| PIR HC-SR501 | VCC | **VIN / 5V** (it needs 5 V) | `HAS_PIR 1` |
| | OUT | **GPIO27** | |
| | GND | GND | |
| SCD40 / SCD41 (CO2) | VDD | 3V3 | `HAS_SCD4X 1` |
| | SDA | **GPIO21** | |
| | SCL | **GPIO22** | |
| | GND | GND | |

Notes:
- A bare 4-pin DHT22 needs a 10 kΩ resistor between DATA and 3V3. Modules on a small board already have it.
- With a PIR fitted, occupancy comes from motion and the BOOT button does nothing.
- Each part works on its own. Anything not fitted shows as "–" on the dashboard.
- Only the SCD40/41 measures real CO2. An MQ-135 doesn't, so don't present it as CO2.

## 3. Upload and watch

1. Click **Upload**. If it hangs at `Connecting....`, hold **BOOT** until the percentage starts.
2. Open *Tools → Serial Monitor* at **115200** baud. You should see:
   ```
   CampusPulse node esp32-b0805 in B-08-05 | DHT22:0 PIR:0 SCD4x:0
   Wi-Fi..... ok, IP 172.20.10.4, RSSI -48 dBm
   MQTT ... connected
   sent | AC ON (schedule) -> {"device_id":"esp32-b0805","room_id":"B-08-05","occupied":false,...}
   ```
3. Press BOOT. The monitor prints `Room marked OCCUPIED (BOOT button)`.

## 4. Get the data into CampusPulse

The board publishes MQTT to the public HiveMQ broker. The bridge on the laptop forwards it to the app.

Open terminal 1 and start the app (skip this if it's already running with the latest code):

```bash
.venv/Scripts/python -m uvicorn campuspulse.api:app --port 8000
```

Open terminal 2 and start the bridge:

```bash
.venv/Scripts/python -m campuspulse.mqtt_bridge --broker broker.hivemq.com --prefix apu-campuspulse-2026 --api http://localhost:8000
```

Then check it on the dashboard at http://localhost:8000:
- Room **B-08-05** shows the green **live** tag. The status bar says a live node is online.
- Path A shows "–" for CO2 and temperature, which is expected. RSSI, occupancy and AC are real.
- Press BOOT to see the room flip between empty and in use within about 5 seconds.
- **Closing the loop:** approve an AC action for B-08-05, or an Autopilot plan. The Serial Monitor prints `Command from CampusPulse: AC mode -> occupancy`. The blue LED then follows occupancy: press BOOT to empty the room and it goes off.

> To send to the cloud deployment instead, point the bridge at it:
> `--api https://campuspulse-443933554054.asia-southeast1.run.app --key <CP_INGEST_KEY>`.

## 5. Path C: the fallback, replaying a real published dataset

Use this if no board turns up, the hotspot fails, or the board dies on stage. It needs no hardware and takes about one minute.

```bash
.venv/Scripts/python scripts/replay_dataset.py
```

- It streams **genuinely measured** CO2, temperature, humidity and occupancy into B-08-05, one 5-minute row every 3 seconds.
- The data comes from the UCI Occupancy Detection dataset, an office in Mons, Belgium, February 2015, licensed CC BY 4.0. Details are in [`campuspulse/data/replay/README.md`](../../campuspulse/data/replay/README.md).
- The room is tagged **"real data, replayed"** (blue), not "live", and the status bar says so.
- The default start (Monday 2015-02-09, 07:30) is chosen for the demo. People arrive at 08:40 dataset time (about 40 s in), and CO2 passes 1,000 ppm at 09:55 (about 90 s in). At that point CampusPulse flags **stale air** for real.
- To reach stale air sooner: `--start "2015-02-09 09:30"`. To use another room: `--room AUD-1`. To change the speed: `--every 2`.
- To send to the cloud deployment: add `--server https://campuspulse-443933554054.asia-southeast1.run.app --key <CP_INGEST_KEY>`.
- Press Ctrl+C to stop. The room goes back to simulation after 5 minutes.

**Say it straight on stage:** "This is real sensor data from a published office study in Belgium, replayed through our pipeline. It isn't APU data, and the other rooms are simulated." Judges reward that honesty. Never call it live.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| No COM port | Install the CP210x or CH340 driver, or try another USB cable (some carry power only) |
| `Connecting....` then a timeout | Hold BOOT during upload |
| `Wi-Fi ... FAILED` | Make sure the hotspot is 2.4 GHz and the SSID and password are exact (case-sensitive) |
| `MQTT failed (-2)` | The network blocks port 1883. Use the phone hotspot |
| Serial shows `sent` but the dashboard isn't live | Check the bridge terminal. The prefix must be `apu-campuspulse-2026` on both sides |
| The bridge says 401 | Pass the ingest key the server expects |
| Temperature shows "–" with a DHT22 fitted | Check DATA is on GPIO15 and the pull-up resistor; check `HAS_DHT22 1` |
| CO2 stays "–" | The SCD4x needs about 5 s for its first reading. Check SDA 21 / SCL 22 and `HAS_SCD4X 1` |
| Anything else in the last 10 minutes | Switch to Path C. It works with no hardware |

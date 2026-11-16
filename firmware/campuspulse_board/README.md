# Real data on stage: two modes

The dashboard's **Today** tab has a card called **"Real data in B-08-05"** with a three-way switch:

| Switch | What feeds B-08-05 | What you need |
|---|---|---|
| **ESP32 + DHT22 sensor** | Our own board: real temperature, humidity, occupancy (BOOT button or PIR), Wi-Fi signal, and the AC relay (LED) obeying CampusPulse | The board flashed with this sketch, plus a phone hotspot |
| **Real dataset replay** (fallback) | Real measured CO2, temperature, humidity and occupancy from a published study (UCI, Belgium 2015), replayed by the server itself | Nothing: one click |
| **Simulated** | The simulator, like the other 19 rooms | Nothing |

Both real modes work **on the Cloud Run URL and on a laptop**, with no bridge and no extra terminal.
- The board posts straight to the server over HTTPS.
- The replay runs inside the server.

The room is tagged **"live"** (ESP32) or **"real data, replayed"** (dataset), never mixed up.

---

## Mode 1: ESP32 + DHT22

### One-time setup (Arduino IDE 2)
1. Add the ESP32 boards:
   - Go to *File → Preferences → Additional boards manager URLs* and paste `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
   - Open the Boards Manager (second sidebar icon), search **esp32** (by Espressif Systems) and click **Install**.
2. Open the Library Manager (books icon) and install these, choosing *Install All* when asked:
   - **ArduinoJson** (Benoit Blanchon)
   - **DHT sensor library for ESPx** (beegee_tokyo)
   - **PubSubClient** (Nick O'Leary): only needed for MQTT mode, but harmless to have
   - **Sensirion I2C SCD4x**: only if you have an SCD40/41 CO2 sensor
3. Open `firmware/campuspulse_board/campuspulse_board.ino` (*File → Open*).
4. Plug in the board. Under *Select Board*, pick its COM port and **ESP32 Dev Module**. If no port appears, install the CP210x or CH340 USB driver, or try a data-capable cable.

### Wiring the DHT22

| DHT22 pin | ESP32 pin |
|---|---|
| + / VCC | 3V3 |
| DATA / OUT | **GPIO15** |
| − / GND | GND |

- A bare 4-pin DHT22 needs a 10 kΩ resistor between DATA and 3V3. A module on a small board already has one.
- No DHT22 on the day? Leave `HAS_DHT22 1` anyway. Temperature and humidity just show "–", and Wi-Fi, occupancy and the AC relay are still real.
- Optional extras:
  - PIR: OUT → GPIO27, VCC → VIN/5V, set `HAS_PIR 1`. Motion then sets occupancy instead of the BOOT button.
  - SCD40/41: SDA → 21, SCL → 22, 3V3, set `HAS_SCD4X 1`. This gives real CO2.

### Settings block (top of the sketch)

```cpp
#define HAS_DHT22 1
#define USE_MQTT  0                 // 0 = HTTPS straight to the server (no bridge)

const char *WIFI_SSID = "YOUR_HOTSPOT_NAME";      // phone hotspot, 2.4 GHz
const char *WIFI_PASS = "YOUR_HOTSPOT_PASSWORD";

const char *SERVER_URL = "https://campuspulse-443933554054.asia-southeast1.run.app";
const char *INGEST_KEY = "PASTE_THE_INGEST_KEY_HERE";
```

- **INGEST_KEY** is the cloud service's ingest key. Shermaine has it in `.deploy.env` (`INGEST_KEY=…`), next to the deploy script. The key in your own `.env` is only for your laptop server, and the cloud rejects it with 401.
- **Wi-Fi:** the ESP32 can't join 5 GHz networks or APU's username-and-password Wi-Fi. Use a phone hotspot (on an iPhone, turn on *Maximise Compatibility*).
- **Laptop server instead of the cloud:**
  - Set `SERVER_URL "http://<laptop-IP>:8000"` and use the `CP_INGEST_KEY` from `.env`.
  - Start the server with `--host 0.0.0.0` and put the laptop on the same hotspot.
  - The laptop's IP is the IPv4 address that `ipconfig` shows.

### Upload and check
1. Click **Upload**. If it hangs at `Connecting....`, hold **BOOT** until the percentage starts.
2. Open *Serial Monitor* at **115200** baud. You should see:
   ```
   CampusPulse node esp32-b0805 in B-08-05 | DHT22:1 PIR:0 SCD4x:0 | https://campuspulse-…run.app
   Wi-Fi..... ok, IP 172.20.10.4, RSSI -48 dBm
   sent | AC ON (schedule) -> {"device_id":"esp32-b0805",...,"temp_c":27.3,"humidity_pct":63}
   ```
   - The first send can take about 10 s while Cloud Run wakes up.
   - `Server answered 401 (wrong INGEST_KEY)` means the key doesn't match the server's.
3. On the dashboard, click **ESP32 + DHT22 sensor**. The card turns green with "Live: ESP32 node esp32-b0805", shows the real temperature, humidity and Wi-Fi, and B-08-05 shows **live**.
4. Press **BOOT** and the room flips between occupied and empty within about 5 s.
5. To close the loop, approve an AC change for B-08-05 (Actions) or an Autopilot plan.
   - Within 10 s, Serial prints `Command from CampusPulse: AC mode -> occupancy`.
   - The blue LED then follows occupancy: press BOOT to empty the room and the LED goes off.

---

## Mode 2: real dataset replay (the fallback)

On the dashboard, click **Real dataset replay**. That's it, and it works on the cloud URL too.

- The server streams genuinely measured readings into B-08-05, one 5-minute row every 3 s by default.
- The data is the UCI Occupancy Detection dataset: an office in Mons, Belgium, February 2015, licensed CC BY 4.0. Details are in [`campuspulse/data/replay/README.md`](../../campuspulse/data/replay/README.md).
- Pick a starting point:
  - **Monday morning:** the room fills at 08:40, and CO2 passes 1,000 ppm about 1.5 minutes later on screen. CampusPulse then flags **stale air** on real data.
  - **Monday mid-morning:** CO2 crosses 1,000 ppm within a minute.
  - **Wednesday afternoon:** people arrive and CO2 jumps straight over 1,000 ppm.
- Use **Restart** to apply a different start or speed. Click **Simulated** or **ESP32** to stop.
- The replay keeps flowing while the dashboard is open, because it advances on each page refresh. Rows fall due by the clock, so an idle Cloud Run instance doesn't drift.

**Say it straight on stage:** "This is real sensor data from a published office study in Belgium, replayed through our pipeline. It isn't APU data, and the other rooms are simulated." Never call it live.

The laptop script still exists for scripted runs: `.venv/Scripts/python scripts/replay_dataset.py --server <url> --key <key>`.

---

## Optional: MQTT instead of HTTPS

Set `USE_MQTT 1`. The board then publishes to the public HiveMQ broker, and you run the bridge on the laptop:

```bash
.venv/Scripts/python -m campuspulse.mqtt_bridge --broker broker.hivemq.com --prefix apu-campuspulse-2026 --api <server URL> --key <ingest key>
```

HTTPS mode is simpler and has one less thing to fail, so use MQTT only if the hotspot blocks HTTPS (it won't).

## Troubleshooting

| Symptom | Fix |
|---|---|
| No COM port | Install the CP210x or CH340 driver, or try another USB cable (some carry power only) |
| `Connecting....` then a timeout | Hold BOOT during upload |
| `Wi-Fi ... FAILED` | Make sure the hotspot is 2.4 GHz and the SSID and password are exact (case-sensitive) |
| `Server answered 401` | Wrong `INGEST_KEY`: use the cloud key from `.deploy.env`, or `.env` for a laptop server |
| `NOT SENT` with no code | Wrong `SERVER_URL`, no internet on the hotspot, or (laptop mode) a firewall blocking port 8000 |
| Card still says "Waiting for the ESP32" | Check that Serial shows `sent`, and that `ROOM_ID` is `B-08-05` |
| Temperature shows "–" | Check DATA is on GPIO15 and the pull-up resistor |
| Anything else in the last 10 minutes | Click **Real dataset replay** |

/*
  CampusPulse room node for a real ESP32: one sketch for whatever hardware you have.

  Set the switches below, then upload (guide: firmware/campuspulse_board/README.md).

  Scenario A, bare ESP32 board (all switches 0). Still REAL data:
    - Wi-Fi signal strength and message delivery (they feed CampusPulse's trust score)
    - occupancy: press the board's BOOT button to mark the room occupied / empty
    - AC relay: the board's own blue LED (GPIO2) obeys CampusPulse's AC commands
  Scenario B, ESP32 + sensors (switch on what you have):
    - HAS_DHT22  temperature and humidity       DATA -> GPIO15
    - HAS_PIR    motion = occupancy (HC-SR501)  OUT  -> GPIO27
    - HAS_SCD4X  real CO2 (Sensirion SCD40/41)  SDA -> GPIO21, SCL -> GPIO22
  Anything not fitted is left out of the message and shows as "–" on the dashboard.

  Up:   JSON every 5 s to <PREFIX>/<ROOM_ID>/telemetry (MQTT, public broker); the CampusPulse
        bridge forwards it to the app and the room shows "live".
  Down: <PREFIX>/<ROOM_ID>/command (retained). Approve an AC change or an Autopilot plan and
        the LED follows the new AC mode.
*/

// ======================= 1. YOUR SETTINGS =======================
#define HAS_DHT22 0      // 1 if a DHT22 (AM2302) is wired to GPIO15
#define HAS_PIR   0      // 1 if a PIR motion sensor is wired to GPIO27
#define HAS_SCD4X 0      // 1 if a Sensirion SCD40/SCD41 is wired to I2C (21/22)

const char *WIFI_SSID = "YOUR_HOTSPOT_NAME";   // a phone hotspot is the safest choice (2.4 GHz)
const char *WIFI_PASS = "YOUR_HOTSPOT_PASSWORD";

const char *MQTT_HOST = "broker.hivemq.com";
const int   MQTT_PORT = 1883;
const char *PREFIX    = "apu-campuspulse-2026"; // same as the bridge --prefix and CP_MQTT_PREFIX
const char *ROOM_ID   = "B-08-05";
const char *DEVICE_ID = "esp32-b0805";
// ================================================================

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#if HAS_DHT22
#include <DHTesp.h>
DHTesp dht;
#endif
#if HAS_SCD4X
#include <Wire.h>
#include <SensirionI2cScd4x.h>
SensirionI2cScd4x scd4x;
#endif

const int DHT_PIN = 15, PIR_PIN = 27, BUTTON_PIN = 0, AC_LED = 2;
const uint32_t REPORT_MS = 5000;
const uint32_t OCCUPANCY_HOLD_MS = 60000;   // demo scale: AC stays on 1 min after the last motion

WiFiClient net;
PubSubClient mqtt(net);
String acMode = "schedule";
bool buttonOccupied = false;
uint32_t lastMotion = 0, sent = 0, acked = 0;
char telemetryTopic[80], commandTopic[80];
float co2Ppm = NAN;

void onCommand(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char *mode = doc["ac_mode"] | "";
  if (!strcmp(mode, "schedule") || !strcmp(mode, "after_hours_off") || !strcmp(mode, "occupancy")) {
    acMode = mode;
    Serial.printf("Command from CampusPulse: AC mode -> %s\n", mode);
  }
}

bool occupiedNow() {
#if HAS_PIR
  return lastMotion && millis() - lastMotion < 30000;
#else
  return buttonOccupied;
#endif
}

bool acAllowed() {
  if (acMode == "occupancy") {
#if HAS_PIR
    return lastMotion && millis() - lastMotion < OCCUPANCY_HOLD_MS;
#else
    return buttonOccupied;
#endif
  }
  struct tm now;
  bool open = true;
  if (getLocalTime(&now, 10)) {
    float h = now.tm_hour + now.tm_min / 60.0;
    open = now.tm_wday >= 1 && now.tm_wday <= 5 && h >= 7.5 && h < 21;
  }
  return open;  // "schedule" and "after_hours_off": the building's teaching hours
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Wi-Fi");
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print('.'); }
  if (WiFi.status() == WL_CONNECTED) Serial.printf(" ok, IP %s, RSSI %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else Serial.println(" FAILED: check WIFI_SSID / WIFI_PASS and use a 2.4 GHz network");
}

void connectMqtt() {
  while (WiFi.status() == WL_CONNECTED && !mqtt.connected()) {
    String id = String(DEVICE_ID) + "-" + String((uint32_t)esp_random(), HEX);
    Serial.print("MQTT ... ");
    if (mqtt.connect(id.c_str())) {
      Serial.println("connected");
      mqtt.subscribe(commandTopic);  // QoS 0: the command is retained, so it returns on every reconnect
    } else {
      Serial.printf("failed (%d), retrying\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nCampusPulse node %s in %s | DHT22:%d PIR:%d SCD4x:%d\n", DEVICE_ID, ROOM_ID, HAS_DHT22, HAS_PIR, HAS_SCD4X);
  pinMode(AC_LED, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
#if HAS_PIR
  pinMode(PIR_PIN, INPUT);
#endif
#if HAS_DHT22
  dht.setup(DHT_PIN, DHTesp::DHT22);
#endif
#if HAS_SCD4X
  Wire.begin(21, 22);
  scd4x.begin(Wire, SCD41_I2C_ADDR_62);
  scd4x.stopPeriodicMeasurement();
  delay(500);
  scd4x.startPeriodicMeasurement();  // first reading after ~5 s
#endif
  snprintf(telemetryTopic, sizeof telemetryTopic, "%s/%s/telemetry", PREFIX, ROOM_ID);
  snprintf(commandTopic, sizeof commandTopic, "%s/%s/command", PREFIX, ROOM_ID);
  connectWifi();
  configTime(8 * 3600, 0, "pool.ntp.org", "time.google.com");  // Malaysia time
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onCommand);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(90);
  mqtt.setSocketTimeout(30);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();

#if HAS_PIR
  if (digitalRead(PIR_PIN) == HIGH) lastMotion = millis();
#else
  static bool lastBtn = HIGH;          // BOOT button: press to toggle occupied / empty
  bool btn = digitalRead(BUTTON_PIN);
  if (lastBtn == HIGH && btn == LOW) {
    buttonOccupied = !buttonOccupied;
    Serial.printf("Room marked %s (BOOT button)\n", buttonOccupied ? "OCCUPIED" : "EMPTY");
  }
  lastBtn = btn;
#endif
  bool ac = acAllowed();
  digitalWrite(AC_LED, ac);

#if HAS_SCD4X
  bool ready = false;
  if (scd4x.getDataReadyStatus(ready) == 0 && ready) {
    uint16_t c = 0; float t, h;
    if (scd4x.readMeasurement(c, t, h) == 0 && c > 0) co2Ppm = c;
  }
#endif

  static uint32_t last = 0;
  if (millis() - last < REPORT_MS) { delay(20); return; }
  last = millis();

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["room_id"] = ROOM_ID;
  doc["occupied"] = occupiedNow();
  doc["ac_on"] = ac;
  doc["rssi_dbm"] = WiFi.RSSI();
  doc["packets_sent"] = sent ? 1 : 0;   // delivery of the previous report
  doc["packets_acked"] = sent ? acked : 0;
#if HAS_DHT22
  TempAndHumidity th = dht.getTempAndHumidity();
  if (!isnan(th.temperature)) doc["temp_c"] = th.temperature;
  if (!isnan(th.humidity)) doc["humidity_pct"] = th.humidity;
#endif
#if HAS_SCD4X
  if (!isnan(co2Ppm)) doc["co2_ppm"] = round(co2Ppm);
#endif
  char body[384];
  size_t n = serializeJson(doc, body);
  sent = 1;
  acked = mqtt.publish(telemetryTopic, (const uint8_t *)body, n, false) ? 1 : 0;
  Serial.printf("%s | AC %s (%s) -> %s\n", acked ? "sent" : "NOT SENT", ac ? "ON" : "off", acMode.c_str(), body);
}

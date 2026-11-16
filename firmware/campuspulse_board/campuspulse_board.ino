/*
  CampusPulse room node for a real ESP32: one sketch for whatever hardware you have.

  Set the switches below, then upload (guide: firmware/campuspulse_board/README.md).

  Bare ESP32 (HAS_DHT22 0). Still REAL data:
    - Wi-Fi signal strength and message delivery (they feed CampusPulse's trust score)
    - occupancy: press the board's BOOT button to mark the room occupied / empty
    - AC relay: the board's own blue LED (GPIO2) obeys CampusPulse's AC commands
  ESP32 + sensors (switch on what you have):
    - HAS_DHT22  temperature and humidity       DATA -> GPIO15
    - HAS_PIR    motion = occupancy (HC-SR501)  OUT  -> GPIO27
    - HAS_SCD4X  real CO2 (Sensirion SCD40/41)  SDA -> GPIO21, SCL -> GPIO22
  Anything not fitted (or not answering) is left out of the message and shows as "–" on the dashboard.

  How the data travels (USE_MQTT):
    0 (default) straight to the CampusPulse server over HTTPS: POST /api/ingest every 5 s, and
                GET /api/rooms/<ROOM>/command every 10 s for the AC mode. No bridge, works with the
                Cloud Run URL or a laptop on the same network.
    1           MQTT to a broker; run campuspulse.mqtt_bridge on the laptop to forward it.
*/

// ======================= 1. YOUR SETTINGS =======================
#define HAS_DHT22 1      // 1 if a DHT22 (AM2302) is wired to GPIO15 (harmless if it isn't: shows "–")
#define HAS_PIR   0      // 1 if a PIR motion sensor is wired to GPIO27
#define HAS_SCD4X 0      // 1 if a Sensirion SCD40/SCD41 is wired to I2C (21/22)
#define USE_MQTT  0      // 0 = HTTPS straight to the server (recommended), 1 = MQTT + bridge

const char *WIFI_SSID = "YOUR_HOTSPOT_NAME";   // a phone hotspot is the safest choice (2.4 GHz)
const char *WIFI_PASS = "YOUR_HOTSPOT_PASSWORD";

// HTTPS mode: the dashboard address and its ingest key (CP_INGEST_KEY / INGEST_KEY in .deploy.env).
// For a laptop server use http://<laptop-ip>:8000 (same hotspot) and the key from .env.
const char *SERVER_URL = "https://campuspulse-443933554054.asia-southeast1.run.app";
const char *INGEST_KEY = "PASTE_THE_INGEST_KEY_HERE";

// MQTT mode only
const char *MQTT_HOST = "broker.hivemq.com";
const int   MQTT_PORT = 1883;
const char *PREFIX    = "apu-campuspulse-2026"; // same as the bridge --prefix and CP_MQTT_PREFIX

const char *ROOM_ID   = "B-08-05";
const char *DEVICE_ID = "esp32-b0805";
// ================================================================

#include <WiFi.h>
#include <ArduinoJson.h>
#include <time.h>
#if USE_MQTT
#include <PubSubClient.h>
WiFiClient net;
PubSubClient mqtt(net);
char telemetryTopic[80], commandTopic[80];
#else
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
WiFiClientSecure tls;
WiFiClient plain;
#endif
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
const uint32_t REPORT_MS = 5000, COMMAND_MS = 10000;
const uint32_t OCCUPANCY_HOLD_MS = 60000;   // demo scale: AC stays on 1 min after the last motion

String acMode = "schedule";
volatile bool buttonPressed = false;
volatile uint32_t lastPressIsr = 0;
bool buttonOccupied = false;
uint32_t lastMotion = 0, sent = 0, acked = 0;
float co2Ppm = NAN;

void IRAM_ATTR onButton() {              // an interrupt, so a press during an HTTPS call is not missed
  uint32_t t = millis();
  if (t - lastPressIsr > 250) { buttonPressed = true; lastPressIsr = t; }
}

void applyMode(const char *mode) {
  if (!strcmp(mode, "schedule") || !strcmp(mode, "after_hours_off") || !strcmp(mode, "occupancy")) {
    if (acMode != mode) Serial.printf("Command from CampusPulse: AC mode -> %s\n", mode);
    acMode = mode;
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

#if USE_MQTT
void onCommand(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  applyMode(doc["ac_mode"] | "");
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

bool sendReading(const char *body, size_t n) {
  return mqtt.publish(telemetryTopic, (const uint8_t *)body, n, false);
}
#else
bool beginHttp(HTTPClient &http, const String &url) {
  http.setTimeout(15000);           // Cloud Run may take a few seconds to wake up
  if (url.startsWith("https://")) {
    tls.setInsecure();              // demo node: encrypted, but the certificate is not pinned
    return http.begin(tls, url);
  }
  return http.begin(plain, url);
}

bool sendReading(const char *body, size_t n) {
  HTTPClient http;
  if (!beginHttp(http, String(SERVER_URL) + "/api/ingest")) return false;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", INGEST_KEY);
  int code = http.POST((uint8_t *)body, n);
  if (code != 200) {
    Serial.printf("Server answered %d %s\n", code, code == 401 ? "(wrong INGEST_KEY)" : http.getString().c_str());
  }
  http.end();
  return code == 200;
}

void pullCommand() {
  HTTPClient http;
  if (!beginHttp(http, String(SERVER_URL) + "/api/rooms/" + ROOM_ID + "/command")) return;
  http.addHeader("X-Device-Key", INGEST_KEY);
  int code = http.GET();
  if (code == 200) {
    JsonDocument doc;
    if (!deserializeJson(doc, http.getString())) applyMode(doc["ac_mode"] | "");
  }
  http.end();
}
#endif

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nCampusPulse node %s in %s | DHT22:%d PIR:%d SCD4x:%d | %s\n", DEVICE_ID, ROOM_ID,
                HAS_DHT22, HAS_PIR, HAS_SCD4X, USE_MQTT ? "MQTT" : SERVER_URL);
  pinMode(AC_LED, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(BUTTON_PIN), onButton, FALLING);
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
  connectWifi();
  configTime(8 * 3600, 0, "pool.ntp.org", "time.google.com");  // Malaysia time
#if USE_MQTT
  snprintf(telemetryTopic, sizeof telemetryTopic, "%s/%s/telemetry", PREFIX, ROOM_ID);
  snprintf(commandTopic, sizeof commandTopic, "%s/%s/command", PREFIX, ROOM_ID);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onCommand);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(90);
  mqtt.setSocketTimeout(30);
#endif
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
#if USE_MQTT
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
#else
  static uint32_t lastCommand = 0;
  if (WiFi.status() == WL_CONNECTED && (lastCommand == 0 || millis() - lastCommand >= COMMAND_MS)) {
    lastCommand = millis();
    pullCommand();
  }
#endif

#if HAS_PIR
  if (digitalRead(PIR_PIN) == HIGH) lastMotion = millis();
#else
  if (buttonPressed) {                 // BOOT button: press to toggle occupied / empty
    buttonPressed = false;
    buttonOccupied = !buttonOccupied;
    Serial.printf("Room marked %s (BOOT button)\n", buttonOccupied ? "OCCUPIED" : "EMPTY");
  }
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
  if (last != 0 && millis() - last < REPORT_MS) { delay(20); return; }
  last = millis();
  if (WiFi.status() != WL_CONNECTED) return;

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
  if (!isnan(th.temperature)) doc["temp_c"] = round(th.temperature * 10) / 10.0;
  if (!isnan(th.humidity)) doc["humidity_pct"] = round(th.humidity);
#endif
#if HAS_SCD4X
  if (!isnan(co2Ppm)) doc["co2_ppm"] = round(co2Ppm);
#endif
  char body[384];
  size_t n = serializeJson(doc, body);
  sent = 1;
  acked = sendReading(body, n) ? 1 : 0;
  Serial.printf("%s | AC %s (%s) -> %s\n", acked ? "sent" : "NOT SENT", ac ? "ON" : "off", acMode.c_str(), body);
}

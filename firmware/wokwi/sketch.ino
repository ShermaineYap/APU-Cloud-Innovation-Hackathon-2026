/*
  CampusPulse virtual sensor node for Wokwi (https://wokwi.com), no hardware needed.

  The same job as firmware/campuspulse_node, on parts Wokwi can simulate:
    DHT22          temperature and humidity          (click it to change the values)
    PIR sensor     motion = room occupied             (click "Simulate motion")
    Potentiometer  CO2 from 400 to 2000 ppm           (Wokwi has no SCD40, so turn the knob)
    Blue LED       the AC relay: lit = AC allowed to run
    Green LED      MQTT connected

  Up:   JSON every 5 s to <PREFIX>/<ROOM_ID>/telemetry on a public MQTT broker.
        The CampusPulse bridge forwards it to /api/ingest, and the room turns "live".
  Down: subscribes to <PREFIX>/<ROOM_ID>/command (retained). Approve an AC change or an
        Autopilot plan in CampusPulse and the AC mode arrives here; the blue LED obeys it.

  Change PREFIX below to something only your team uses, and pass the same value to the
  bridge (--prefix) and the app (CP_MQTT_PREFIX).
*/
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>
#include <time.h>

const char *WIFI_SSID = "Wokwi-GUEST";  // Wokwi's simulated Wi-Fi, with real internet
const char *WIFI_PASS = "";
const char *MQTT_HOST = "broker.hivemq.com";
const int MQTT_PORT = 1883;
const char *PREFIX = "apu-campuspulse-2026";
const char *ROOM_ID = "B-08-05";
const char *DEVICE_ID = "esp32-b0805";

const int DHT_PIN = 15, PIR_PIN = 27, CO2_PIN = 34, AC_LED = 26, LINK_LED = 2;
const uint32_t REPORT_MS = 5000;
const uint32_t OCCUPANCY_HOLD_MS = 60000;  // demo scale: keep cooling 1 min after the last motion
const float AC_KW = 4.5;                   // Block B classroom estimate (as campus.py)

DHTesp dht;
WiFiClient net;
PubSubClient mqtt(net);
String acMode = "schedule";
uint32_t lastMotion = 0, sent = 0, acked = 0;
char telemetryTopic[80], commandTopic[80];

void onCommand(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char *mode = doc["ac_mode"] | "";
  if (!strcmp(mode, "schedule") || !strcmp(mode, "after_hours_off") || !strcmp(mode, "occupancy")) {
    acMode = mode;
    Serial.printf("Command from CampusPulse: AC mode -> %s\n", mode);
  }
}

bool acAllowed() {
  if (acMode == "occupancy") return lastMotion && millis() - lastMotion < OCCUPANCY_HOLD_MS;
  struct tm now;
  bool open = true;
  if (getLocalTime(&now, 10)) {
    float h = now.tm_hour + now.tm_min / 60.0;
    open = now.tm_wday >= 1 && now.tm_wday <= 5 && h >= 7.5 && h < 21;
  }
  if (acMode == "after_hours_off") return open;
  return open;  // "schedule": the building's fixed timetable
}

void connectMqtt() {
  while (!mqtt.connected()) {
    String id = String(DEVICE_ID) + "-wokwi-" + String((uint32_t)esp_random(), HEX);
    Serial.print("MQTT ... ");
    if (mqtt.connect(id.c_str())) {
      Serial.println("connected");
      mqtt.subscribe(commandTopic);  // QoS 0: the command is retained, so it comes back on every reconnect
    } else {
      Serial.printf("failed (%d), retrying\n", mqtt.state());
      delay(1500);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  pinMode(AC_LED, OUTPUT);
  pinMode(LINK_LED, OUTPUT);
  dht.setup(DHT_PIN, DHTesp::DHT22);
  snprintf(telemetryTopic, sizeof telemetryTopic, "%s/%s/telemetry", PREFIX, ROOM_ID);
  snprintf(commandTopic, sizeof commandTopic, "%s/%s/command", PREFIX, ROOM_ID);

  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);
  Serial.print("Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) { delay(250); Serial.print('.'); }
  Serial.printf(" ok, RSSI %d dBm\n", WiFi.RSSI());
  configTime(8 * 3600, 0, "pool.ntp.org");  // Malaysia time
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onCommand);
  mqtt.setBufferSize(512);
  // Generous keepalive: a simulator tab that slows down (or a busy campus Wi-Fi) must not drop the link.
  mqtt.setKeepAlive(90);
  mqtt.setSocketTimeout(30);
}

void loop() {
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
  digitalWrite(LINK_LED, mqtt.connected());
  if (digitalRead(PIR_PIN) == HIGH) lastMotion = millis();
  bool ac = acAllowed();
  digitalWrite(AC_LED, ac);

  static uint32_t last = 0;
  if (millis() - last < REPORT_MS) return;
  last = millis();

  TempAndHumidity th = dht.getTempAndHumidity();
  bool occupied = lastMotion && millis() - lastMotion < 30000;
  float co2 = 400 + analogRead(CO2_PIN) / 4095.0 * 1600 + random(-6, 7);  // sensor noise, so it never looks stuck
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["room_id"] = ROOM_ID;
  doc["occupied"] = occupied;
  doc["co2_ppm"] = round(co2);
  doc["temp_c"] = isnan(th.temperature) ? 25.0 : th.temperature;
  doc["humidity_pct"] = isnan(th.humidity) ? 60.0 : th.humidity;
  doc["ac_on"] = ac;
  doc["watts"] = ac ? AC_KW * 1000 * (occupied ? 0.8 : 0.6) + 260 : 60;
  doc["rssi_dbm"] = WiFi.RSSI();
  // Delivery for the previous report window (1 sent, 1 or 0 delivered), as the real node reports it.
  doc["packets_sent"] = sent ? 1 : 0;
  doc["packets_acked"] = sent ? acked : 0;
  char body[384];
  size_t n = serializeJson(doc, body);
  sent = 1;
  acked = mqtt.publish(telemetryTopic, (const uint8_t *)body, n, false) ? 1 : 0;
  Serial.printf("%s AC %s (%s) -> %s\n", occupied ? "occupied" : "empty", ac ? "ON" : "off", acMode.c_str(), body);
}

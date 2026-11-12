/*
  CampusPulse sensor node (ESP32)

  Senses:   PIR motion (HC-SR501), CO2 / temperature / humidity (Sensirion SCD40/41),
            power via a non-invasive current clamp (SCT-013-030, optional)
  Sends:    JSON every 30 s over MQTT QoS 1 to campuspulse/<ROOM_ID>/telemetry
            (or HTTPS POST straight to the ingest API when TRANSPORT_MQTT is 0)
  Network:  each report carries RSSI and how many of the previous window's
            reports the broker acknowledged. CampusPulse turns that into a
            per-room data-confidence score.
  Acts:     when Estates approves an AC change, CampusPulse sends this room's AC mode
            on campuspulse/<ROOM_ID>/command (QoS 1, retained), or the node polls
            GET /api/rooms/<ROOM_ID>/command on the HTTPS transport. The mode drives a
            relay (GPIO26) that enables the room's AC:
              schedule         relay closed: the building's own schedule is in charge
              after_hours_off  relay open at night and at weekends (NTP time, MYT)
              occupancy        relay closed only while motion was seen in the last
                               OCCUPANCY_HOLD_MS (pre-cool before booked classes is
                               left to the building schedule in this version)

  Libraries (Arduino Library Manager):
    espMqttClient (bertmelis)         MQTT 3.1.1 with QoS 1 acknowledgements
    ArduinoJson (bblanchon) v7
    Sensirion I2C SCD4x               SCD40/41 driver
    EmonLib (openenergymonitor)       only when HAS_CURRENT_CLAMP is 1

  Wiring (ESP32 DevKit):
    SCD40  SDA -> GPIO21, SCL -> GPIO22, VDD -> 3V3, GND -> GND
    PIR    OUT -> GPIO27, VCC -> 5V,  GND -> GND
    Relay  IN  -> GPIO26 (opto-isolated module; switches the AC controller's enable input,
                          never mains directly)
    SCT-013 via burden resistor + 1.65 V bias -> GPIO34 (ADC1)

  Copy secrets.h.example to secrets.h and fill in Wi-Fi and broker details.
*/

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <SensirionI2cScd4x.h>
#include "secrets.h"

#ifndef TRANSPORT_MQTT
#define TRANSPORT_MQTT 1
#endif
#ifndef HAS_CURRENT_CLAMP
#define HAS_CURRENT_CLAMP 0
#endif

#if TRANSPORT_MQTT
#include <espMqttClient.h>
espMqttClient mqtt;
#else
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#endif

#if HAS_CURRENT_CLAMP
#include <EmonLib.h>
EnergyMonitor clamp;
const float MAINS_VOLTS = 230.0;   // Malaysia
#endif

const int PIR_PIN = 27;
const int RELAY_PIN = 26;
const uint32_t REPORT_MS = 30000;
const uint32_t OCCUPANCY_HOLD_MS = 15UL * 60 * 1000;  // keep cooling 15 min after the last motion
const uint32_t COMMAND_POLL_MS = 5UL * 60 * 1000;     // HTTPS transport only

// AC mode sent by CampusPulse; "schedule" until told otherwise.
String acMode = "schedule";
uint32_t lastMotionMs = 0;

SensirionI2cScd4x scd4x;
volatile bool motionSeen = false;

// Window counters: reports sent in the previous window and how many the broker acked.
volatile uint16_t windowSent = 0, windowAcked = 0;
uint16_t lastSent = 0, lastAcked = 0;

void IRAM_ATTR onMotion() { motionSeen = true; }

void applyCommand(const uint8_t *payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char *mode = doc["ac_mode"] | "";
  if (strcmp(mode, "schedule") && strcmp(mode, "after_hours_off") && strcmp(mode, "occupancy")) return;
  acMode = mode;
  Serial.printf("AC mode -> %s\n", acMode.c_str());
}

// Relay closed = the AC may run. Called every loop.
void driveRelay() {
  bool allow = true;
  if (acMode == "occupancy") {
    allow = lastMotionMs && millis() - lastMotionMs < OCCUPANCY_HOLD_MS;
  } else if (acMode == "after_hours_off") {
    struct tm now;
    if (getLocalTime(&now, 10)) {
      float h = now.tm_hour + now.tm_min / 60.0;
      bool weekday = now.tm_wday >= 1 && now.tm_wday <= 5;
      allow = weekday && h >= 7.5 && h < 21;
    }
  }
  digitalWrite(RELAY_PIN, allow ? HIGH : LOW);
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Wi-Fi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print('.'); }
  Serial.printf(" %s, RSSI %d dBm\n", WiFi.status() == WL_CONNECTED ? "ok" : "failed", WiFi.RSSI());
}

#if TRANSPORT_MQTT
void setupMqtt() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setClientId(DEVICE_ID);
  if (strlen(MQTT_USER)) mqtt.setCredentials(MQTT_USER, MQTT_PASS);
  mqtt.onConnect([](bool) {
    Serial.println("MQTT connected");
    char cmdTopic[64];
    snprintf(cmdTopic, sizeof(cmdTopic), "campuspulse/%s/command", ROOM_ID);
    mqtt.subscribe(cmdTopic, 1);  // retained: the current mode arrives straight away
  });
  mqtt.onMessage([](const espMqttClientTypes::MessageProperties &, const char *, const uint8_t *payload,
                    size_t len, size_t index, size_t total) {
    if (index == 0 && len == total) applyCommand(payload, len);
  });
  mqtt.onDisconnect([](espMqttClientTypes::DisconnectReason r) {
    Serial.printf("MQTT disconnected (%u)\n", static_cast<uint8_t>(r));
  });
  mqtt.onPublish([](uint16_t) { windowAcked++; });  // PUBACK received for a QoS 1 publish
}
#endif

void setup() {
  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);  // fail safe: the building schedule stays in charge
  attachInterrupt(digitalPinToInterrupt(PIR_PIN), onMotion, RISING);

  Wire.begin(21, 22);
  scd4x.begin(Wire, SCD41_I2C_ADDR_62);
  scd4x.stopPeriodicMeasurement();
  delay(500);
  scd4x.startPeriodicMeasurement();  // a new reading every 5 s

#if HAS_CURRENT_CLAMP
  clamp.current(34, 30.0);  // SCT-013-030: 30 A per 1 V
#endif

  connectWifi();
  configTime(8 * 3600, 0, "pool.ntp.org", "time.google.com");  // Malaysia time, for after_hours_off
#if TRANSPORT_MQTT
  setupMqtt();
  mqtt.connect();
#endif
}

bool readAir(float &co2, float &temp, float &rh) {
  bool ready = false;
  if (scd4x.getDataReadyStatus(ready) != 0 || !ready) return false;
  uint16_t c = 0;
  if (scd4x.readMeasurement(c, temp, rh) != 0 || c == 0) return false;
  co2 = c;
  return true;
}

void report() {
  static float co2 = 0, temp = 0, rh = 0;
  float c, t, h;
  if (readAir(c, t, h)) { co2 = c; temp = t; rh = h; }
  if (co2 == 0) return;  // sensor still warming up

  bool occupied = motionSeen || digitalRead(PIR_PIN) == HIGH;
  motionSeen = false;

  // Close the window: report what happened to the previous window's publishes.
  lastSent = windowSent; lastAcked = windowAcked;
  windowSent = 0; windowAcked = 0;

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["room_id"] = ROOM_ID;
  doc["occupied"] = occupied;
  doc["co2_ppm"] = co2;
  doc["temp_c"] = temp;
  doc["humidity_pct"] = rh;
  doc["rssi_dbm"] = WiFi.RSSI();
  doc["packets_sent"] = lastSent ? lastSent : 1;
  doc["packets_acked"] = lastSent ? lastAcked : 1;
#if HAS_CURRENT_CLAMP
  doc["watts"] = clamp.calcIrms(1480) * MAINS_VOLTS;
#endif
  char body[384];
  size_t n = serializeJson(doc, body, sizeof(body));

#if TRANSPORT_MQTT
  char topic[64];
  snprintf(topic, sizeof(topic), "campuspulse/%s/telemetry", ROOM_ID);
  windowSent++;  // every attempt counts; one that never gets a PUBACK shows up as lost
  if (mqtt.connected()) mqtt.publish(topic, 1, false, body);
  (void)n;
#else
  WiFiClientSecure tls;
  tls.setInsecure();  // demo only: pin the Cloud Run certificate for production
  HTTPClient http;
  http.begin(tls, INGEST_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", INGEST_KEY);
  windowSent++;
  int code = http.POST((uint8_t *)body, n);
  if (code >= 200 && code < 300) windowAcked++;
  http.end();
#endif
  Serial.printf("%s %s\n", occupied ? "occupied" : "empty", body);
}

#if !TRANSPORT_MQTT
// HTTPS transport: ask CampusPulse which AC mode this room should run.
void pollCommand() {
  String url = String(INGEST_URL);
  url.replace("/api/ingest", String("/api/rooms/") + ROOM_ID + "/command");
  WiFiClientSecure tls;
  tls.setInsecure();  // demo: pin the Cloud Run certificate for production
  HTTPClient http;
  http.begin(tls, url);
  http.addHeader("X-Device-Key", INGEST_KEY);
  if (http.GET() == 200) {
    String body = http.getString();
    applyCommand((const uint8_t *)body.c_str(), body.length());
  }
  http.end();
}
#endif

void loop() {
  static uint32_t last = 0;
  if (WiFi.status() != WL_CONNECTED) connectWifi();
#if TRANSPORT_MQTT
  if (WiFi.status() == WL_CONNECTED && !mqtt.connected()) { mqtt.connect(); delay(500); }
#endif
  if (digitalRead(PIR_PIN) == HIGH) lastMotionMs = millis();
  if (millis() - last >= REPORT_MS) {
    last = millis();
    report();
  }
#if !TRANSPORT_MQTT
  static uint32_t lastPoll = 0;
  if (WiFi.status() == WL_CONNECTED && (lastPoll == 0 || millis() - lastPoll >= COMMAND_POLL_MS)) {
    lastPoll = millis();
    pollCommand();
  }
#endif
  driveRelay();
  delay(50);
}

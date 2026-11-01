# CampusPulse node

One ESP32 per room. It reports motion, CO2, temperature, humidity and (optionally) power every 30 seconds, plus the link-quality fields the confidence score is built on.

## Parts (about RM 200 per node)

| Part | Purpose |
| --- | --- |
| ESP32 DevKit (or ESP32-C3) | Wi-Fi microcontroller |
| Sensirion SCD40 or SCD41 | True NDIR CO2, temperature, humidity over I2C |
| HC-SR501 PIR | Motion, used as the occupancy signal |
| SCT-013-030 clamp + 10 µF, 2 × 10 kΩ bias (optional) | AC circuit power |

Prices vary by seller; check before ordering.

## Build

1. Install the libraries listed at the top of `campuspulse_node.ino` from the Arduino Library Manager (board package: esp32 by Espressif).
2. `cp secrets.h.example secrets.h` and fill in Wi-Fi, room and broker details.
3. Flash, then open the serial monitor at 115200 baud. You should see a JSON line every 30 s.

## Two ways to reach the cloud

- **MQTT (default):** node → Mosquitto broker on the campus LAN → `python -m campuspulse.mqtt_bridge` → Cloud Run `/api/ingest`. QoS 1 means the broker acknowledges every report; the node counts acknowledgements, so lost reports show up as a lower delivery ratio.
- **HTTPS:** set `TRANSPORT_MQTT 0`. The node posts directly to Cloud Run; a 2xx response counts as acknowledged.

Run a local broker for testing: `docker run -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf`.

## Notes

- The SCD4x needs a few minutes after power-on and benefits from a week of automatic self-calibration with regular fresh air.
- PIR sees motion, not head count, so the live room reports `occupied` and leaves `people` empty. The simulated rooms carry head counts; the app labels which is which.
- Not yet compile-tested on hardware. Check library versions on the first build and adjust the espMqttClient calls if the API has moved.

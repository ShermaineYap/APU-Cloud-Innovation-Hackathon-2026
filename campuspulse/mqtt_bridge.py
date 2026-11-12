"""MQTT to CampusPulse bridge.

Sensor nodes publish JSON to `campuspulse/<room_id>/telemetry` with QoS 1.
This bridge subscribes on the campus broker and forwards each message to the
ingest API over HTTPS. Run it next to the broker (laptop, Pi, or a small VM):

    python -m campuspulse.mqtt_bridge --broker localhost --api https://<service>.run.app
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import httpx
import paho.mqtt.client as mqtt

log = logging.getLogger("bridge")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--broker", default=os.getenv("MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    p.add_argument("--username", default=os.getenv("MQTT_USER"))
    p.add_argument("--password", default=os.getenv("MQTT_PASS"))
    p.add_argument("--tls", action="store_true", default=os.getenv("MQTT_TLS") == "1")
    p.add_argument("--api", default=os.getenv("CP_API", "http://localhost:8080"))
    p.add_argument("--key", default=os.getenv("CP_INGEST_KEY", "dev-ingest-key"))
    p.add_argument("--prefix", default=os.getenv("CP_MQTT_PREFIX", "campuspulse"),
                   help="topic prefix; use a unique one on a public broker")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    TOPIC = f"{args.prefix}/+/telemetry"
    http = httpx.Client(base_url=args.api, headers={"X-Device-Key": args.key}, timeout=10)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("connected to %s:%s (%s), subscribing %s", args.broker, args.port, reason_code, TOPIC)
        client.subscribe(TOPIC, qos=1)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            payload.setdefault("room_id", msg.topic.split("/")[1])
            r = http.post("/api/ingest", json=payload)
            log.info("%s -> %s %s", msg.topic, r.status_code, r.text[:120])
        except Exception as e:
            log.warning("dropped message on %s: %s", msg.topic, e)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"campuspulse-bridge-{os.getpid()}")
    if args.username:
        client.username_pw_set(args.username, args.password)
    if args.tls:
        client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()

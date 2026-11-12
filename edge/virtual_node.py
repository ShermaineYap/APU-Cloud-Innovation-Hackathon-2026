"""A virtual CampusPulse sensor node in Python: the same messages as firmware/wokwi/sketch.ino.

Use it to test the MQTT path without Wokwi, or as a backup during the demo.
It publishes telemetry for one room to <prefix>/<room>/telemetry every few seconds and
prints every AC command that arrives on <prefix>/<room>/command.

    python edge/virtual_node.py --broker broker.hivemq.com --prefix apu-campuspulse-2026
    python edge/virtual_node.py --people 35 --co2 1250        # a crowded, stuffy room
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time

import paho.mqtt.client as mqtt


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--broker", default=os.getenv("CP_MQTT_HOST", "broker.hivemq.com"))
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--prefix", default=os.getenv("CP_MQTT_PREFIX", "apu-campuspulse-2026"))
    p.add_argument("--room", default="B-08-05")
    p.add_argument("--device", default="esp32-b0805")
    p.add_argument("--people", type=int, default=0, help="0 = empty room")
    p.add_argument("--co2", type=float, default=None, help="CO2 ppm (default follows people)")
    p.add_argument("--every", type=float, default=5.0, help="seconds between reports")
    p.add_argument("--count", type=int, default=0, help="stop after N reports (0 = run until Ctrl+C)")
    args = p.parse_args()

    tele, cmd = f"{args.prefix}/{args.room}/telemetry", f"{args.prefix}/{args.room}/command"
    state = {"mode": "schedule"}

    def on_connect(c, userdata, flags, reason_code, properties=None):
        print(f"connected to {args.broker}; publishing {tele}; listening on {cmd}")
        c.subscribe(cmd, qos=1)

    def on_message(c, userdata, msg):
        try:
            state["mode"] = json.loads(msg.payload).get("ac_mode", state["mode"])
            print(f"<- command from CampusPulse: AC mode = {state['mode']}")
        except ValueError:
            pass

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{args.device}-virtual-{random.randint(0, 99999)}")
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(args.broker, args.port, keepalive=30)
    c.loop_start()
    n = 0
    try:
        while not args.count or n < args.count:
            occupied = args.people > 0
            ac = occupied if state["mode"] == "occupancy" else True
            co2 = (args.co2 if args.co2 is not None else 430 + args.people * 22) + random.uniform(-6, 6)
            body = {"device_id": args.device, "room_id": args.room, "occupied": occupied, "people": args.people or None,
                    "co2_ppm": round(co2), "temp_c": round((24.0 if ac else 29.5) + random.uniform(-0.2, 0.2), 1),
                    "humidity_pct": 60, "ac_on": ac, "watts": 4500 * 0.7 + 260 if ac else 60,
                    "rssi_dbm": -58, "packets_sent": 1, "packets_acked": 1}
            c.publish(tele, json.dumps(body), qos=1)
            print(f"-> {body['co2_ppm']} ppm, {args.people} people, AC {'on' if ac else 'off'} ({state['mode']})")
            n += 1
            time.sleep(args.every)
    except KeyboardInterrupt:
        pass
    finally:
        c.loop_stop()
        c.disconnect()


if __name__ == "__main__":
    main()

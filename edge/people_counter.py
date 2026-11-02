"""CampusPulse edge camera: count people with YOLO and send only the count.

Runs next to the room (a laptop, a mini PC, or any machine with a camera).
Frames never leave this device and are never saved: only {room, people} goes
to CampusPulse, the same way the ESP32 sensor node reports.

    # Mac webcam, room B-08-05, local CampusPulse
    python edge/people_counter.py --room B-08-05 --show

    # A phone as the camera (e.g. the "IP Webcam" app on Android, or any RTSP/MJPEG URL)
    python edge/people_counter.py --source http://192.168.1.23:8080/video --room B-08-05

    # Deployed CampusPulse
    python edge/people_counter.py --server https://campuspulse-xxxx.run.app --key $CP_INGEST_KEY

    # Check the model on a photo, without a camera or a server
    python edge/people_counter.py --image classroom.jpg

Network behaviour: each report is an HTTPS POST. Failed reports are kept in a
small queue and retried, and the node reports how many it attempted and how
many the server confirmed (packets_sent / packets_acked), so CampusPulse can
score this camera's link the same way it scores the sensor nodes.
"""
from __future__ import annotations

import argparse
import collections
import os
import statistics
import sys
import time
from datetime import datetime, timezone

PERSON = 0  # COCO class id for "person"


def load_model(name: str):
    try:
        from ultralytics import YOLO
    except ImportError:
        sys.exit("Ultralytics is not installed. Run: python -m pip install -r edge/requirements.txt")
    return YOLO(name)


def count_people(model, frame, conf: float, imgsz: int) -> tuple[int, list]:
    """People in one frame, and their boxes (for the optional local preview)."""
    result = model.predict(frame, classes=[PERSON], conf=conf, imgsz=imgsz, verbose=False)[0]
    boxes = result.boxes.xyxy.tolist() if result.boxes is not None else []
    return len(boxes), boxes


def draw(frame, boxes, n: int, room: str):
    import cv2

    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (109, 122, 31), 2)
    label = f"{room}: {n} {'person' if n == 1 else 'people'} (only this number is sent)"
    cv2.rectangle(frame, (0, 0), (len(label) * 11 + 20, 34), (48, 42, 29), -1)
    cv2.putText(frame, label, (10, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (242, 245, 244), 1, cv2.LINE_AA)
    return frame


class Reporter:
    """POSTs counts to /api/ingest, queues failures, and tracks delivery for link scoring."""

    def __init__(self, server: str, key: str, room: str, device_id: str, timeout: float = 5.0):
        import httpx

        self.http = httpx.Client(timeout=timeout)
        self.url = server.rstrip("/") + "/api/ingest"
        self.headers = {"X-Device-Key": key}
        self.room, self.device_id = room, device_id
        self.queue: collections.deque[dict] = collections.deque(maxlen=120)  # ~10 min at 5 s

    def report(self, people: int) -> bool:
        self.queue.append({"device_id": self.device_id, "room_id": self.room, "occupied": people > 0,
                           "people": people, "ts": datetime.now(timezone.utc).isoformat(), "packets_sent": 0})
        # Each report carries how many times it was attempted (packets_sent) and that it arrived
        # (packets_acked = 1), so the server's delivery ratio = reports delivered / attempts made.
        for r in self.queue:
            r["packets_sent"] += 1
            r["packets_acked"] = 1
        batch = list(self.queue)
        try:
            res = self.http.post(self.url, json=batch, headers=self.headers)
        except Exception as e:  # network down: keep the queue, try again next time
            print(f"  not sent ({type(e).__name__}); {len(self.queue)} report(s) queued")
            return False
        if res.status_code == 200:
            self.queue.clear()
            return True
        if res.status_code in (401, 422):
            sys.exit(f"CampusPulse rejected the report ({res.status_code}): {res.text[:200]}")
        print(f"  server said {res.status_code}; {len(self.queue)} report(s) queued")
        return False


def run_image(args) -> None:
    import cv2

    model = load_model(args.model)
    frame = cv2.imread(args.image)
    if frame is None:
        sys.exit(f"Could not read {args.image}")
    n, boxes = count_people(model, frame, args.conf, args.imgsz)
    print(f"{n} {'person' if n == 1 else 'people'} in {args.image}")
    if args.save:
        cv2.imwrite(args.save, draw(frame, boxes, n, args.room))
        print(f"Preview with boxes written to {args.save} (stays on this device)")


def run_camera(args) -> None:
    import cv2

    model = load_model(args.model)
    source = int(args.source) if str(args.source).isdigit() else args.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        sys.exit(f"Could not open camera {args.source}. On a Mac, allow camera access for your terminal "
                 "in System Settings > Privacy & Security > Camera, then run again.")
    device_id = args.device or f"cam-{args.room.lower().replace('-', '')}"
    reporter = None if args.dry_run else Reporter(args.server, args.key, args.room, device_id)
    window = collections.deque(maxlen=args.smooth)  # median of recent frames ignores one-frame misses
    last = 0.0
    print(f"Counting people for {args.room} as {device_id}. Sending to "
          f"{'nowhere (dry run)' if args.dry_run else reporter.url} every {args.interval:g} s. Ctrl+C to stop.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Camera frame missing; retrying")
                time.sleep(1)
                continue
            n, boxes = count_people(model, frame, args.conf, args.imgsz)
            window.append(n)
            if args.show:
                cv2.imshow("CampusPulse camera (local preview)", draw(frame, boxes, n, args.room))
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            if time.time() - last >= args.interval:
                last = time.time()
                people = int(statistics.median(window))
                ok = reporter.report(people) if reporter else True
                print(f"{datetime.now():%H:%M:%S}  {args.room}: {people} people  {'sent' if ok else ''}")
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if args.show:
            cv2.destroyAllWindows()


def main() -> None:
    ap = argparse.ArgumentParser(description="Count people with YOLO and send only the count to CampusPulse.")
    ap.add_argument("--room", default=os.getenv("CP_LIVE_ROOM", "B-08-05"), help="room id, e.g. B-08-05 or AUD-1")
    ap.add_argument("--source", default="0", help="camera index (0 = built-in webcam) or a stream URL")
    ap.add_argument("--server", default=os.getenv("CP_SERVER", "http://localhost:8080"))
    ap.add_argument("--key", default=os.getenv("CP_INGEST_KEY", "dev-ingest-key"), help="X-Device-Key")
    ap.add_argument("--device", help="device id (default cam-<room>)")
    ap.add_argument("--model", default="yolo26n.pt", help="YOLO weights; downloaded on first run")
    ap.add_argument("--conf", type=float, default=0.4, help="detection confidence threshold")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between reports")
    ap.add_argument("--smooth", type=int, default=5, help="frames in the median window")
    ap.add_argument("--show", action="store_true", help="local preview window with boxes (press q to quit)")
    ap.add_argument("--dry-run", action="store_true", help="count but do not send")
    ap.add_argument("--image", help="count people in one photo and exit")
    ap.add_argument("--save", help="with --image: write a preview with boxes to this path")
    args = ap.parse_args()
    run_image(args) if args.image else run_camera(args)


if __name__ == "__main__":
    main()

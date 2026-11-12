"""Real Kuala Lumpur weather for the simulator, the planner and the header.

Hourly outdoor temperature at APU (Bukit Jalil) from Open-Meteo: the last 30 days
of observations and the next 3 days of forecast, free and keyless. Cached in
memory and on disk; with no network the app uses a typical KL daily curve
(24 °C at dawn, 32 °C mid-afternoon) and says so.

Set CP_WEATHER=off to force the typical curve (tests do, for repeatable runs).
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger("campuspulse.weather")

APU_LAT, APU_LON = 3.0553, 101.7005
URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
       "&hourly=temperature_2m,relative_humidity_2m&past_days=31&forecast_days=3&timezone=UTC")
CACHE = Path(os.getenv("CP_WEATHER_CACHE", "data/weather_cache.json"))
REFRESH_S = 3 * 3600


class Weather:
    def __init__(self):
        self.lock = threading.Lock()
        self.temps: dict[int, float] = {}     # unix hour (UTC) -> °C
        self.humid: dict[int, float] = {}
        self.fetched = 0.0
        self.source = "typical KL daily curve"

    @staticmethod
    def _typical(ts: datetime) -> float:
        h = ts.astimezone(settings.timezone)
        hour = h.hour + h.minute / 60
        return 28.0 - 4.0 * math.cos((hour - 2.0) / 24 * 2 * math.pi)  # min ~24 at 02-06, max ~32 at 14

    def _load(self, data: dict) -> None:
        hours = data["hourly"]["time"]
        t = data["hourly"]["temperature_2m"]
        rh = data["hourly"].get("relative_humidity_2m") or [None] * len(t)
        temps, humid = {}, {}
        for iso, v, h in zip(hours, t, rh):
            if v is None:
                continue
            k = int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() // 3600)
            temps[k] = float(v)
            if h is not None:
                humid[k] = float(h)
        if temps:
            self.temps, self.humid = temps, humid

    def refresh(self, force: bool = False) -> None:
        if os.getenv("CP_WEATHER", "on").lower() == "off":
            return
        with self.lock:
            if not force and time.time() - self.fetched < REFRESH_S:
                return
            self.fetched = time.time()
        try:
            import httpx
            r = httpx.get(URL.format(lat=APU_LAT, lon=APU_LON), timeout=8)
            r.raise_for_status()
            data = r.json()
            with self.lock:
                self._load(data)
                self.source = "Open-Meteo (live, Bukit Jalil)"
            try:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(data))
            except OSError:
                pass
        except Exception as e:
            log.warning("weather fetch failed (%s); using cache or typical curve", e)
            if not self.temps and CACHE.is_file():
                try:
                    with self.lock:
                        self._load(json.loads(CACHE.read_text()))
                        self.source = "Open-Meteo (cached)"
                except Exception:
                    pass

    def outdoor_c(self, ts: datetime) -> float:
        """Outdoor temperature at a moment, interpolated between hourly values."""
        if not self.temps:
            self.refresh()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        x = ts.timestamp() / 3600
        k0 = int(x)
        a, b = self.temps.get(k0), self.temps.get(k0 + 1)
        if a is None or b is None:
            return a if a is not None else (b if b is not None else self._typical(ts))
        return a + (b - a) * (x - k0)

    def now(self) -> dict:
        ts = datetime.now(timezone.utc)
        self.refresh()
        k = int(ts.timestamp() // 3600)
        return {"temp_c": round(self.outdoor_c(ts), 1), "humidity_pct": self.humid.get(k),
                "source": self.source if self.temps else "typical KL daily curve"}

    def day_profile(self, day_local) -> list[float]:
        """Hourly outdoor °C (local hours 0-23) for a date, forecast or observed."""
        out = []
        for h in range(24):
            ts = datetime(day_local.year, day_local.month, day_local.day, h, tzinfo=settings.timezone)
            out.append(round(self.outdoor_c(ts), 1))
        return out


weather = Weather()

"""Runtime settings, read once from the environment.

Two backends share one code path:
  * local  - DuckDB file, no cloud account needed (default, used for dev and tests)
  * bigquery - Google BigQuery dataset, used on Cloud Run
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=value lines from .env into the environment.

    Variables already set in the shell win, so Cloud Run and CI are unaffected.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


_load_dotenv(os.getenv("CP_ENV_FILE", ".env"))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    backend: str = field(default_factory=lambda: os.getenv("CP_BACKEND", "local"))
    duckdb_path: str = field(default_factory=lambda: os.getenv("CP_DUCKDB_PATH", "data/campuspulse.duckdb"))

    gcp_project: str | None = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT"))
    bq_dataset: str = field(default_factory=lambda: os.getenv("CP_BQ_DATASET", "campuspulse"))
    bq_location: str = field(default_factory=lambda: os.getenv("CP_BQ_LOCATION", "asia-southeast1"))
    pubsub_topic: str | None = field(default_factory=lambda: os.getenv("CP_PUBSUB_TOPIC"))

    # Gemini: Vertex AI when CP_GEMINI_VERTEX=true, otherwise an AI Studio key.
    gemini_vertex: bool = field(default_factory=lambda: _bool("CP_GEMINI_VERTEX", False))
    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: os.getenv("CP_GEMINI_MODEL", "gemini-3.5-flash-lite"))
    gemini_location: str = field(default_factory=lambda: os.getenv("CP_GEMINI_LOCATION", "global"))

    # MQTT broker for device commands (approved AC changes). Unset: nodes pull commands over HTTPS.
    mqtt_host: str | None = field(default_factory=lambda: os.getenv("CP_MQTT_HOST") or None)
    mqtt_port: int = field(default_factory=lambda: int(os.getenv("CP_MQTT_PORT", "1883")))
    mqtt_user: str | None = field(default_factory=lambda: os.getenv("CP_MQTT_USER") or None)
    mqtt_pass: str | None = field(default_factory=lambda: os.getenv("CP_MQTT_PASS") or None)
    mqtt_tls: bool = field(default_factory=lambda: _bool("CP_MQTT_TLS", False))
    # Topic prefix: <prefix>/<room_id>/telemetry and /command. Make it unique on a public broker.
    mqtt_prefix: str = field(default_factory=lambda: os.getenv("CP_MQTT_PREFIX", "campuspulse"))

    ingest_key: str = field(default_factory=lambda: os.getenv("CP_INGEST_KEY", "dev-ingest-key"))
    live_simulator: bool = field(default_factory=lambda: _bool("CP_LIVE_SIM", True))
    history_days: int = field(default_factory=lambda: int(os.getenv("CP_HISTORY_DAYS", "28")))

    # Electricity price used for savings estimates. An assumption, shown in the UI.
    # Set it from the current TNB commercial tariff before presenting.
    tariff_rm_per_kwh: float = field(default_factory=lambda: float(os.getenv("CP_TARIFF_RM_PER_KWH", "0.50")))
    # Grid emission factor for carbon figures (kg CO2e per kWh). An assumption, shown in the UI.
    # 0.758 is the commonly cited Peninsular Malaysia grid factor; set CP_GRID_KG_CO2E_PER_KWH to update it.
    grid_kg_co2e_per_kwh: float = field(default_factory=lambda: float(os.getenv("CP_GRID_KG_CO2E_PER_KWH", "0.758")))
    co2_limit_ppm: int = field(default_factory=lambda: int(os.getenv("CP_CO2_LIMIT_PPM", "1000")))
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.getenv("CP_TZ", "Asia/Kuala_Lumpur")))

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key) or (self.gemini_vertex and bool(self.gcp_project))


settings = Settings()

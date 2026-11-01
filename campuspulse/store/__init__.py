"""Storage backends. All SQL is written in BigQuery dialect."""
from __future__ import annotations

from functools import lru_cache

from ..config import settings
from .base import Store


@lru_cache(maxsize=1)
def get_store() -> Store:
    if settings.backend == "bigquery":
        from .bq import BigQueryStore

        return BigQueryStore()
    from .duck import DuckStore

    return DuckStore(settings.duckdb_path)


__all__ = ["Store", "get_store"]

"""Create the BigQuery dataset, load simulated history and train BigQuery ML.

    GOOGLE_CLOUD_PROJECT=my-project CP_BACKEND=bigquery python scripts/bq_setup.py --seed
    GOOGLE_CLOUD_PROJECT=my-project CP_BACKEND=bigquery python scripts/bq_setup.py --ml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campuspulse.config import settings  # noqa: E402
from campuspulse.simulator import Fleet  # noqa: E402
from campuspulse.store.bq import BigQueryStore  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", action="store_true", help="load simulated history if the table is empty")
    p.add_argument("--ml", action="store_true", help="train the ARIMA_PLUS models")
    args = p.parse_args()
    if settings.backend != "bigquery" or not settings.gcp_project:
        sys.exit("Set CP_BACKEND=bigquery and GOOGLE_CLOUD_PROJECT first.")

    store = BigQueryStore()
    print(f"Setting up {store.ds} in {settings.bq_location}")
    store.setup(ml=False)
    if args.seed:
        if store.reading_count() == 0:
            rows = Fleet().history()
            print(f"Loading {len(rows):,} simulated readings")
            store.insert_readings(rows)
        else:
            print("readings already has data; skipping seed")
    if args.ml:
        print("Training BigQuery ML models (a few minutes)")
        store._run_file("ml.sql")
    print("Done.")


if __name__ == "__main__":
    main()

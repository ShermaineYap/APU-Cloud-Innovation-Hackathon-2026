import os

# Settings are read at import, so configure the test environment first.
os.environ.update(CP_BACKEND="local", CP_DUCKDB_PATH=":memory:", CP_LIVE_SIM="false",
                  CP_HISTORY_DAYS="14", CP_INGEST_KEY="test-key",
                  CP_WEATHER="off", CP_ENV_FILE="tests/no-such.env")  # never read the real .env
os.environ.pop("GEMINI_API_KEY", None)

import pytest  # noqa: E402

from campuspulse.simulator import Fleet  # noqa: E402
from campuspulse.store.duck import DuckStore  # noqa: E402


@pytest.fixture(scope="session")
def store():
    s = DuckStore(":memory:")
    s.setup()
    s.insert_readings(Fleet().history())
    return s

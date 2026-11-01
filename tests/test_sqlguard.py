import pytest

from campuspulse.agent.sqlguard import MAX_ROWS, UnsafeSQL, guard


@pytest.mark.parametrize("sql", [
    "DELETE FROM readings",
    "INSERT INTO rooms VALUES ('x')",
    "UPDATE rooms SET capacity = 0",
    "DROP TABLE readings",
    "SELECT 1; DROP TABLE rooms",
    "SELECT * FROM secrets",
    "SELECT * FROM `other-project.ds.readings`",
    "SELECT * FROM INFORMATION_SCHEMA.TABLES",
    "CREATE TABLE x AS SELECT 1",
])
def test_rejects_unsafe(sql):
    with pytest.raises(UnsafeSQL):
        guard(sql)


def test_adds_limit():
    assert f"LIMIT {MAX_ROWS}" in guard("SELECT room_id FROM v_room_hourly")


def test_caps_large_limit():
    assert f"LIMIT {MAX_ROWS}" in guard("SELECT room_id FROM v_room_hourly LIMIT 100000")


def test_keeps_small_limit_and_ctes():
    out = guard("WITH x AS (SELECT room_id FROM rooms) SELECT * FROM x LIMIT 5")
    assert "LIMIT 5" in out

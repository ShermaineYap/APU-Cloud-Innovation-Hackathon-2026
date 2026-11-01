"""Read-only guard for SQL written by Gemini.

Only a single SELECT over the curated tables and views is allowed, and a row
limit is always applied. Anything else is rejected before it reaches the
warehouse, so a confused or manipulated model cannot change data.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

ALLOWED = {"v_room_hourly", "v_empty_cooled", "v_network_daily", "v_slot_attendance",
           "rooms", "timetable", "readings"}
MAX_ROWS = 200


class UnsafeSQL(ValueError):
    pass


def guard(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"could not parse SQL: {e}") from e
    if len(statements) != 1 or statements[0] is None:
        raise UnsafeSQL("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQL("only SELECT queries are allowed")
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Merge, exp.Alter, exp.Command):
        if tree.find(bad):
            raise UnsafeSQL("data-changing statements are not allowed")

    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name in ctes:
            continue
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeSQL("use bare table names, without a project or dataset")
        if name not in ALLOWED:
            raise UnsafeSQL(f"table '{table.name}' is not available; use one of {sorted(ALLOWED)}")

    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(MAX_ROWS)
    else:
        try:
            if int(limit.expression.this) > MAX_ROWS:
                tree = tree.limit(MAX_ROWS)
        except (AttributeError, ValueError):
            tree = tree.limit(MAX_ROWS)
    return tree.sql(dialect="bigquery")

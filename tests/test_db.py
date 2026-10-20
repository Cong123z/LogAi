from datetime import date

import pytest

from logai2.db import (StaleRevision, drop_partitions_before, ensure_partitions, get_setting,
                       migrate, put_setting)


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []


def test_tables_exist(conn):
    names = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert {"services", "templates", "hourly_counts_20261015", "baseline",
            "alert_state", "documents", "commands", "worker_status"} <= names


def test_partitions_idempotent_and_droppable(conn):
    ensure_partitions(conn, date(2026, 10, 1), date(2026, 10, 2))   # already there: no error
    dropped = drop_partitions_before(conn, "hourly_counts", date(2026, 10, 3))
    assert dropped == ["hourly_counts_20261001", "hourly_counts_20261002"]


def test_setting_revision_conflict(conn):
    rev = put_setting(conn, "x", {"a": 1})
    put_setting(conn, "x", {"a": 2}, revision=rev)
    with pytest.raises(StaleRevision):
        put_setting(conn, "x", {"a": 3}, revision=rev)
    assert get_setting(conn, "x") == {"a": 2}

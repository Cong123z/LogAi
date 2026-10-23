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


def test_migration_004_assigns_documents_to_the_one_service_that_uses_them(conn):
    from logai2.db import MIGRATIONS_DIR
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES "
                 "('ONE', 't', 'h', 'r'), ('TWO', 't', 'h', 'r'), ('NONE', 't', 'h', 'r')")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES "
                 "('a', 'a:1', 'ONE', 'manual'), ('a', 'a:2', 'ONE', 'corpus'), "
                 "('a', 'a:3', 'TWO', 'manual'), ('b', 'b:1', 'TWO', 'manual')")
    conn.execute("UPDATE documents SET service = NULL")
    sql = (MIGRATIONS_DIR / "004_document_service.sql").read_text()
    backfill = sql[sql.index("UPDATE documents"):sql.index("CREATE INDEX")]
    conn.execute(backfill)   # run again on these rows
    rows = {r["doc_id"]: r["service"] for r in conn.execute("SELECT doc_id, service FROM documents")}
    assert rows == {"ONE": "a", "TWO": None, "NONE": None}


def test_module_can_belong_to_two_services(conn):
    conn.execute("INSERT INTO services VALUES ('a', 1), ('b', 2)")
    conn.execute("INSERT INTO modules (module_code, service) VALUES ('M1', 'a'), ('M1', 'b')")
    assert conn.execute("SELECT count(*) AS n FROM modules WHERE module_code = 'M1'").fetchone()["n"] == 2


def test_migration_007_seeds_from_alert_minutes(conn):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz)
    m1 = (now - timedelta(days=2)).replace(hour=10, minute=5, second=0, microsecond=0)
    m2 = m1.replace(minute=40)
    old = now - timedelta(days=20)
    m3 = (m1 + timedelta(days=1)).replace(hour=0, minute=30)     # 17:30 UTC, next local day, hour 0
    for m in (m1, m3, old):
        ensure_partitions(conn, m.date(), m.date())
    conn.execute("DROP TABLE template_module_hours, template_modules")
    conn.execute("DELETE FROM schema_migrations WHERE version = 7")
    for key, m, n in (("s:T1", m1, 3), ("s:T1", m2, 4), ("s:T1", m3, 2), ("s:unparsed", m1, 8), ("s:T1", old, 5), ("*total", m1, 9), ("doc:x", m1, 9)):
        conn.execute("INSERT INTO alert_minutes (service, key, module_code, day, minute, count) "
                     "VALUES ('s', %s, 'M1', %s, %s, %s)", (key, m.date(), m, n))
    assert migrate(conn) == [7]
    hours = conn.execute("SELECT template_id, module_code, day, hour, count FROM template_module_hours ORDER BY day").fetchall()
    assert hours == [{"template_id": "s:T1", "module_code": "M1", "day": m1.date(), "hour": 10, "count": 7},
                     {"template_id": "s:T1", "module_code": "M1", "day": m3.date(), "hour": 0, "count": 2}]
    pairs = conn.execute("SELECT template_id, first_seen, last_seen FROM template_modules").fetchall()
    assert pairs == [{"template_id": "s:T1", "first_seen": old, "last_seen": m3}]

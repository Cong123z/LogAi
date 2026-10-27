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


LAB_TABLES = {"lab_minute_counts", "lab_runs", "lab_results", "lab_episodes", "lab_bands", "lab_sweeps",
              "lab_choices", "lab_incidents"}


def test_migration_010_creates_lab_tables_on_009(conn):
    conn.execute("DROP TABLE lab_minute_counts, lab_results, lab_episodes, lab_bands, lab_sweeps, "
                 "lab_choices, lab_incidents, lab_runs CASCADE")
    conn.execute("DELETE FROM schema_migrations WHERE version IN (10, 11)")
    assert migrate(conn) == [10, 11]
    names = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert LAB_TABLES <= names
    assert conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] == 11


def test_migration_011_lab_knob_and_daily_bands(conn):
    conn.execute("DROP TABLE lab_episodes, lab_bands")
    conn.execute("DELETE FROM schema_migrations WHERE version = 11")
    conn.execute("CREATE TABLE lab_episodes (run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, "
                 "service text, key text, start_minute int, end_minute int, peak_score double precision, "
                 "alerted_minutes int, PRIMARY KEY (run_id, method, service, key, start_minute))")
    conn.execute("CREATE TABLE lab_bands (run_id bigint, method text, service text, key text, step smallint, "
                 "minute int, count int, expected real, low real, high real, high_flag boolean)")
    assert migrate(conn) == [11]
    cols = lambda t: {r["column_name"]: r for r in conn.execute(     # noqa: E731
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s", (t,))}
    assert cols("lab_episodes")["knob"]["is_nullable"] == "NO"
    b = cols("lab_bands")
    assert {"day", "step", "counts", "expected", "low", "high", "flags", "knob"} <= set(b) and "minute" not in b
    pk = lambda t: [r["attname"] for r in conn.execute(                # noqa: E731
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY array_position(i.indkey::int[], a.attnum::int)", (t,))]
    assert pk("lab_episodes") == ["run_id", "method", "knob", "service", "key", "start_minute"]
    assert pk("lab_bands") == ["run_id", "method", "knob", "service", "key", "day"]
    rid = conn.execute("INSERT INTO lab_runs (site, test, code_version, window_from, window_to, tune_to, seed, params) "
                       "VALUES ('laptop', 'B', 'x', '2026-10-01', '2026-10-02', '2026-10-01', 1, '{}') RETURNING id"
                       ).fetchone()["id"]
    conn.execute("INSERT INTO lab_bands VALUES (%s, 'm', 3.0, 's', 'k', '2026-10-01', 5, %s, %s, %s, %s, %s)",
                 (rid, [1], [1.0], [0.0], [2.0], [False]))
    conn.execute("DELETE FROM lab_runs")
    assert conn.execute("SELECT count(*) AS n FROM lab_bands").fetchone()["n"] == 0     # cascade


def test_lab_minute_counts_is_partitioned(conn):
    assert conn.execute("SELECT relkind FROM pg_class WHERE oid = to_regclass('lab_minute_counts')").fetchone()["relkind"] == "p"
    ensure_partitions(conn, date(2026, 11, 5), date(2026, 11, 5))
    assert conn.execute("SELECT to_regclass('lab_minute_counts_20261105') AS t").fetchone()["t"]


def test_migration_010_on_an_empty_database(schema):
    from tests.conftest import TEST_DSN
    from logai2.db import connect
    c = connect(TEST_DSN)
    try:
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"SET search_path TO {schema}, public")
        c.commit()
        assert migrate(c)[-1] == 11
        names = {r["tablename"] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
        assert LAB_TABLES <= names
        c.rollback()
    finally:
        c.rollback()
        c.execute(f"DROP SCHEMA {schema} CASCADE")
        c.commit()
        c.close()


def test_migrate_checks_only_the_current_schema(conn, schema):
    b = schema + "_b"          # fresh schema B in front of the migrated one on the search_path
    conn.execute(f"CREATE SCHEMA {b}")
    conn.execute(f"SET search_path TO {b}, {schema}, public")
    conn.commit()
    try:
        versions = lambda s: [r["version"] for r in conn.execute(f"SELECT version FROM {s}.schema_migrations ORDER BY 1")]
        a_before = versions(schema)
        assert migrate(conn) == a_before
        assert versions(b) == a_before and versions(schema) == a_before
        assert conn.execute(f"SELECT to_regclass('{b}.templates') AS t").fetchone()["t"]
    finally:
        conn.rollback()
        conn.execute(f"SET search_path TO {schema}, public")
        conn.execute(f"DROP SCHEMA {b} CASCADE")
        conn.commit()

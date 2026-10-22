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

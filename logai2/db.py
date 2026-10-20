"""Postgres access: connection, numbered .sql migrations, daily partitions, settings."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
PARTITIONED = ("service_hours", "hourly_counts", "alert_minutes")


class StaleRevision(Exception):
    """put_setting was given a revision that is no longer current."""

    def __init__(self, current: str | None):
        super().__init__(f"stale revision; current is {current}")
        self.current = current


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def migrate(conn: psycopg.Connection) -> list[int]:
    with conn.transaction():
        done = set()
        if conn.execute("SELECT to_regclass('schema_migrations') AS t").fetchone()["t"]:
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(path.name[:3])
        if version in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def ensure_partitions(conn: psycopg.Connection, first: date, last: date) -> None:
    with conn.transaction():
        day = first
        while day <= last:
            for table in PARTITIONED:
                conn.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})"
                ).format(sql.Identifier(f"{table}_{day:%Y%m%d}"), sql.Identifier(table),
                         sql.Literal(day), sql.Literal(day + timedelta(days=1))))
            day += timedelta(days=1)


def drop_partitions_before(conn: psycopg.Connection, table: str, day: date) -> list[str]:
    if table not in PARTITIONED:
        raise ValueError(f"not a partitioned table: {table}")
    with conn.transaction():
        rows = conn.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = to_regclass(%s)", (table,)).fetchall()
        old = sorted(r["relname"] for r in rows
                     if datetime.strptime(r["relname"].rsplit("_", 1)[1], "%Y%m%d").date() < day)
        for name in old:
            conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))
    return old


def get_setting(conn: psycopg.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def put_setting(conn: psycopg.Connection, key: str, value: dict, revision: str | None = None) -> str:
    """Upsert; with a revision, only if it still matches (optimistic locking for the web)."""
    new = uuid.uuid4().hex
    with conn.transaction():
        row = conn.execute("SELECT revision FROM settings WHERE key = %s FOR UPDATE", (key,)).fetchone()
        current = row["revision"] if row else None
        if revision is not None and revision != current:
            raise StaleRevision(current)
        conn.execute(
            "INSERT INTO settings (key, value, revision) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, revision = EXCLUDED.revision, "
            "updated_at = now()", (key, Jsonb(value), new))
    return new

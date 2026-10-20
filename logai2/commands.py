"""Commands table: the web enqueues, the engine takes and finishes them between flushes."""
from __future__ import annotations

from psycopg.types.json import Jsonb

KINDS = ("recompute_baseline", "reload_sources", "reload_links")


def enqueue(conn, kind: str, payload: dict | None = None) -> int:
    with conn.transaction():
        return conn.execute("INSERT INTO commands (kind, payload) VALUES (%s, %s) RETURNING id",
                            (kind, Jsonb(payload or {}))).fetchone()["id"]


def take(conn) -> list[dict]:
    """Undone commands, oldest first; the row locks last until the caller ends its transaction."""
    return conn.execute("SELECT id, kind, payload FROM commands WHERE done_at IS NULL "
                        "ORDER BY id FOR UPDATE SKIP LOCKED").fetchall()


def finish(conn, id: int, result: dict) -> None:
    with conn.transaction():
        conn.execute("UPDATE commands SET done_at = now(), result = %s WHERE id = %s", (Jsonb(result), id))

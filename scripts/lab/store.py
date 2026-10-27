"""Lab result storage. Unqualified table names: the results connection's search_path picks the schema."""
from __future__ import annotations

from psycopg.types.json import Jsonb


def start_run(conn, *, site, test, code_version, image_tag, data_schema, window_from, window_to, tune_to, seed,
              params) -> int:
    with conn.transaction():
        return conn.execute(
            "INSERT INTO lab_runs (site, test, code_version, image_tag, data_schema, window_from, window_to, tune_to, "
            "seed, params) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (site, test, code_version, image_tag, data_schema, window_from, window_to, tune_to, seed,
             Jsonb(params))).fetchone()["id"]


def finish_run(conn, run_id, status: str, kinds: dict | None = None, error: str | None = None) -> None:
    with conn.transaction():
        conn.execute("UPDATE lab_runs SET status = %s, finished_at = now(), kinds = %s, error = %s WHERE id = %s",
                     (status, None if kinds is None else Jsonb(kinds), error, run_id))


def put_result(conn, run_id, method, kind, scope, knob, status, metrics: dict, verdict=None, strength=None) -> None:
    with conn.transaction():
        conn.execute("INSERT INTO lab_results (run_id, method, kind, scope, knob, status, metrics, verdict, strength) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (run_id, method, kind, scope, knob, status, Jsonb(metrics), verdict, strength))


def clear_results(conn, run_id, method, scope) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM lab_results WHERE run_id = %s AND method = %s AND scope = %s", (run_id, method, scope))


def put_episodes(conn, run_id, method, service, key, episodes) -> None:
    """episodes: rows (start, end, peak, alerted)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("INSERT INTO lab_episodes VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(run_id, method, service, key, *e) for e in episodes])


def put_bands(conn, run_id, method, service, key, step, rows) -> None:
    """rows: (minute, count, expected, low, high, flag)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("INSERT INTO lab_bands VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [(run_id, method, service, key, step, *r) for r in rows])


def put_sweep(conn, run_id, method, kind, knob, part, metrics) -> None:
    with conn.transaction():
        conn.execute("INSERT INTO lab_sweeps VALUES (%s, %s, %s, %s, %s, %s)",
                     (run_id, method, kind, knob, part, Jsonb(metrics)))


def prune(conn, site: str, keep: int = 5) -> int:
    """Delete the site's runs older than its newest `keep` (children cascade); returns how many."""
    with conn.transaction():
        return conn.execute("DELETE FROM lab_runs WHERE site = %s AND id NOT IN "
                            "(SELECT id FROM lab_runs WHERE site = %s ORDER BY id DESC LIMIT %s)",
                            (site, site, keep)).rowcount

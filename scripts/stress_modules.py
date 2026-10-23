"""Stress test for template modules (spec section 6):
python scripts/stress_modules.py --dsn <dsn> [--services 50 --templates 2000 --modules 3 --active 0.3]
       [--days 1] [--alert-minutes 300000] [--scale tiny] [--max-bytes N] [--keep]

Creates a throwaway schema, migrates, loads with COPY, times the module flush, the API and the
retention drop, prints the EXPLAIN plans and a Markdown table, drops the schema unless --keep.
Exit 1 when a target is missed, 2 when the load would not fit on disk.

Default load: the 24 window hours at full density (what the API reads after partition pruning) plus one
older day of --old-rows for the retention drop. --days 7 loads the whole retention (~15M rows; needs
>= 5 GB free). The migration 007 seed runs from --alert-minutes rows in its own schema; its time is
reported per 1M rows. Before loading, the bytes to write (rows x 150 B incl. indexes, x2 for WAL) are
compared with half the free space on / (the Docker volume's disk) or --max-bytes; larger is refused.
"""
from __future__ import annotations

import argparse
import math
import random
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from logai2.config import load_config  # noqa: E402
from logai2.db import MIGRATIONS_DIR, drop_partitions_before, ensure_partitions, migrate  # noqa: E402
from logai2.owner.owner import copy_upsert  # noqa: E402
from logai2.timeutil import day_hour, minute_of  # noqa: E402
from logai2.web import modules_view  # noqa: E402
from logai2.web.app import create_app  # noqa: E402

# measure -> (target, unit); API in ms (p95 of --runs), flush in s (p95 of --flush-runs)
TARGETS = {
    "flush, live page": (1.0, "s"),
    "flush, catch-up page": (3.0, "s"),
    "GET /api/templates?service=s&size=100": (300, "ms"),
    "GET /api/templates?service=s&size=500": (800, "ms"),
    "GET /api/templates?service=s&module=m&size=100": (500, "ms"),
    "GET /api/modules?service=s": (300, "ms"),
    "retention: drop one day partition": (2.0, "s"),
    "migration 007 seed, per 1M alert_minutes": (60.0, "s"),
}
TINY = dict(services=2, templates=20, alert_minutes=1000, old_rows=100, flush=[200, 600])
ROW_BYTES = 150   # heap + indexes per row, measured ~140 on template_module_hours


def p95(xs: list[float]) -> float:
    return sorted(xs)[math.ceil(0.95 * len(xs)) - 1]   # nearest rank


def schema_dsn(dsn: str, schema: str) -> str:
    return f"{dsn}{'&' if '?' in dsn else '?'}options=-csearch_path%3D{schema}%2Cpublic"


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def module_names(i: int, k: int) -> list[str]:
    """Template i's k modules: 'main' in 90 % of templates (the large module), '' in the rest, then smaller ones."""
    return ["" if i % 10 == 0 else "main", *(f"m{j}.{i % (7 * j + 3)}" for j in range(1, k))]


def estimate(a) -> int:
    pairs = a.services * a.templates * a.modules
    main = a.services * a.templates + pairs + pairs * a.active * a.days * 24 + a.old_rows + 3 * max(a.flush)
    return int(max(main, 3 * a.alert_minutes) * ROW_BYTES * 2)   # the seed schema is dropped before the main one


def seed(a, now_min: int, pairs: list[tuple]) -> float:
    """Seconds of the migration 007 seed (both inserts) from --alert-minutes rows, in its own schema."""
    schema = f"stress_seed_{uuid.uuid4().hex[:8]}"
    with connect(a.dsn) as c:
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with connect(schema_dsn(a.dsn, schema)) as c:
            for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
                if int(path.name[:3]) < 7:
                    c.execute(path.read_text(encoding="utf-8"))
            today = day_hour(now_min)[0]
            for back in range(9):
                d = today - timedelta(days=back)
                c.execute(sql.SQL("CREATE TABLE {} PARTITION OF alert_minutes FOR VALUES FROM ({}) TO ({})").format(
                    sql.Identifier(f"alert_minutes_{d:%Y%m%d}"), sql.Literal(d), sql.Literal(d + timedelta(days=1))))
            with c.cursor().copy("COPY alert_minutes (service, key, module_code, day, minute, count) FROM STDIN") as cp:
                for r in range(a.alert_minutes):   # j-th minute of pair p: distinct per pair, within the last 7 days
                    p, j = r % len(pairs), r // len(pairs)
                    m = now_min - 60 - (p * 7919) % 9000 - j
                    cp.write_row((*pairs[p], day_hour(m)[0], datetime.fromtimestamp(m * 60, timezone.utc), 3))
            c.execute("ANALYZE alert_minutes")
            text = (MIGRATIONS_DIR / "007_template_modules.sql").read_text(encoding="utf-8")
            t0 = time.perf_counter()
            with c.transaction():
                c.execute(text)
            return time.perf_counter() - t0
    finally:
        with connect(a.dsn) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def load(c, a, now: float, buckets: list, pairs: list[tuple]) -> int:
    rng = random.Random(1)
    now_ts = datetime.fromtimestamp(now, timezone.utc)
    old = min(d for d, _ in buckets) - timedelta(days=1)
    ensure_partitions(c, old, max(d for d, _ in buckets))
    with c.cursor().copy("COPY templates (service, template_id, level, text, first_seen, last_seen) FROM STDIN") as cp:
        for s in range(a.services):
            for i in range(a.templates):
                cp.write_row((f"s{s:02d}", f"s{s:02d}:T{i:05d}", rng.choice(("ERROR", "WARN", "INFO")),
                              f"request <*> failed in step {i} with code <*>", now_ts - timedelta(days=30),
                              now_ts - timedelta(minutes=rng.randrange(7 * 1440))))
    with c.cursor().copy("COPY template_modules (service, template_id, module_code, first_seen, last_seen) "
                         "FROM STDIN") as cp:
        for p in pairs:
            cp.write_row((*p, now_ts - timedelta(days=30), now_ts - timedelta(minutes=rng.randrange(7 * 1440))))
    n = 0
    with c.cursor().copy("COPY template_module_hours (service, template_id, module_code, day, hour, count) "
                         "FROM STDIN") as cp:
        for d, h in buckets:
            for p in pairs:
                if rng.random() < a.active:
                    cp.write_row((*p, d, h, rng.randint(1, 500)))
                    n += 1
        for i in range(a.old_rows):
            cp.write_row((*pairs[i % len(pairs)], old, i // len(pairs) % 24, 1))
    c.execute("VACUUM ANALYZE templates, template_modules, template_module_hours")   # autovacuum's steady state
    c.execute("CHECKPOINT")   # recycle the load's WAL before the next phase
    return n


def flush_runs(c, a, now: float, buckets: list, pairs: list[tuple], n: int) -> float:
    """p95 seconds of the owner's two module upserts, n rows each, in one transaction (as Owner.flush)."""
    ts = datetime.fromtimestamp(now, timezone.utc)
    hours = [(*pairs[i % len(pairs)], *buckets[i // len(pairs) % len(buckets)], 7) for i in range(n)]
    mods = [(*p, ts, ts) for p in pairs[:n]]   # unique keys, as the owner's dict gives
    times = []
    for _ in range(a.flush_runs):
        t0 = time.perf_counter()
        with c.transaction(), c.cursor() as cur:
            copy_upsert(cur, "template_module_hours", hours)
            copy_upsert(cur, "template_modules", mods)
        times.append(time.perf_counter() - t0)
        c.execute("VACUUM template_module_hours, template_modules")   # bounds bloat to one run, untimed
    c.execute("CHECKPOINT")
    return p95(times)


def api_p95(client, url: str, runs: int) -> float:
    client.get(url)   # warm: first connection, plan caches
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = client.get(url)
        times.append(time.perf_counter() - t0)
        assert r.status_code == 200, (url, r.status_code, r.get_data(as_text=True)[:200])
    return p95(times) * 1000


def explains(c, service: str, module: str, now: float) -> list[tuple[str, str]]:
    days, bdays, bhours = modules_view.window_args(now)
    keys = [(r["service"], r["template_id"]) for r in c.execute(
        "SELECT service, template_id FROM templates WHERE service = %s ORDER BY last_seen DESC, template_id "
        "LIMIT 100", (service,))]
    w = modules_view.WINDOW_SQL
    queries = [
        ("window lines of 100 templates (modules_for)",
         "SELECT service, template_id, module_code, sum(count)::bigint AS lines FROM template_module_hours h "
         "WHERE (service, template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
         f"AND {w} GROUP BY 1, 2, 3", ([s for s, _ in keys], [t for _, t in keys], days, bdays, bhours)),
        ("module filter, window lines first (/api/templates items)",
         "SELECT t.service, t.template_id FROM templates t LEFT JOIN template_doc_links l ON l.service = t.service "
         "AND l.template_id = t.template_id LEFT JOIN alert_state a ON a.service = t.service AND a.key = t.template_id "
         "WHERE t.service = %s AND EXISTS (SELECT 1 FROM template_modules m WHERE m.service = t.service "
         "AND m.template_id = t.template_id AND m.module_code = %s) ORDER BY EXISTS (SELECT 1 FROM "
         "template_module_hours h WHERE h.service = t.service AND h.template_id = t.template_id AND h.module_code = %s "
         f"AND {w}) DESC, t.last_seen DESC, t.template_id LIMIT 100",
         (service, module, module, days, bdays, bhours)),
        ("/api/modules window aggregate",
         "SELECT m.module_code AS module, count(*) AS templates, COALESCE(sum(w.n), 0)::bigint AS lines_24h "
         "FROM template_modules m LEFT JOIN (SELECT template_id, module_code, sum(count) AS n "
         f"FROM template_module_hours h WHERE service = %s AND {w} GROUP BY 1, 2) w "
         "ON w.template_id = m.template_id AND w.module_code = m.module_code "
         "WHERE m.service = %s GROUP BY m.module_code ORDER BY templates DESC, module",
         (service, days, bdays, bhours, service)),
    ]
    return [(name, "\n".join(r["QUERY PLAN"] for r in c.execute("EXPLAIN (ANALYZE, BUFFERS) " + q, args)))
            for name, q, args in queries]


def measure(a, schema: str, now: float, pairs: list[tuple], results: dict) -> list[tuple[str, str]]:
    now_min = minute_of(now)
    buckets = [day_hour(now_min // 60 * 60 - 60 * back) for back in range(a.days * 24)]   # newest first
    with connect(schema_dsn(a.dsn, schema)) as c:
        migrate(c)
        t0 = time.perf_counter()
        rows = load(c, a, now, buckets, pairs)
        print(f"loaded {rows:,} template_module_hours rows ({a.days * 24} hours) + {a.old_rows:,} old, "
              f"{len(pairs):,} pairs in {time.perf_counter() - t0:.0f} s", flush=True)

        live, catch_up = a.flush
        results["flush, live page"] = (flush_runs(c, a, now, buckets, pairs, live), f"{live:,} pairs")
        results["flush, catch-up page"] = (flush_runs(c, a, now, buckets, pairs, catch_up), f"{catch_up:,} pairs")

        cfg = load_config(None)
        cfg.database.dsn = schema_dsn(a.dsn, schema)
        client = create_app(cfg).test_client()
        clock, modules_view.clock = modules_view.clock, lambda: now   # one window for the whole run
        try:
            for name, url in (("GET /api/templates?service=s&size=100", "/api/templates?service=s00&size=100"),
                              ("GET /api/templates?service=s&size=500", "/api/templates?service=s00&size=500"),
                              ("GET /api/templates?service=s&module=m&size=100",
                               "/api/templates?service=s00&module=main&size=100"),
                              ("GET /api/modules?service=s", "/api/modules?service=s00")):
                results[name] = (api_p95(client, url, a.runs), f"p95 of {a.runs}")
            plans = explains(c, "s00", "main", now)
        finally:
            modules_view.clock = clock

        parts = c.execute(
            "SELECT sum(pg_total_relation_size(i.inhrelid))::bigint AS b FROM pg_inherits i JOIN pg_class p "
            "ON p.oid = i.inhrelid WHERE i.inhparent = 'template_module_hours'::regclass "
            "AND p.relname >= %s", (f"template_module_hours_{min(d for d, _ in buckets):%Y%m%d}",)).fetchone()["b"]
        per_row = parts / rows
        full = per_row * len(pairs) * a.active * 168
        tm = c.execute("SELECT pg_total_relation_size('template_modules') AS b").fetchone()["b"]
        results["size of 7 days"] = (full / 2**20, f"{per_row:.0f} B/row x {len(pairs) * a.active * 168:,.0f} "
                                                   f"rows; template_modules {tm / 2**20:.0f} MB")

        t0 = time.perf_counter()
        dropped = drop_partitions_before(c, "template_module_hours", min(d for d, _ in buckets))
        results["retention: drop one day partition"] = (time.perf_counter() - t0, ", ".join(dropped))
    return plans


def public_is_migrated(dsn: str) -> bool:
    with connect(dsn) as c:
        return c.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()["t"] is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--scale", choices=("default", "tiny"), default="default")
    ap.add_argument("--services", type=int, default=50)
    ap.add_argument("--templates", type=int, default=2000)
    ap.add_argument("--modules", type=int, default=3)
    ap.add_argument("--active", type=float, default=0.3)
    ap.add_argument("--days", type=int, default=1, help="days of hours loaded; 1 = the 24 window hours")
    ap.add_argument("--old-rows", type=int, default=10_000)
    ap.add_argument("--alert-minutes", type=int, default=300_000)
    ap.add_argument("--flush", type=lambda s: [int(x) for x in s.split(",")], default=[20_000, 60_000])
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--flush-runs", type=int, default=10)
    ap.add_argument("--max-bytes", type=int)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    if a.scale == "tiny":
        vars(a).update(TINY)

    if public_is_migrated(a.dsn):   # search_path is <schema>,public: migrate() would skip and the load would hit public
        print("REFUSED: this database has a migrated public schema (real data); use an empty database", file=sys.stderr)
        return 3

    need, free = estimate(a), shutil.disk_usage("/").free
    limit = min(a.max_bytes, free // 2) if a.max_bytes else free // 2
    print(f"estimated write {need / 2**20:,.0f} MB, limit {limit / 2**20:,.0f} MB (free on / {free / 2**20:,.0f} MB)")
    if need > limit:
        print(f"REFUSED: the load would write ~{need / 2**20:,.0f} MB, over the {limit / 2**20:,.0f} MB limit "
              "(half the free space on /, or --max-bytes); lower --days/--services/--alert-minutes", file=sys.stderr)
        return 2

    now = time.time()
    pairs = [(f"s{s:02d}", f"s{s:02d}:T{i:05d}", m)
             for s in range(a.services) for i in range(a.templates) for m in module_names(i, a.modules)]
    results: dict[str, tuple[float, str]] = {}
    t = seed(a, minute_of(now), pairs)
    results["migration 007 seed, per 1M alert_minutes"] = (t * 1e6 / a.alert_minutes,
                                                           f"{t:.2f} s for {a.alert_minutes:,} rows")
    schema = f"stress_{uuid.uuid4().hex[:8]}"
    with connect(a.dsn) as c:
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        plans = measure(a, schema, now, pairs, results)
    finally:
        if a.keep:
            print(f"kept schema {schema}")
        else:
            with connect(a.dsn) as c:
                c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                c.execute("CHECKPOINT")
            print(f"dropped schema {schema}; free on / {shutil.disk_usage('/').free / 2**20:,.0f} MB "
                  f"(was {free / 2**20:,.0f} MB)")

    for name, plan in plans:
        print(f"\n### EXPLAIN (ANALYZE, BUFFERS): {name}\n\n```\n{plan}\n```")
    print("\n| Measure | Value | Unit | Target | OK | Notes |\n|---|---|---|---|---|---|")
    missed = []
    for name, (value, notes) in results.items():
        target, unit = TARGETS.get(name, (None, "MB"))
        if name.startswith("migration") and a.alert_minutes < 100_000:
            target = None   # fixed costs (DDL, the DO block) dominate; too few rows to extrapolate
        ok = target is None or value <= target
        if not ok:
            missed.append(name)
        print(f"| {name} | {value:.3f} | {unit} | {'-' if target is None else f'<= {target:g}'} | "
              f"{'yes' if ok else 'NO'} | {notes} |")
    if missed:
        print(f"\nMISSED: {', '.join(missed)}", file=sys.stderr)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

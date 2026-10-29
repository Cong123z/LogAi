"""Memory probe: rebuild a vas_066-sized service in a throwaway schema and measure what the engine costs.
python scripts/mem_probe.py --dsn DSN --templates N [--minutes M] [--keep]

Seeds one service `vas` (N templates, N x 48 baseline rows, a Drain3 blob of N clusters, 5 committed weekdays) in
schema probe_<8 hex>, then a CHILD process (clean RSS) builds Owner, times Miners.dirty() after one new cluster, feeds
M minutes of one count per template per minute with the watermark held, moves it past all M in one batch and flushes
(the B25 shape). The last stdout line is one JSON object:
templates, baseline_rows, blob_bytes, load_peak_mib, load_steady_mib, save_state_ms, flush_peak_mib.
Detection is off (no detection_enabled setting), so the flush numbers are a floor.
"""
from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.conninfo import make_conninfo  # noqa: E402

from logai2.config import load_config  # noqa: E402
from logai2.db import connect, ensure_partitions, migrate  # noqa: E402
from logai2.metrics import rss_mib, trim  # noqa: E402
from logai2.owner.miners import Miners  # noqa: E402
from logai2.owner.owner import Owner  # noqa: E402

START = int(datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc).timestamp()) // 60   # a Tuesday, 10:00 local
CHUNK = 200_000


def _w(n: int, k: int) -> str:   # k letters (no digits: Drain3 sends digit tokens down one wildcard branch)
    return "".join(chr(97 + n // 26 ** i % 26) for i in range(k))


def line(i: int) -> str:
    """Distinct lines that spread over the Drain3 tree (<= ~14 clusters per leaf) and never merge."""
    return f"{_w(i % 100, 2)} {_w(i // 100 % 100, 2)} {_w(i // 10_000, 3)} q{_w(i, 4)} r{_w(i, 4)} s{_w(i, 4)} t{_w(i, 4)} u{_w(i, 4)}"


def with_schema(dsn: str, schema: str) -> str:
    return make_conninfo(dsn, options=f"-csearch_path={schema},public")


def seed(conn, n: int, cfg) -> int:
    """Seed the tables; returns the Drain3 blob size."""
    miners = Miners(cfg.drain3, {})
    ids, texts = [], []
    for i in range(n):
        tid, text, _ = miners.match("vas", line(i))
        ids.append(tid)
        texts.append(text)
    blob = miners.dirty()["vas"]
    seen = datetime.fromtimestamp((START - 1440) * 60, timezone.utc)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO services (service, partition) VALUES ('vas', 0)")
        cur.execute("INSERT INTO drain3_state (service, blob) VALUES ('vas', %s)", (blob,))
        with cur.copy("COPY templates (service, template_id, level, text, first_seen, last_seen) FROM STDIN") as cp:
            for tid, text in zip(ids, texts):
                cp.write_row(("vas", tid, "INFO", text, seen, seen))
        with cur.copy("COPY baseline (service, key, daytype, hour, mean, std, days) FROM STDIN") as cp:
            for tid in ids:
                for dt in ("weekday", "weekend"):
                    for h in range(24):
                        cp.write_row(("vas", tid, dt, h, 5.0, 1.0, 5))
        for d in range(5):
            cur.execute("INSERT INTO day_status (service, day, committed, daytype) VALUES ('vas', %s, true, 'weekday')",
                        (date(2026, 10, 12) + timedelta(days=d),))
    return len(blob)


def child(dsn: str, n: int, minutes: int) -> dict:
    conn = connect(dsn)
    cfg = load_config(None)
    owner = Owner(conn, cfg, START)
    trim()
    out = {"load_peak_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, "load_steady_mib": rss_mib()}
    owner.miners.match("vas", line(n))   # one new cluster dirties the service
    t = time.perf_counter()
    blobs = owner.miners.dirty()
    out["save_state_ms"] = (time.perf_counter() - t) * 1000
    owner._blobs.update(blobs)   # the flush writes it, as a real one would
    ids = [f"vas:T{i:05d}" for i in range(1, n + 1)]
    held = START * 60_000
    for m in range(minutes):
        owner.handle({"counts": [{"service": "vas", "template_id": tid, "text": None, "level": "INFO", "module": "",
                                  "minute": START + m, "n": 1, "sample": "x"} for tid in ids],
                      "watermark_ms": held, "events": n, "positions": {}, "sources": []})
    owner.handle({"counts": [], "watermark_ms": (START + minutes) * 60_000, "events": 0, "positions": {}, "sources": []})
    owner.flush({"a": ((START + minutes) * 60_000, held)}, {})
    out["flush_peak_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--templates", type=int, required=True)
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.child:
        print(json.dumps(child(a.dsn, a.templates, a.minutes)))
        return
    schema = f"probe_{uuid.uuid4().hex[:8]}"
    conn = connect(a.dsn)
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path TO {schema}, public")
    conn.commit()
    try:
        migrate(conn)
        ensure_partitions(conn, date(2026, 10, 19), date(2026, 10, 23))
        t = time.perf_counter()
        blob_bytes = seed(conn, a.templates, load_config(None))
        print(f"seeded in {time.perf_counter() - t:.0f} s into {schema}", file=sys.stderr)
        r = subprocess.run([sys.executable, __file__, "--child", "--dsn", with_schema(a.dsn, schema),
                            "--templates", str(a.templates), "--minutes", str(a.minutes)],
                           capture_output=True, text=True)
        sys.stderr.write(r.stderr[-2000:])
        if r.returncode:   # e.g. -9 when the OOM killer took the child: report it, still drop the schema
            print(f"child exited {r.returncode}", file=sys.stderr)
        res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"child_exit": r.returncode}
        print(json.dumps({"templates": a.templates, "baseline_rows": a.templates * 48, "blob_bytes": blob_bytes, **res}))
        sys.exit(r.returncode and 1)
    finally:
        conn.rollback()
        if not a.keep:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()
        conn.close()


if __name__ == "__main__":
    main()

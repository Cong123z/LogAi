"""Estimate check (spec 5): coverage estimated from hourly moments vs counted minute by minute.

    .venv/bin/python scripts/bands_estimate_check.py --schema lab_s_w28 [--dsn DSN] [--test-days 3] [--today YYYY-MM-DD]

Runs `tune` on the schema (writes settings.bands_tune there), then walks the same test hours and counts the
minutes of lab_minute_counts.counts outside each band. Prints markdown tables and the gate verdict."""
import argparse
import math
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")
from logai2.bands import KGRID, METHODS, band, expected_outside, pick_k, tune, tune_hours  # noqa: E402
from logai2.config import load_config  # noqa: E402
from logai2.db import connect  # noqa: E402

GAP_PP = 0.2


def coverage_exact(counts: list[int], b, floor, low_side: bool) -> int:
    """Minutes of one hour outside band b: count >= t above, count <= ceil(low) - 1 below (*total only)."""
    t = max(math.floor(b.high) + 1, floor or 0)
    lo = math.ceil(b.low) - 1
    return sum(c >= t or (low_side and c <= lo) for c in counts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", required=True)
    ap.add_argument("--dsn", default="postgresql://logai:logai@127.0.0.1:5433/logai")
    ap.add_argument("--test-days", type=int, default=3)
    ap.add_argument("--today")
    a = ap.parse_args()
    conn = connect(f"{a.dsn}{'&' if '?' in a.dsn else '?'}options=-csearch_path%3D{a.schema}")
    cfg = load_config(None)
    today = date.fromisoformat(a.today) if a.today else conn.execute(
        "SELECT max(day) + 1 AS d FROM day_status WHERE committed").fetchone()["d"]
    first = today - timedelta(days=a.test_days)
    counts = {(r["service"], r["key"], r["day"], r["hour"]): r["counts"] for r in conn.execute(
        "SELECT service, key, day, hour, counts FROM lab_minute_counts WHERE day BETWEEN %s AND %s",
        (first, today - timedelta(days=1)))}

    t0 = time.monotonic()
    stored = tune(conn, cfg, today, a.test_days)
    t_tune = time.monotonic() - t0

    acc = {}           # kind -> method -> k -> [est outside, exact outside, minutes]
    for th in tune_hours(conn, cfg, today, a.test_days):
        cts = counts.get((th.service, th.key, th.day, th.hour)) or [0] * 60
        for m in METHODS:
            for k in KGRID:
                b = band(m, th.rows[m], k)
                if b is None:
                    break
                c = acc.setdefault(th.kind, {}).setdefault(m, {}).setdefault(k, [0.0, 0, 0])
                c[0] += expected_outside(th.act.m, th.act.v, th.act.n, b, th.floor, th.low_side)
                c[1] += coverage_exact(cts, b, th.floor, th.low_side)
                c[2] += th.act.n

    fails = []
    print(f"schema {a.schema}, today {today}, test days {first}..{today - timedelta(days=1)}, tune {t_tune:.1f} s\n")
    print("| kind | method | max gap, all k (pp) | at k | max gap, k >= best (pp) | best k est | best k exact | stored k | verdict |")
    print("|---|---|---|---|---|---|---|---|---|")
    for kind, ms in acc.items():
        for m, cells in ms.items():
            est = {k: 1 - c[0] / c[2] for k, c in cells.items()}
            ex = {k: 1 - c[1] / c[2] for k, c in cells.items()}
            gk = max(cells, key=lambda k: abs(est[k] - ex[k]))
            gap = abs(est[gk] - ex[gk]) * 100
            ke, kx = pick_k(est)[0], pick_k(ex)[0]
            step = abs(KGRID.index(ke) - KGRID.index(kx))
            gb = max(abs(est[k] - ex[k]) for k in cells if k >= (kx or 0)) * 100     # gate: best k and above
            ok = gb <= GAP_PP and step <= 1
            note = "ok" if ok and step == 0 else "ok, best k one step apart" if ok else "FAIL"
            if not ok:
                fails.append((kind, m))
            print(f"| {kind} | {m} | {gap:.3f} | {gk} | {gb:.3f} | {ke} | {kx} | {stored['kinds'][kind]['methods'][m]['k']} | {note} |")
    print("\nPer k (estimated / exact coverage %, gap pp):")
    for kind, ms in acc.items():
        print(f"\n{kind}\n\n| method | " + " | ".join(f"k {k}" for k in KGRID) + " |\n|---|" + "---|" * len(KGRID))
        for m, cells in ms.items():
            print(f"| {m} | " + " | ".join(
                f"{100 * (1 - cells[k][0] / cells[k][2]):.2f} / {100 * (1 - cells[k][1] / cells[k][2]):.2f}"
                if k in cells else "-" for k in KGRID) + " |")
    print("\nGATE:", "PASS" if not fails else f"FAIL {fails}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

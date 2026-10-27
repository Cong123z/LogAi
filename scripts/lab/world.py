"""Build the synthetic world file from a source schema (read-only): per service, per template, the mean and
variance of the per-minute count for each local hour, from hourly_counts and service_hours."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from logai2.config import load_config
from logai2.db import connect
from logai2.links import ALERTING_SOURCES


def build_world(conn) -> dict:
    n_min: dict[tuple[str, int], int] = {}
    for r in conn.execute("SELECT service, hour, sum(n_minutes) AS n FROM service_hours GROUP BY 1, 2"):
        n_min[r["service"], r["hour"]] = int(r["n"])
    total: dict[tuple[str, int], int] = {}
    for r in conn.execute("SELECT service, hour, sum(sum) AS s FROM hourly_counts WHERE key = '*total' GROUP BY 1, 2"):
        total[r["service"], r["hour"]] = int(r["s"])
    # active hours only: observed-but-silent hours (engine up, replay not running) would teach a rate of zero
    active = {svc: [h for h in range(24) if n_min.get((svc, h), 0) >= 30 and total.get((svc, h), 0) > 0]
              for svc in {s for s, _ in n_min}}
    for svc, hs in sorted(active.items()):
        if len(hs) < 8:
            print(f"warning: service {svc} has only {len(hs)} active hours {hs}; rebuild after a full replay",
                  file=sys.stderr)
    sums: dict[tuple[str, str], dict[int, tuple[int, int]]] = {}
    for r in conn.execute("SELECT h.service, h.key, h.hour, sum(h.sum) AS s, sum(h.sumsq) AS q FROM hourly_counts h "
                          "JOIN templates t ON t.service = h.service AND t.template_id = h.key GROUP BY 1, 2, 3"):
        sums.setdefault((r["service"], r["key"]), {})[r["hour"]] = (int(r["s"]), int(r["q"]))
    docs = {(r["service"], r["template_id"]): r["doc_id"] for r in conn.execute(
        "SELECT service, template_id, doc_id FROM template_doc_links WHERE doc_id IS NOT NULL AND source = ANY(%s)",
        (list(ALERTING_SOURCES),))}
    world: dict[str, list[dict]] = {}
    for t in conn.execute("SELECT service, template_id, level, text FROM templates ORDER BY 1, 2"):
        svc, tid = t["service"], t["template_id"]
        seen = active.get(svc, [])
        per = {h: v for h, v in sums.get((svc, tid), {}).items() if h in seen}
        if not seen or not any(s for s, _ in per.values()):
            continue
        stat = {}
        for h in seen:
            s, q = per.get(h, (0, 0))
            n = n_min[svc, h]
            stat[h] = (s / n, q / n - (s / n) ** 2)
        n_all = sum(n_min[svc, h] for h in seen)
        s_all = sum(per.get(h, (0, 0))[0] for h in seen)
        q_all = sum(per.get(h, (0, 0))[1] for h in seen)
        overall = (s_all / n_all, q_all / n_all - (s_all / n_all) ** 2)
        mean, var, mirrored = [], [], []
        for h in range(24):
            m, v = stat.get(h) or stat.get(23 - h) or overall
            mean.append(m), var.append(v), mirrored.append(h not in stat)
        world.setdefault(svc, []).append({"template_id": tid, "text": t["text"], "level": t["level"],
                                          "doc_id": docs.get((svc, tid)), "mean": mean, "var": var,
                                          "mirrored": mirrored})
    return {"services": world, "active_hours": {s: active[s] for s in world}}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--schema", default="public")
    ap.add_argument("--out", default="lab/world.json")
    a = ap.parse_args(argv)
    conn = connect(a.dsn or load_config(None).database.dsn)
    conn.execute("SELECT set_config('search_path', %s, false)", (f"{a.schema}, public",))
    world = build_world(conn)
    conn.rollback()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(world))
    print(f"{a.out}: " + ", ".join(f"{s} {len(t)} templates" for s, t in world["services"].items()))


if __name__ == "__main__":
    main()

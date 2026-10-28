"""Test A injections vs the count floor: for each upward injection, can its peak minute reach the floor of its key?
usage: PYTHONPATH=<worktree> python reach.py <data schema> <window days> [seed]"""
import sys
from collections import defaultdict
from datetime import timedelta
import psycopg
from psycopg.rows import dict_row
from logai2.config import load_config
from scripts.lab import inject
from scripts.lab.kinds import assign
from scripts.lab.load import load_service, services
from scripts.lab.methods import floor_for
from scripts.lab.run import HISTORY_DAYS, today
schema, n = sys.argv[1], int(sys.argv[2]); seed = int(sys.argv[3]) if len(sys.argv) > 3 else 1
cfg = load_config(None)
c = psycopg.connect("postgresql://logai:logai@127.0.0.1:5433/logai", row_factory=dict_row)
c.execute(f"set search_path={schema}, public")
end = today() - timedelta(1); d0 = end - timedelta(n - 1)
days = [d0 + timedelta(i) for i in range(n)]
res = defaultdict(lambda: [0, 0])
for svc in services(c, d0, end):
    sd = load_service(c, svc, d0, end, HISTORY_DAYS)
    kinds = assign(sd, days[:n // 2])
    exp = inject.Expect(sd)
    for p in range(inject.PASSES):
        for i in inject.plan(sd, kinds, seed + p, exp, cfg):
            if i.type not in ("spike", "ramp", "level_shift"): continue
            e = exp.window(i.carrier, i.start, i.end - i.start)
            peak = max(a + x for a, x in zip(i.add, e)) if i.add else 0          # the carrier's count at its busiest minute
            kpeak = peak if i.key == i.carrier else None
            fl = floor_for(i.key, sd.level(i.key), cfg)
            r = res[(i.kind, i.type)]; r[0] += 1; r[1] += (peak >= fl)
for (k, t), (tot, ok) in sorted(res.items()):
    print(f"{k} {t:12s} injections={tot} peak_count_reaches_floor={ok} ({ok/tot:.0%})")

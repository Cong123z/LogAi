"""Parity of lab sigma k=3 vs the engine's alert_changes on an engine-mode world (rule of tests/lab/test_lab_parity.py).
usage: PYTHONPATH=<worktree> python parity.py <schema> <engine days>"""
import sys
from datetime import timedelta
import psycopg
from psycopg.rows import dict_row
from logai2.config import load_config
from scripts.lab.data import minute_at
from scripts.lab.episodes import Tracker
from scripts.lab.load import load_service
from scripts.lab.methods.sigma import Sigma
from scripts.stress_modules import schema_dsn
schema, E = sys.argv[1], int(sys.argv[2])
D = "postgresql://logai:logai@127.0.0.1:5433/logai"
cfg = load_config(None)
with psycopg.connect(schema_dsn(D, schema), row_factory=dict_row) as c:
    ch = c.execute("select service, key, extract(epoch from at)::float8 at, to_state from alert_changes").fetchall()
    svcs = [r["service"] for r in c.execute("select distinct service from lab_minute_counts")]
    last = c.execute("select max(day) d from lab_minute_counts").fetchone()["d"]
    days = [last - timedelta(E - 1 - i) for i in range(E)]
    print("engine days", days[0], days[-1], "alert_changes", len(ch), "services", svcs)
    tot = dict(lab=0, eng=0, matched=0, outside=0, inwin=0)
    for svc in svcs:
        sd = load_service(c, svc, days[0], days[-1], 35)
        m = Sigma(); out = set()
        trackers = {k: Tracker(cfg.alert) for k in sd.keys() if k != "*minutes"}
        prev = m.fit(sd, days[0], cfg)
        for d in days[1:]:
            model = m.fit(sd, d, cfg)
            for key, tr in trackers.items():
                o = m.day(model, prev, sd, key, d, (3.0,), cfg)[3.0]
                for slot in range(1440):
                    if sd.observed[d][slot]:
                        tr.step(minute_at(d, slot), bool(o.high_flag[slot]), o.score[slot])
            prev = model
        for key, tr in trackers.items():
            out |= {(key, mn, st) for mn, st in tr.changes}
        first = minute_at(days[1], 0)
        eng = {(r["key"], int(r["at"]) // 60 - 1, r["to_state"]) for r in ch if r["service"] == svc and int(r["at"]) // 60 - 1 >= first}
        window = [(minute_at(d, 120) - 2, minute_at(d, 120) + 10) for d in days[1:]]
        inside = lambda mm: any(lo <= mm <= hi for lo, hi in window)
        diff = out ^ eng
        outside = sorted(x for x in diff if not inside(x[1])); inw = [x for x in diff if inside(x[1])]
        print(svc, f"lab={len(out)} engine={len(eng)} matched={len(out & eng)} mismatch_outside_windows={len(outside)} mismatch_in_windows={len(inw)} keys={len(trackers)}")
        for x in outside[:10]: print("   outside:", x, "lab" if x in out else "engine")
        tot["lab"] += len(out); tot["eng"] += len(eng); tot["matched"] += len(out & eng); tot["outside"] += len(outside); tot["inwin"] += len(inw)
    print("TOTAL", tot)

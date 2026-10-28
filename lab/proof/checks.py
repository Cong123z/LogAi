"""Interval score (Winkler/n) per method and day group (weekday / weekend / holiday), ratio to sigma, at the chosen knob.
usage: PYTHONPATH=<worktree> python checks.py <data schema> [results schema]"""
import sys, yaml, psycopg
from collections import defaultdict
from datetime import timedelta
from psycopg.rows import dict_row
schema = sys.argv[1]; res = sys.argv[2] if len(sys.argv) > 2 else schema
c = psycopg.connect("postgresql://logai:logai@127.0.0.1:5433/logai", row_factory=dict_row)
c.execute(f"set search_path={res}")
rb = c.execute("select * from lab_runs where test='B' and status='ok' order by id desc limit 1").fetchone()
print("B run", rb["id"], rb["window_from"], rb["window_to"])
knob = {(r["method"], r["kind"]): r["knob"] for r in c.execute("select method,kind,knob from lab_results where run_id=%s and scope='all' and status='ok'", (rb["id"],))}
sc = yaml.safe_load(open(f"/home/cong/Documents/logai2-task-bk17/lab/scenarios/{schema.replace('lab_s_','')}.yaml"))
nwin = (rb["window_to"] - rb["window_from"]).days + 1
w0 = rb["window_from"] - timedelta(int(sc["days"]) - nwin)
h = sc["holiday"]; hol = {(w0 + timedelta(i)).isoformat() for i in range(h["start_day"], h["start_day"] + h["length"])} if h["length"] else set()
days = [rb["window_from"] + timedelta(i) for i in range(nwin)]
wk = {d.isoformat() for d in days if d.weekday() >= 5} - hol
print("holiday days in window", sorted(hol & {d.isoformat() for d in days}), "weekend days", len(wk))
agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
for r in c.execute("select method,kind,metrics from lab_results where run_id=%s and scope<>'all' and status='ok'", (rb["id"],)):
    k = knob.get((r["method"], r["kind"]))
    kd = next((v for kk, v in r["metrics"].get("days", {}).items() if k is not None and abs(float(kk) - k) < 1e-12), None)
    if not kd: continue
    for day, m in kd.items():
        g = "hol" if day in hol else ("wkend" if day in wk else "wkday")
        for gg in (g, "all"):
            a = agg[(r["method"], r["kind"])][gg]; a[0] += m["winkler"]; a[1] += m["n"]; a[2] += m["covered"]
for kind in ("M1", "M2", "M3", "M4", "M5"):
    sg = agg.get(("sigma", kind)); print("KIND", kind)
    for (m, kd), gs in sorted(agg.items()):
        if kd != kind: continue
        out = []
        for g in ("wkday", "wkend", "hol", "all"):
            if gs[g][1] and sg and sg[g][1]:
                out.append(f"{g}: is={gs[g][0]/gs[g][1]:.2f} r={(gs[g][0]/gs[g][1])/(sg[g][0]/sg[g][1]):.2f} cov={gs[g][2]/gs[g][1]:.4f}")
        print(f"  {m:9s} knob={knob.get((m,kind))} " + " | ".join(out))

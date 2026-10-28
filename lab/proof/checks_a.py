import sys, psycopg
from psycopg.rows import dict_row
schema = sys.argv[1]
c = psycopg.connect("postgresql://logai:logai@127.0.0.1:5433/logai", row_factory=dict_row)
c.execute(f"set search_path={schema}")
ra = c.execute("select * from lab_runs where test='A' and status='ok' order by id desc limit 1").fetchone()
print("A run", ra["id"], ra["window_from"], ra["window_to"])
rows = c.execute("select method,kind,knob,status,metrics from lab_results where run_id=%s and scope='all' order by kind, method", (ra["id"],)).fetchall()
f = lambda x: " - " if x is None else f"{x:.2f}"
for r in rows:
    if r["status"] != "ok": print(f'{r["kind"]} {r["method"]:9s} {r["status"]}'); continue
    m = r["metrics"]
    bt = " ".join(f"{t}:{f(v.get('medium'))}" for t, v in m["by_type"].items())
    print(f'{r["kind"]} {r["method"]:9s} knob={r["knob"]:<7g} rate={m["alert_rate"] if m["alert_rate"] is None else round(m["alert_rate"],2)} med_recall={f(m["recall"]["medium"])} delay_med/p90={m["delay"]["median"]}/{m["delay"]["p90"]} n={m["n_injections"]} | {bt}')

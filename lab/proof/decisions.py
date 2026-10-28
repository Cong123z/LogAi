import sys, psycopg
from psycopg.rows import dict_row
c = psycopg.connect("postgresql://logai:logai@127.0.0.1:5433/logai", row_factory=dict_row)
c.execute(f"set search_path={sys.argv[1]}")
rb = c.execute("select id from lab_runs where test='B' and status='ok' order by id desc limit 1").fetchone()["id"]
for r in c.execute("select kind, verdict, strength, metrics from lab_results where method='_decision' and run_id=%s order by kind", (rb,)):
    m = r["metrics"]; print(r["kind"], "|", r["verdict"], "|", r["strength"], "| leader", m.get("leader"), "| tied", m.get("tied"), "|", m.get("text"))
print("scoreboard (scope all)")
for r in c.execute("select method, kind, knob, status, verdict, metrics from lab_results where method<>'_decision' and scope='all' and run_id=%s order by kind, method", (rb,)):
    m = r["metrics"]
    if r["status"] != "ok": print(r["kind"], r["method"], r["status"]); continue
    g = m.get("gates", {}); fl = [k for k, v in g.items() if v == "fail"]
    ci = m.get("ci90", {}).get("ratio")
    print(r["kind"], f'{r["method"]:9s}', "knob", r["knob"], "ratio", None if m.get("ratio_vs_sigma") is None else round(m["ratio_vs_sigma"], 3), "ci", None if not ci else [round(x, 3) for x in ci], "cov", None if m.get("coverage") is None else round(m["coverage"], 4), "IS", None if m.get("interval_score") is None else round(m["interval_score"], 2), "rate", m.get("alert_rate") if m.get("alert_rate") is None else round(m["alert_rate"], 2), "failed", fl, r["verdict"])

"""Re-run test_a.finish on the latest ok Test A run of a schema (replaces its scope='all' rows; no walking)."""
import sys, psycopg
from psycopg.rows import dict_row
from scripts.lab import test_a
c = psycopg.connect("postgresql://logai:logai@127.0.0.1:5433/logai", row_factory=dict_row, autocommit=False)
c.execute(f"set search_path={sys.argv[1]}"); c.commit()
rid = c.execute("select id from lab_runs where test='A' and status='ok' order by id desc limit 1").fetchone()["id"]
c.execute("delete from lab_results where run_id=%s and scope='all'", (rid,)); c.commit()
test_a.finish(c, rid); c.commit()
print(sys.argv[1], "run", rid, c.execute("select count(*) n from lab_results where run_id=%s and scope='all'", (rid,)).fetchone()["n"], "all rows")

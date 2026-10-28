"""Load an incident_test.py run directory as `test-c` lab_incidents:
python -m scripts.lab.load_incidents reports/incident-YYYYMMDD-HHMM [--dsn ...] [--schema public]

One row per target of every burst/spread phase. The key is the template id re-resolved against the current `templates`
(exact text of meta.targets[label], else the inject pattern as a regex); none or several matches -> key NULL and a warning.
Re-loading a run directory replaces its rows.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import psycopg.sql as sql
from psycopg.types.json import Jsonb

from logai2.config import load_config
from logai2.db import connect


def _resolve(conn, service, text, pattern):
    """(template_id | None, why not)."""
    tpls = conn.execute("SELECT template_id, text FROM templates WHERE service = %s", (service,)).fetchall()
    for found, how in (([t for t in tpls if t["text"] == text], "exact text"),
                       ([t for t in tpls if pattern and re.search(pattern, t["text"])], f"pattern {pattern!r}")):
        if len(found) == 1:
            return found[0]["template_id"], None
        if len(found) > 1:
            return None, f"{how} matches {len(found)} templates"
    return None, "no template matches"


def load(conn, run_dir, schema="public") -> tuple[int, list[str]]:
    """Returns (rows written, warnings). The caller's connection must already point at the data schema."""
    run_dir = Path(run_dir).resolve()
    meta = json.loads((run_dir / "meta.json").read_text())
    events = [json.loads(line) for line in (run_dir / "phases.jsonl").read_text().splitlines() if line.strip()]
    injects = {e["label"]: e for e in events if e["event"] == "inject"}
    warns, rows = [], []
    for ph in (e for e in events if e["event"] == "phase" and e["kind"] in ("burst", "spread")):
        for label in ph["targets"]:
            inj = injects.get(f"{ph['name']}-{label}")
            if inj is None or inj["status"] != 200:
                warns.append(f"{ph['name']}-{label}: " + ("no inject event" if inj is None else f"inject status {inj['status']}") + ", skipped")
                continue
            t = meta["targets"][label]
            key, why = _resolve(conn, t["service"], t["text"], inj.get("pattern"))
            if why:
                warns.append(f"{ph['name']}-{label}: {why}, key NULL")
            rows.append((schema, t["service"], key, ph["kind"], inj["per_minute"], int(ph["start"]) // 60,
                         int(ph["end"]) // 60, Jsonb({"run_dir": run_dir.name, "label": label, "phase": ph["name"]})))
    with conn.transaction():
        conn.execute("DELETE FROM lab_incidents WHERE source = 'test-c' AND data_schema = %s AND meta->>'run_dir' = %s",
                     (schema, run_dir.name))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO lab_incidents (source, data_schema, service, key, type, size, start_minute, "
                            "end_minute, meta) VALUES ('test-c', %s, %s, %s, %s, %s, %s, %s, %s)", rows)
    conn.commit()           # the reads above opened a transaction: the block above was only a savepoint
    return len(rows), warns


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="scripts.lab.load_incidents")
    p.add_argument("run_dir")
    p.add_argument("--dsn")
    p.add_argument("--schema", default="public")
    a = p.parse_args(argv)
    conn = connect(a.dsn or load_config(None).database.dsn)
    try:
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(a.schema)))
        conn.commit()
        n, warns = load(conn, a.run_dir, a.schema)
    finally:
        conn.close()
    for w in warns:
        print(f"warning: {w}", file=sys.stderr)
    print(f"{n} incidents loaded from {Path(a.run_dir).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

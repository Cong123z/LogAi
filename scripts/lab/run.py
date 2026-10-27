"""Lab runner: python -m scripts.lab.run --site laptop --test B --methods core --days 28

Walk-forward every selected method over the recorded per-minute counts, one service at a time, and store the results.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg.sql as sql

import scripts.lab.methods as methods
from logai2.config import load_config
from logai2.db import connect
from logai2.timeutil import TZ
from scripts.lab import store
from scripts.lab.kinds import assign
from scripts.lab.load import load_service, services
from scripts.lab.methods import Unavailable


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="scripts.lab.run")
    p.add_argument("--site", choices=("laptop", "company"), required=True)
    p.add_argument("--test", choices=("A", "B", "C"), required=True)
    p.add_argument("--methods", required=True, help="core | heavy | all | comma list")
    p.add_argument("--days", type=int)
    p.add_argument("--from", dest="date_from", type=date.fromisoformat)
    p.add_argument("--to", dest="date_to", type=date.fromisoformat)
    p.add_argument("--schema", help="data schema (default public)")
    p.add_argument("--results-schema", help="results schema (default public)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--check", action="store_true")
    p.add_argument("--dsn")
    a = p.parse_args(argv)
    if (a.days is None) == (a.date_from is None or a.date_to is None):
        p.error("give --days N or both --from and --to")
    return a


def today() -> date:
    return datetime.now(TZ).date()


def code_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, capture_output=True,
                             text=True, check=True).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.CalledProcessError):
        pass
    return os.environ.get("LOGAI2_CODE_VERSION") or "unknown"


def default_collect(conn, run_id, service, method, outputs) -> None:
    """Stub: one result row per kind of the method (bk05/bk07 replace it with scoring). outputs: see run()."""
    spec = outputs["spec"]
    for kind in sorted(spec.kinds):
        keys = [k for k, kd in outputs["kinds"].items() if kd == kind]
        done = [k for k in keys if k in outputs["out"]]
        store.put_result(conn, run_id, method, kind, service, None, "ok" if outputs["out"] else "skipped",
                         {"n_keys": len(keys), "n_minutes": sum(1440 * len(outputs["out"][k]) for k in done)})


def run(args, cfg, data_conn, results_conn, collect=default_collect) -> int:
    """outputs given to collect(results_conn, run_id, service, method, outputs):
    {"spec": Spec, "kinds": {key: kind}, "out": {key: {day: {knob: DayOut}}}} (days a fit refused are absent)."""
    names = methods.select(args.methods)
    if args.days:
        end = today() - timedelta(1)
        d0, d1 = end - timedelta(args.days - 1), end
    else:
        d0, d1 = args.date_from, args.date_to
    days = [d0 + timedelta(n) for n in range((d1 - d0).days + 1)]
    by_days = len(days) >= 14
    tune_days = days[:len(days) // 2] if by_days else days
    instances = {}
    for n in names:
        try:
            instances[n] = methods.METHODS[n]()
        except Unavailable as e:
            print(f"{n}: unavailable ({e})", file=sys.stderr)
    params = {"methods": list(instances), "unavailable": [n for n in names if n not in instances],
              "knobs": {n: list(m.spec.knobs) for n, m in instances.items()},
              "split": "days" if by_days else "keys", "scenario": None}
    run_id = store.start_run(results_conn, site=args.site, test=args.test, code_version=code_version(),
                             image_tag=os.environ.get("LOGAI2_IMAGE_TAG"), data_schema=args.schema or "public",
                             window_from=d0, window_to=d1, tune_to=tune_days[-1], seed=args.seed, params=params)
    try:
        counts: dict[str, int] = {}
        digest = hashlib.sha1()
        for service in services(data_conn):
            sd = load_service(data_conn, service, d0, d1, cfg.retention.counts_days)
            kinds = assign(sd, tune_days)       # ponytail: M7 (*mix) is not walked yet; bk05+ add it
            for key, kind in sorted(kinds.items()):
                counts[kind] = counts.get(kind, 0) + 1
                digest.update(f"{service}\0{key}\0{kind}\n".encode())
            for name, m in instances.items():
                try:
                    out: dict = {}
                    prev = None
                    for d in days:
                        model = m.fit(sd, d, cfg)
                        if model is not None:
                            for key, kind in kinds.items():
                                if kind in m.spec.kinds:
                                    out.setdefault(key, {})[d] = m.day(model, prev, sd, key, d, m.spec.knobs, cfg)
                        prev = model
                except Exception as exc:
                    for kind in sorted(m.spec.kinds):
                        store.put_result(results_conn, run_id, name, kind, service, None, "failed",
                                         {"error": repr(exc)})
                    continue
                collect(results_conn, run_id, service, name, {"spec": m.spec, "kinds": kinds, "out": out})
            del sd
        store.finish_run(results_conn, run_id, "ok", {"counts": counts, "hash": digest.hexdigest()})
        store.prune(results_conn, args.site)
        return 0
    except Exception as exc:
        store.finish_run(results_conn, run_id, "failed", error=repr(exc))
        print(f"run failed: {exc!r}", file=sys.stderr)
        return 1


def check(names) -> int:
    ok = 0
    for n in names:
        try:
            methods.METHODS[n]()
            print(f"{n}: available")
            ok += 1
        except Unavailable as e:
            print(f"{n}: unavailable ({e})")
    return 0 if ok else 1


def _open(dsn, schema, read_only=False):
    c = connect(dsn)
    c.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema or "public")))
    c.commit()
    if read_only:
        c.execute("SET TRANSACTION READ ONLY")
    return c


def main(argv=None) -> int:
    a = parse_args(argv)
    if a.site == "company" and a.test in ("A", "C"):
        print("--site company supports --test B only", file=sys.stderr)
        return 2
    try:
        names = methods.select(a.methods)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if a.check:
        return check(names)
    if not names:
        print("no methods selected", file=sys.stderr)
        return 2
    cfg = load_config(None)
    dsn = a.dsn or cfg.database.dsn
    data, results = _open(dsn, a.schema, read_only=True), _open(dsn, a.results_schema)
    try:
        return run(a, cfg, data, results)
    finally:
        data.close()
        results.close()


if __name__ == "__main__":
    sys.exit(main())

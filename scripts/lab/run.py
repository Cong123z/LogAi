"""Lab runner: python -m scripts.lab.run --site laptop --test B --methods core --days 28

Walk-forward every selected method over the recorded per-minute counts, one service at a time, and store the results.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg.sql as sql

import scripts.lab.methods as methods
from logai2.config import load_config
from logai2.db import connect
from logai2.timeutil import TZ
from scripts.lab import inject, store, test_a
from scripts.lab.data import ServiceData
from scripts.lab.decide import finish_all
from scripts.lab.kinds import assign, has_new
from scripts.lab.load import load_service, services
from scripts.lab.methods import Unavailable
from scripts.lab.score import score_collect


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
    if a.days is not None and (a.date_from or a.date_to) or a.days is None and not (a.date_from and a.date_to):
        p.error("give --days N or both --from and --to")
    if a.days is not None and a.days < 1:
        p.error("--days must be at least 1")
    if a.days is None and a.date_from > a.date_to:
        p.error("--from must not be after --to")
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


HISTORY_DAYS = 35      # sigma: 5 weekdays + 4 weekend committed days; weekly profile / forecasters: up to 4 weeks


@dataclass
class RunCtx:
    """What a sink needs about the run: part(key, day) -> 'tune' | 'test' is the split (by days, or by keys)."""
    days: list[date]
    tune_days: list[date]
    split: str                       # 'days' | 'keys'
    part: Callable[[str, date], str]
    cfg: object
    sd: ServiceData
    injections: list = field(default_factory=list)     # Test A: the injections planted in `sd`


def make_part(service: str, days: list[date], tune_days: list[date], by_days: bool):
    if by_days:
        tune = set(tune_days)
        return lambda key, day: "tune" if day in tune else "test"
    return lambda key, day: "tune" if zlib.crc32((service + key).encode()) % 2 == 0 else "test"


def run(args, cfg, data_conn, results_conn, collect=None, finish=None) -> int:
    """collect(results_conn, run_id, service, method, spec, kinds, ctx) returns a sink: sink.add(key, day, {knob: DayOut})
    for each walked key and fitted day in ascending day order, sink.skip(day) for a day the fit refused, sink.end()
    after the last day. A sink should write only in end(), in one transaction (all-or-nothing); if add/end raises
    anyway, the runner deletes the method's lab_results rows for the service and writes `failed` rows. The runner keeps no output.
    Test A (args.test == "A", collect defaults to test_a.collect): the method is fitted once per day on the clean data and
    walked over 5 injected copies of it, one sink per copy; test_a.write_service stores the service's rows.
    finish(results_conn, run_id), when given, runs after the last service and before the run is marked ok (cross-service
    work such as tuning); an exception in it fails the run. A key is walked when its kind is in spec.kinds, or "M6" is and the
    key has a new minute in the window."""
    collect = collect or (test_a.collect if args.test == "A" else score_collect)
    names = methods.select(args.methods)
    if args.days is not None:
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
        for service in services(data_conn, d0, d1):
            sd = load_service(data_conn, service, d0, d1, HISTORY_DAYS)
            kinds = assign(sd, tune_days)     
            for key, kind in sorted(kinds.items()):
                counts[kind] = counts.get(kind, 0) + 1
                digest.update(f"{service}\0{key}\0{kind}\n".encode())
            ctx = RunCtx(days, tune_days, params["split"], make_part(service, days, tune_days, by_days), cfg, sd)
            variants, ps = [(sd, kinds, ctx)], None
            if args.test == "A":
                ps = inject.passes(sd, kinds, ctx.part, args.seed)
                inject.record(results_conn, run_id, args.schema, service, [injs for _, _, injs in ps])
                variants = [(p_sd, p_kinds, replace(ctx, sd=p_sd, injections=injs)) for p_sd, p_kinds, injs in ps]
            for name, m in instances.items():
                walked = [[k for k, kd in v_kinds.items() if kd in m.spec.kinds or ("M6" in m.spec.kinds and has_new(v_sd, k))]
                          for v_sd, v_kinds, _ in variants]
                try:
                    sinks = [collect(results_conn, run_id, service, name, m.spec, v_kinds, v_ctx)
                             for _, v_kinds, v_ctx in variants]
                    prev = m.fit(sd, days[0] - timedelta(1), cfg)    # live ran D-1's commit before 02:00 of day 1
                    for d in days:
                        model = m.fit(sd, d, cfg)
                        if model is None:
                            for sink in sinks:
                                sink.skip(d)
                        else:
                            for sink, v, keys in zip(sinks, variants, walked):
                                for key in keys:
                                    sink.add(key, d, m.day(model, prev, v[0], key, d, m.spec.knobs, cfg))
                        prev = model
                    for sink in sinks:
                        sink.end()
                    if args.test == "A":
                        test_a.write_service(results_conn, run_id, service, name, m.spec, sinks)
                except Exception as exc:
                    store.clear_results(results_conn, run_id, name, service)
                    for kind in sorted(m.spec.kinds):
                        store.put_result(results_conn, run_id, name, kind, service, None, "failed",
                                         {"error": repr(exc)})
                sinks = sink = model = prev = None
            sd = ctx = variants = ps = v = None         # only one ServiceData alive: nothing of this service survives into the next load
        if finish is not None:
            finish(results_conn, run_id)
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
    """read_only: the data connection (sticky for every later transaction); it also sees `public` (pgvector)."""
    c = connect(dsn)
    c.execute(sql.SQL("SET search_path TO {}" + (", public" if read_only else "")).format(sql.Identifier(schema or "public")))
    c.commit()
    c.read_only = read_only
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
        finish = {"A": test_a.finish, "B": lambda conn, rid: finish_all(conn, rid, seed=a.seed, cfg=cfg)}.get(a.test)
        return run(a, cfg, data, results, finish=finish)
    finally:
        data.close()
        results.close()


if __name__ == "__main__":
    sys.exit(main())

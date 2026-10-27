"""Test A: per-service rows from the injection passes, and the finish that picks a knob per method and kind.

Per service, per method and kind, `write_service` stores one lab_results row (scope = service) with, per knob, the clean tune
alert counters and the injection counters pooled over the passes. `finish` pools the services, takes the most sensitive knob
whose tune alert rate is within budget (tune.tune, as score methods in Test B) and writes the `scope = 'all'` row that
decide.py reads: knob and metrics {"recall": {small, medium, large}, "by_type": {type: {size: recall}},
"delay": {median, p90}, "alert_rate", "in_budget", "n_injections"}. The headline is recall["medium"].
"""
from __future__ import annotations

import math

from psycopg.types.json import Jsonb

from scripts.lab import store
from scripts.lab.decide import _spec
from scripts.lab.inject import SIZES, Injection
from scripts.lab.score import SIZE_BREAKS, IncidentSink, InjectSink
from scripts.lab.tune import alert_rate, tune


def collect(results_conn, run_id, service, method, spec, kinds, ctx) -> InjectSink:
    return InjectSink(results_conn, run_id, service, method, spec, kinds, ctx)


def collect_c(results_conn, run_id, service, method, spec, kinds, ctx) -> IncidentSink:
    return IncidentSink(results_conn, run_id, service, method, spec, kinds, ctx)


def incidents_c(conn, service, kinds, lo, hi, schema=None) -> list[Injection]:
    """The `test-c` incidents of the service touching minutes [lo, hi) as Injections (an incident without a key is not scorable)."""
    out = []
    for r in conn.execute("SELECT key, type, size, start_minute, end_minute FROM lab_incidents WHERE source = 'test-c' "
                          "AND service = %s AND data_schema = %s AND key IS NOT NULL AND start_minute < %s "
                          "AND end_minute > %s ORDER BY start_minute, id", (service, schema or "public", hi, lo)):
        name = SIZES[sum(r["size"] >= b for b in SIZE_BREAKS)]
        out.append(Injection(r["type"], name, r["size"], r["key"], kinds.get(r["key"], ""), r["start_minute"],
                             r["end_minute"]))
    return out


def _merge(into: dict, cells: dict) -> None:
    """cells: type -> size -> {n, hits, delays}, added into `into`."""
    for typ, sizes in cells.items():
        for size, c in sizes.items():
            t = into.setdefault(typ, {}).setdefault(size, {"n": 0, "hits": 0, "delays": []})
            t["n"] += c["n"]
            t["hits"] += c["hits"]
            t["delays"] += c["delays"]


def write_service(conn, run_id, service, method, spec, sinks) -> None:
    """sinks: the pass sinks (already ended). The tune counters come from the first pass (every pass has the same clean tune part)."""
    built, sums = sinks[0].build(), [s.summary() for s in sinks]
    with conn.transaction():
        for kd in sorted(spec.kinds):
            knobs = {}
            for k in sinks[0].knobs:
                tune_m = built[kd]["knobs"][str(k)]["tune"]
                inj = {}
                for s in sums:
                    _merge(inj, s[str(k)].get(kd, {}))
                knobs[str(k)] = {"tune": {"episodes": tune_m["episodes"], "key_days": tune_m["key_days"]}, "inj": inj}
            ok = any(key in sinks[0].added for key in sinks[0].keys_of[kd])
            store.put_result(conn, run_id, method, kd, service, None, "ok" if ok else "skipped",
                             {"n_keys": built[kd]["n_keys"], "knobs": knobs})


def _quantile(xs, q):
    xs = sorted(xs)
    return xs[min(math.ceil(q * len(xs)) - 1, len(xs) - 1)] if xs else None


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def finish(conn, run_id) -> None:
    conn.commit()           # the store's transactions must be the outermost ones
    groups: dict[tuple, list] = {}
    for r in conn.execute("SELECT method, kind, metrics FROM lab_results WHERE run_id = %s AND scope <> 'all' "
                          "AND status = 'ok' ORDER BY scope", (run_id,)):
        groups.setdefault((r["method"], r["kind"]), []).append(r["metrics"])
    with conn.transaction():
        for (m, kd), rows in sorted(groups.items()):
            key = {float(k): k for r in rows for k in r["knobs"]}
            rates = {k: alert_rate(sum(r["knobs"][key[k]]["tune"]["episodes"] for r in rows if key[k] in r["knobs"]),
                                   sum(r["knobs"][key[k]]["tune"]["key_days"] for r in rows if key[k] in r["knobs"]), kd)
                     for k in sorted(key)}
            knob, in_budget = tune(False, {k: {"alert_rate": v} for k, v in rates.items()})
            if knob is None:        # no normal minutes of this kind in the tune part (M6 before any injection): the method's default
                spec = _spec(m)
                knob, in_budget = (spec.default_knob if spec and spec.default_knob in rates else min(rates, default=None)), None
            if knob is None:
                store.put_result(conn, run_id, m, kd, "all", None, "skipped", {})
                continue
            inj = {}
            for r in rows:
                _merge(inj, r["knobs"].get(key[knob], {}).get("inj", {}))
            by_size = {s: [c for sizes in inj.values() for n, c in sizes.items() if n == s] for s in SIZES}
            rec = lambda cs: sum(c["hits"] for c in cs) / sum(c["n"] for c in cs) if sum(c["n"] for c in cs) else None   # noqa: E731
            delays = [d for cs in by_size.values() for c in cs for d in c["delays"]]
            metrics = {"recall": {s: rec(cs) for s, cs in by_size.items()},
                       "by_type": {t: {s: rec([c]) for s, c in sizes.items()} for t, sizes in sorted(inj.items())},
                       "delay": {"median": _median(delays), "p90": _quantile(delays, 0.9)},
                       "alert_rate": rates[knob], "in_budget": in_budget,
                       "n_injections": sum(c["n"] for cs in by_size.values() for c in cs)}
            store.put_result(conn, run_id, m, kd, "all", knob, "ok", metrics)


def finish_c(conn, run_id) -> None:
    finish(conn, run_id)
    flag_gap(conn, run_id)


GAP = 0.10


def flag_gap(conn, run_id) -> None:
    """Per method and kind: Test C medium recall against the latest ok Test A run of the same code_version (its `spike`
    medium recall, `burst` for M6, which has no spike). A gap above GAP sets injector_flag with both numbers."""
    a = conn.execute("SELECT a.id FROM lab_runs a, lab_runs c WHERE c.id = %s AND a.test = 'A' AND a.status = 'ok' "
                     "AND a.code_version = c.code_version ORDER BY a.id DESC LIMIT 1", (run_id,)).fetchone()
    if a is None:
        return
    with conn.transaction():
        for r in conn.execute("SELECT method, kind, metrics FROM lab_results WHERE run_id = %s AND scope = 'all' "
                              "AND status = 'ok'", (run_id,)).fetchall():
            ra = conn.execute("SELECT metrics FROM lab_results WHERE run_id = %s AND method = %s AND kind = %s "
                              "AND scope = 'all' AND status = 'ok'", (a["id"], r["method"], r["kind"])).fetchone()
            by_type = ra["metrics"].get("by_type", {}) if ra else {}
            a_rec = (by_type.get("spike") or by_type.get("burst") or {}).get("medium")
            c_rec = r["metrics"].get("recall", {}).get("medium")
            if a_rec is None or c_rec is None:
                continue
            conn.execute("UPDATE lab_results SET metrics = metrics || %s WHERE run_id = %s AND method = %s AND kind = %s "
                         "AND scope = 'all'", (Jsonb({"injector_flag": abs(a_rec - c_rec) > GAP, "a_recall": a_rec,
                                                      "c_recall": c_rec}), run_id, r["method"], r["kind"]))

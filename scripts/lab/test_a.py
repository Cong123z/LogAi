"""Test A: per-service rows from the injection passes, and the finish that picks a knob per method and kind.

Per service, per method and kind, `write_service` stores one lab_results row (scope = service) with, per knob, the clean tune
alert counters and the injection counters pooled over the passes. `finish` pools the services, takes the most sensitive knob
whose tune alert rate is within budget (tune.tune, as score methods in Test B) and writes the `scope = 'all'` row that
decide.py reads: knob and metrics {"recall": {small, medium, large}, "by_type": {type: {size: recall}},
"delay": {median, p90}, "alert_rate", "in_budget", "n_injections"}. The headline is recall["medium"].
"""
from __future__ import annotations

import math

from scripts.lab import store
from scripts.lab.decide import _spec
from scripts.lab.inject import SIZES
from scripts.lab.score import InjectSink
from scripts.lab.tune import alert_rate, tune


def collect(results_conn, run_id, service, method, spec, kinds, ctx) -> InjectSink:
    return InjectSink(results_conn, run_id, service, method, spec, kinds, ctx)


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

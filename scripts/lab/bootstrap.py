"""Day-block bootstrap (judging J6): resample the shared test days, recompute ratio, MASE and alert rate."""
from __future__ import annotations

import random

from scripts.lab.tune import alert_rate

F = ("n", "winkler", "abs_err", "abs_naive", "episodes", "key_days")


def sum_days(per_service_days: list[dict]) -> dict:
    """[{date: D}] over services -> {date: D summed}. Days with no normal minutes (key_days 0) are not data."""
    out: dict = {}
    for days in per_service_days:
        for d, x in days.items():
            if not x["key_days"]:
                continue
            o = out.setdefault(d, dict.fromkeys(F, 0.0))
            for f in F:
                o[f] += x[f]
    return out


def percentile(v: list[float], q: float) -> float:
    v = sorted(v)
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def point_ratio(m: dict, s: dict):
    """Interval-score ratio over the same day pool the bootstrap draws from (days both have), or None."""
    pool = set(m) & set(s)
    a = {f: sum(m[d][f] for d in pool) for f in F}
    b = {f: sum(s[d][f] for d in pool) for f in F}
    return (a["winkler"] / a["n"]) / (b["winkler"] / b["n"]) if a["n"] and b["n"] and b["winkler"] else None


def ci90(m: dict, s: dict, kind: str, seed: int, draws: int = 1000) -> dict:
    """m, s: summed days of the method and sigma. -> {"ratio": [lo, hi], "mase": [...], "alert_rate": [...]},
    each None when no draw defines it. Same seed -> same numbers."""
    pool = sorted(set(m) & set(s))
    rng = random.Random(seed)
    res: dict[str, list] = {"ratio": [], "mase": [], "alert_rate": []}
    for _ in range(draws if pool else 0):
        pick = rng.choices(pool, k=len(pool))
        a = {f: sum(m[d][f] for d in pick) for f in F}
        b = {f: sum(s[d][f] for d in pick) for f in F}
        if a["n"] and b["n"] and b["winkler"]:
            res["ratio"].append((a["winkler"] / a["n"]) / (b["winkler"] / b["n"]))
        if a["abs_naive"] > 0:
            res["mase"].append(a["abs_err"] / a["abs_naive"])
        if (r := alert_rate(a["episodes"], a["key_days"], kind)) is not None:
            res["alert_rate"].append(r)
    return {k: [percentile(v, 0.05), percentile(v, 0.95)] if v else None for k, v in res.items()}

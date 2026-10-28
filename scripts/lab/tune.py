"""Aggregation over services and knob choice (judging J4). Pure functions on the lab_results metrics dicts."""
from __future__ import annotations

COVERAGE_TARGET = 0.995
BUDGET = 1.0           # episodes per 100 key-days (M1: per service-week); the score of every kind


def alert_rate(episodes, key_days, kind):
    per, scale = (key_days / 7, 1) if kind == "M1" else (key_days, 100)
    return episodes / per * scale if per else None


def aggregate(ms, kind) -> dict:
    """Sum the additive parts of per-service metrics M (one knob, one part) and recompute the ratios.
    Sharpness is a median and cannot be summed: it is the minute-weighted mean of the per-service medians."""
    t = dict(n_minutes=0, covered=0.0, wink=0.0, episodes=0, flaps=0.0, key_days=0.0)
    sw = sn = 0.0
    for m in ms:
        n = m["n_minutes"] or 0
        t["n_minutes"] += n
        t["covered"] += (m["coverage"] or 0) * n
        t["wink"] += (m["interval_score"] or 0) * n
        t["episodes"] += m["episodes"]
        t["flaps"] += (m["flap_share"] or 0) * m["episodes"]
        t["key_days"] += m["key_days"]
        if m["sharpness"] is not None:
            sw, sn = sw + m["sharpness"] * n, sn + n
    n = t["n_minutes"]
    t.update(coverage=t["covered"] / n if n else None, interval_score=t["wink"] / n if n else None,
             sharpness=sw / sn if sn else None, alert_rate=alert_rate(t["episodes"], t["key_days"], kind),
             flap_share=t["flaps"] / t["episodes"] if t["episodes"] else None)
    return t


def tune(band: bool, tune_by_knob: dict[float, dict]) -> tuple[float | None, bool]:
    """(chosen knob, within budget). Band methods: coverage closest to 99.5 %, tie -> higher coverage (wider band).
    Score methods: the first (most sensitive) knob, in the dict's order, whose tune alert rate is within budget; none
    -> the last knob and False. The caller orders the dict most sensitive first (a method's knob grid; ascending when
    a low knob is the sensitive one, descending for a tail probability). Knobs with nothing measured are ignored; None when no knob is left."""
    if band:
        have = {k: m["coverage"] for k, m in tune_by_knob.items() if m["coverage"] is not None}
        return (min(have, key=lambda k: (round(abs(have[k] - COVERAGE_TARGET), 9), -have[k])) if have else None), True
    have = {k: m["alert_rate"] for k, m in tune_by_knob.items() if m["alert_rate"] is not None}
    if not have:
        return None, True
    ok = [k for k in have if have[k] <= BUDGET]
    return (ok[0], True) if ok else (list(have)[-1], False)

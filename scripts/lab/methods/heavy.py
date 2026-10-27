"""Shared helpers of the hourly forecasters (#8-#11): hourly history in, per-minute DayOut out. Pure Python."""
from __future__ import annotations

import math
from array import array
from datetime import date, timedelta
from statistics import NormalDist

from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, apply_d4

NAN = float("nan")


def z_of(coverage: float) -> float:
    """Two-sided normal z of a central coverage (0.995 -> 2.81)."""
    return NormalDist().inv_cdf(0.5 + coverage / 2)


def hist_days(sd: ServiceData, day: date) -> int:
    """Days before `day` with at least one observed minute."""
    return len({d for (d, _), n in sd.n_minutes.items() if d < day and n > 0})


def hourly_mean(sd: ServiceData, key: str, d: date, h: int) -> float:
    """Mean count per minute of the key in that hour; NaN when the hour has no observed minutes or is excluded."""
    n = sd.n_minutes.get((d, h), 0)
    if n <= 0 or (d, h) in sd.excluded:
        return NAN
    s = sd.hourly.get((key, d, h))
    return s[0] / n if s else 0.0


def hourly_series(sd: ServiceData, key: str, day: date, days: int) -> list[float]:
    """days*24 hourly means ending at 23:00 of day-1 (never reads `day` or later); NaN = missing."""
    return [hourly_mean(sd, key, day - timedelta(k), h) for k in range(days, 0, -1) for h in range(24)]


def fill(xs: list[float]) -> list[float]:
    """Linear interpolation of NaN gaps, nearest value at the edges; all-NaN stays NaN."""
    ok = [i for i, x in enumerate(xs) if not math.isnan(x)]
    if not ok:
        return list(xs)
    out = list(xs)
    for i in range(len(xs)):
        if math.isnan(xs[i]):
            lo = max((j for j in ok if j < i), default=None)
            hi = min((j for j in ok if j > i), default=None)
            if lo is None:
                out[i] = xs[hi]
            elif hi is None:
                out[i] = xs[lo]
            else:
                out[i] = xs[lo] + (xs[hi] - xs[lo]) * (i - lo) / (hi - lo)
    return out


def within_var(sd: ServiceData, key: str, day: date, hour: int, days: int = 14) -> float:
    """Mean over the last `days` days of the per-minute variance inside that hour (from sum, sumsq): the minute-level
    noise an hourly forecast does not carry."""
    vs = []
    for k in range(1, days + 1):
        d = day - timedelta(k)
        n = sd.n_minutes.get((d, hour), 0)
        if n > 0 and (d, hour) not in sd.excluded:
            s, sq = sd.hourly.get((key, d, hour), (0, 0))
            vs.append(max(sq / n - (s / n) ** 2, 0.0))
    return sum(vs) / len(vs) if vs else 0.0


# Method choice (bk11): an hourly forecast interval says where the hour's MEAN count per minute will be, far narrower than
# where a single minute falls. So the per-minute band half-width combines the method's own interval and the minute-level
# noise (the within-hour variance seen at that hour in the last 14 days) in quadrature.
def widen(half: float, z: float, var: float) -> float:
    """Half-width of the per-minute band: forecast uncertainty `half` and minute noise z*sqrt(var), in quadrature."""
    return math.sqrt(half * half + z * z * var)


def shape(sd: ServiceData, key: str, day: date) -> list[float]:
    """Within-hour shape, 1440 factors: per-slot mean of the last 7 days (observed minutes) normalised to mean 1 per
    hour. Flat (1.0) without per-minute history, or when the hour's mean is 0."""
    sums, cnt = [0.0] * 1440, [0] * 1440
    for k in range(1, 8):
        a = sd.minutes.get(key, {}).get(day - timedelta(k))
        if a is not None:
            for i, x in enumerate(a):
                if x != UNOBSERVED:
                    sums[i] += x
                    cnt[i] += 1
    out = [1.0] * 1440
    for h in range(24):
        sl = range(h * 60, h * 60 + 60)
        if any(cnt[i] == 0 for i in sl):
            continue
        means = [sums[i] / cnt[i] for i in sl]
        m = sum(means) / 60
        if m > 0:
            for i, x in zip(sl, means):
                out[i] = x / m
    return out


def nan_day() -> DayOut:
    return DayOut(array("d", [NAN] * 1440), array("d", [NAN] * 1440), array("d", [NAN] * 1440),
                  array("d", [0.0] * 1440), bytearray(1440))


def to_day(sd: ServiceData, key: str, day: date, exp: list[float], low: list[float], high: list[float],
           sh: list[float], cfg) -> DayOut:
    """24 hourly (expected, low, high) -> 1440-minute DayOut. NaN hour = no forecast (NaN band, never flagged);
    unobserved minutes get a NaN band and no flag. Score = (count - expected) / (high - expected)."""
    out = nan_day()
    counts = sd.minutes.get(key, {}).get(day)
    if counts is None:
        return out
    level = sd.level(key)
    for slot in range(1440):
        n = counts[slot]
        e, lo, hi = exp[slot // 60], low[slot // 60], high[slot // 60]
        if n == UNOBSERVED or math.isnan(e) or math.isnan(hi):
            continue
        f = sh[slot]
        e, lo, hi = e * f, max(lo * f, 0.0), hi * f
        out.expected[slot], out.low[slot], out.high[slot] = e, lo, hi
        if hi > e:
            out.score[slot] = (n - e) / (hi - e)
        out.high_flag[slot] = apply_d4(n, lo, hi, e, key, level, cfg)
    return out

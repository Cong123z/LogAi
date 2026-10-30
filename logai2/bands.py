"""Pure band methods for the Evaluation page; spec docs/specs/2026-10-30-evaluation-bands.md."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from statistics import NormalDist, median
from typing import NamedTuple

from logai2.owner.baseline import DAY_CAP, FLOOR_LEVEL, KEY_LEVEL
from logai2.timeutil import daytype

METHODS = ("sigma", "nb", "median", "ewma")
MIN_DAYS = {"sigma": 1, "nb": 1, "median": 3, "ewma": 2}
EWMA_DAYS = 28
HALF_LIFE = 2.0
UNTUNED_K = 3.0
SERVICE_KEYS = ("*total", "*error", "*warn", "*info")
NORMAL_ABOVE = 1000          # mean above which NB/Poisson is replaced by a normal (continuity-corrected)
_Z = NormalDist()


class DayHour(NamedTuple):
    day: date
    n: int
    s: int
    q: int

    @property
    def m(self) -> float:
        return self.s / self.n

    @property
    def v(self) -> float:
        return max(0.0, self.q / self.n - self.m ** 2)


class Band(NamedTuple):
    low: float
    high: float


class Hist(dict):
    """history() result; `.days` = every committed day in the range (a day with no usable row still counts as a picked day)."""
    days: list[date]


HISTORY = """
WITH born AS (SELECT min(day) AS day FROM hourly_counts WHERE service = %(s)s AND key = %(k)s AND key IN ('*warn', '*info'))
SELECT sh.day, sh.hour, sh.n_minutes AS n, coalesce(hc.sum, 0)::bigint AS s, coalesce(hc.sumsq, 0)::bigint AS q
FROM service_hours sh
JOIN day_status ds ON ds.service = sh.service AND ds.day = sh.day AND ds.committed
CROSS JOIN born b
LEFT JOIN hourly_counts hc ON hc.service = sh.service AND hc.key = %(k)s AND hc.day = sh.day AND hc.hour = sh.hour
WHERE sh.service = %(s)s AND sh.day BETWEEN %(a)s AND %(b)s AND sh.n_minutes > 0
  AND (b.day IS NULL OR sh.day >= b.day)
  AND NOT EXISTS (SELECT 1 FROM review_exclusions x
                  WHERE x.decision = 'exclude' AND x.service = sh.service AND x.day = sh.day
                    AND sh.hour >= x.hour_from AND sh.hour < x.hour_to)
"""


def history(conn, service: str, key: str, first: date, last: date) -> dict[tuple[date, int], DayHour]:
    """Every committed day in [first, last] with a service_hours row, n > 0, not excluded (REBUILD rules)."""
    p = {"s": service, "k": key, "a": first, "b": last}
    h = Hist(((r["day"], r["hour"]), DayHour(r["day"], r["n"], r["s"], r["q"])) for r in conn.execute(HISTORY, p))
    h.days = [r["day"] for r in conn.execute(
        "SELECT day FROM day_status WHERE service = %s AND committed AND day BETWEEN %s AND %s ORDER BY day",
        (service, first, last))]
    return h


def training(hist: dict[tuple[date, int], DayHour], day: date, hour: int, method: str) -> list[DayHour]:
    """Rows `method` fits on to judge `day`, oldest first."""
    dt = daytype(day)
    days = getattr(hist, "days", None) or sorted({d for d, _ in hist})
    same = sorted((d for d in days if d < day and daytype(d) == dt), reverse=True)
    if method == "ewma":
        same = [d for d in same if d >= day - timedelta(days=EWMA_DAYS)]
    else:
        same = same[:DAY_CAP[dt]]
    return [hist[(d, hour)] for d in reversed(same) if (d, hour) in hist]


def band(method: str, rows: list[DayHour], k: float) -> Band | None:
    if len(rows) < MIN_DAYS[method]:
        return None
    if method == "median":
        spread = math.sqrt(median(r.v for r in rows) + (1.4826 * _mad([r.m for r in rows])) ** 2)
        c = median(r.m for r in rows)
        return Band(c - k * spread, c + k * spread)
    if method == "ewma":
        newest = max(r.day for r in rows)
        w = [0.5 ** ((newest - r.day).days / HALF_LIFE) for r in rows]
    else:
        w = [1.0] * len(rows)
    wn = sum(a * r.n for a, r in zip(w, rows))
    m = sum(a * r.s for a, r in zip(w, rows)) / wn
    v = max(0.0, sum(a * r.q for a, r in zip(w, rows)) / wn - m * m)
    if method == "nb":
        return Band(*map(float, nb_quantiles(m, v, _Z.cdf(-k))))
    spread = k * math.sqrt(v)
    return Band(m - spread, m + spread)


def _mad(xs: list[float]) -> float:
    c = median(xs)
    return median(abs(x - c) for x in xs)


def nb_quantiles(mu: float, var: float, p: float) -> tuple[int, int]:
    return _quantiles(round(mu, 6), round(var, 6), p)


@lru_cache(maxsize=1 << 16)
def _quantiles(mu: float, var: float, p: float) -> tuple[int, int]:
    """(low, high): high = smallest c with P(X > c) <= p; low = largest c with P(X < c) <= p (0 when none).
    Port of scripts/lab/methods/nb.py::_quantiles, plus a normal branch above NORMAL_ABOVE."""
    if mu <= 0:
        return 0, 0
    if mu > NORMAL_ABOVE:
        z, sd = -_Z.inv_cdf(p), math.sqrt(max(var, mu))
        return max(0, math.floor(mu - z * sd + 0.5)), math.ceil(mu + z * sd - 0.5)
    r = _r(mu, var)
    limit = int(mu + 50 * math.sqrt(max(var, mu)) + 50)
    cum = 0.0                      # P(X < k)
    low, high = 0, limit
    for k in range(limit + 1):
        if cum <= p:
            low = k
        cum += _pmf(k, mu, r)
        if 1 - cum <= p:
            high = k
            break
    return low, high


def _r(mu: float, var: float) -> float:
    r = mu * mu / (var - mu) if var > mu else math.inf
    return math.inf if r > 1e6 else r


def _pmf(k: int, mu: float, r: float) -> float:
    if r == math.inf:
        return math.exp(-mu + k * math.log(mu) - math.lgamma(k + 1))
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                    + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu)))


def _sum_pmf(m: float, v: float, a: int, b: int) -> float:
    r = _r(m, v)
    tot = 0.0
    for k in range(a, b + 1):
        x = _pmf(k, m, r)
        tot += x
        if k > m and x < 1e-15 * tot:       # past the mode, the rest cannot move the sum
            break
    return tot


def _limit(m: float, v: float) -> int:
    return int(m + 50 * math.sqrt(max(v, m)) + 50)


def tail_above(m: float, v: float, t: int) -> float:
    """P(X >= t), X ~ NB(m, v) (Poisson when v <= m, normal when m > 1000)."""
    if t <= 0:
        return 1.0
    if m <= 0:
        return 0.0
    if m > NORMAL_ABOVE:
        return 1 - NormalDist(m, math.sqrt(max(v, m))).cdf(t - 0.5)
    if t <= m:
        return max(0.0, 1 - tail_below(m, v, t - 1))
    return _sum_pmf(m, v, t, max(t, _limit(m, v)))


def tail_below(m: float, v: float, t: int) -> float:
    """P(X <= t)."""
    if t < 0:
        return 0.0
    if m <= 0:
        return 1.0
    if m > NORMAL_ABOVE:
        return NormalDist(m, math.sqrt(max(v, m))).cdf(t + 0.5)
    if t >= m:
        return max(0.0, 1 - tail_above(m, v, t + 1))
    return _sum_pmf(m, v, 0, t)


def floor_of(cfg, key: str, level: str | None) -> int | None:
    """Alerting floor (events/min) of a key: ERROR/WARN levels only; `*warn` is view-only and has none."""
    if key in ("*warn", "*info"):
        return None
    lv = FLOOR_LEVEL.get((KEY_LEVEL.get(key) or level or "").upper())
    return cfg.floors.get(lv) if lv else None


def kind_of(key: str, mean: float) -> str:
    if key in ("*total", "*error"):
        return key
    if key in ("*warn", "*info"):
        return "*warn/*info"
    return "busy" if mean >= 2 else "sparse"


def in_scope(conn, service: str, key: str, now: datetime) -> bool:
    if key in SERVICE_KEYS:
        return True
    if key.startswith(("doc:", "verdict:")):
        return False
    return conn.execute("SELECT 1 FROM templates WHERE service = %s AND template_id = %s AND first_seen < %s",
                        (service, key, now - timedelta(hours=24))).fetchone() is not None

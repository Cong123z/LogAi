"""Pure band methods for the Evaluation page; spec docs/specs/2026-10-30-evaluation-bands.md."""
from __future__ import annotations

import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from statistics import NormalDist, median
from typing import NamedTuple

from logai2.db import get_setting, put_setting
from logai2.owner.baseline import DAY_CAP, FLOOR_LEVEL, KEY_LEVEL
from logai2.timeutil import TZ, daytype

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
    if len(rows) < MIN_DAYS[method] or not any(r.s for r in rows):    # all-zero rows: REBUILD writes no baseline row
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
    x = _pmf(a, m, r)
    step = None if x < 1e-290 else (lambda k, x: x * m / (k + 1)) if r == math.inf else \
        (lambda k, x, c=m / (r + m): x * (k + r) / (k + 1) * c)
    tot = 0.0
    for k in range(a, b + 1):
        tot += x
        if k > m and x < 1e-15 * tot:       # past the mode, the rest cannot move the sum
            break
        x = _pmf(k + 1, m, r) if step is None else step(k, x)     # recursion; lgamma when pmf(a) underflowed
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


# ---- tune: walk-forward fit test (spec section 5) ----

KGRID = (2, 2.5, 3, 3.5, 4, 5, 6, 8)
TARGET = 0.995
TUNE_VERSION = 1
FLAG_NEVER, FLAG_FEW = "never reaches 99.5 %", "not enough days"
_ta = lru_cache(maxsize=1 << 14)(tail_above)      # one hour's (m, v) is hit by 4 methods x 8 k
_tb = lru_cache(maxsize=1 << 14)(tail_below)


class TuneHour(NamedTuple):
    kind: str
    service: str
    key: str
    floor: int | None
    low_side: bool
    day: date
    hour: int
    act: DayHour               # the test hour: its (n, m, v) drive the expected minutes outside
    rows: dict                 # method -> training rows


def expected_outside(m: float, v: float, n: int, b: Band, floor: int | None, low_side: bool) -> float:
    """Expected minutes outside `b` in an hour with X ~ NB(m, v): above t = max(floor(high)+1, floor); below for *total."""
    out = n * _ta(m, v, max(math.floor(b.high) + 1, floor or 0))
    if low_side:
        out += n * _tb(m, v, math.ceil(b.low) - 1)
    return out


def _services(conn) -> list[str]:
    has = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                       "AND table_name = 'services' AND column_name = 'monitored'").fetchone()
    return [r["service"] for r in conn.execute(
        "SELECT service FROM services" + (" WHERE monitored" if has else "") + " ORDER BY service")]


def _lookback(conn, service: str, tdays: list[date]) -> date:
    """Earliest day training() can need for the test days: the oldest of the picked days and day - 28."""
    com = [r["day"] for r in conn.execute(
        "SELECT day FROM day_status WHERE service = %s AND committed AND day < %s", (service, tdays[-1]))]
    first = min(tdays) - timedelta(days=EWMA_DAYS)
    for d in tdays:
        same = sorted((c for c in com if c < d and daytype(c) == daytype(d)), reverse=True)[:DAY_CAP[daytype(d)]]
        first = min(first, same[-1] if same else first)
    return first


def _test_days(conn, today: date, n: int) -> list[date]:
    """The last n days before today that are committed in any monitored service, oldest first."""
    sv = _services(conn)
    return sorted(r["day"] for r in conn.execute(
        "SELECT DISTINCT day FROM day_status WHERE committed AND day < %s AND service = ANY(%s) "
        "ORDER BY day DESC LIMIT %s", (today, sv, n)))


def _plan(conn, today: date, tdays: list[date]):
    cutoff = datetime.combine(today, datetime.min.time(), TZ) - timedelta(hours=24)
    for svc in _services(conn):
        a = _lookback(conn, svc, tdays)
        yield svc, a, [(k, None) for k in SERVICE_KEYS]
        yield svc, a, [(r["template_id"], r["level"]) for r in conn.execute(
            "SELECT t.template_id, t.level FROM templates t WHERE t.service = %s AND t.first_seen < %s "
            "AND t.template_id NOT LIKE 'doc:%%' AND t.template_id NOT LIKE 'verdict:%%' "
            "AND EXISTS (SELECT 1 FROM hourly_counts h WHERE h.service = t.service AND h.key = t.template_id "
            "AND h.day BETWEEN %s AND %s) ORDER BY t.template_id", (svc, cutoff, a, tdays[-1]))]


def tune_hours(conn, cfg, today: date, test_days: int = 3, on_key=None):
    """Every (key, test day, hour) of every in-scope key of every monitored service, with the rows each method fits on."""
    tdays = _test_days(conn, today, test_days)
    plan = list(_plan(conn, today, tdays)) if tdays else []
    total, done = sum(len(keys) for _, _, keys in plan), 0
    for svc, a, keys in plan:
        for key, level in keys:
            hist = history(conn, svc, key, a, tdays[-1])
            prior = [r for (d, _), r in hist.items() if d < tdays[0]] or list(hist.values())
            mean = sum(r.s for r in prior) / max(1, sum(r.n for r in prior))
            kind, floor = kind_of(key, mean), floor_of(cfg, key, level)
            for d in tdays:
                for h in range(24):
                    if (d, h) in hist:
                        yield TuneHour(kind, svc, key, floor, key == "*total", d, h, hist[(d, h)],
                                       {m: training(hist, d, h, m) for m in METHODS})
            done += 1
            if on_key:
                on_key(done, total)


def pick_k(cov: dict[float, float]) -> tuple[float | None, str]:
    """Smallest k whose coverage reaches TARGET; none: k 8 with a flag; no data: no k."""
    if not cov:
        return None, FLAG_FEW
    return next(((k, "") for k in KGRID if cov[k] >= TARGET), (KGRID[-1], FLAG_NEVER))


def tune(conn, cfg, today: date, test_days: int = 3, on_key=None) -> dict:
    t0 = time.monotonic()
    # kind -> keys seen, method -> k -> [expected outside, minutes, width sum, hours]
    acc: dict[str, dict] = {}
    seen = set()
    tdays = _test_days(conn, today, test_days)
    for th in tune_hours(conn, cfg, today, test_days, on_key):
        a = acc.setdefault(th.kind, {"keys": 0, "m": {m: {k: [0.0, 0, 0.0, 0] for k in KGRID} for m in METHODS}})
        if (th.service, th.key) not in seen:
            seen.add((th.service, th.key))
            a["keys"] += 1
        for m in METHODS:
            for k in KGRID:
                b = band(m, th.rows[m], k)
                if b is None:
                    break                                   # min days / all zero do not depend on k
                c = a["m"][m][k]
                c[0] += expected_outside(th.act.m, th.act.v, th.act.n, b, th.floor, th.low_side)
                c[1] += th.act.n
                c[2] += b.high - max(b.low, 0)
                c[3] += 1
    kinds = {}
    for kind, a in acc.items():
        out = {}
        for m in METHODS:
            cells = a["m"][m]
            k, flag = pick_k({k: 1 - c[0] / c[1] for k, c in cells.items() if c[1]})
            out[m] = {"k": k, "flag": flag, "coverage": None, "width": None}
            if k is not None:
                out[m].update(coverage=1 - cells[k][0] / cells[k][1], width=cells[k][2] / cells[k][3])
        base = out["sigma"]["width"]
        for r in out.values():
            r["ratio"] = None if not base or r["width"] is None else r["width"] / base
        if base is not None:
            out["sigma"]["ratio"] = 1.0
        kinds[kind] = {"keys": a["keys"], "methods": out}
    result = {"version": TUNE_VERSION, "run_at": datetime.now(timezone.utc).isoformat(),
              "test_days": [d.isoformat() for d in tdays],
              "seconds": time.monotonic() - t0, "kinds": kinds}
    put_setting(conn, "bands_tune", result)
    conn.commit()
    return result


def stored_tune(conn) -> dict | None:
    v = get_setting(conn, "bands_tune")
    return v if v and v.get("version") == TUNE_VERSION else None


def best_k(result: dict | None, kind: str, method: str) -> tuple[float, bool]:
    k = ((result or {}).get("kinds", {}).get(kind, {}).get("methods", {}).get(method, {})).get("k")
    return (UNTUNED_K, False) if k is None else (k, True)


def main(argv: list[str]) -> int:
    import argparse
    from logai2.config import load_config
    from logai2.db import connect
    ap = argparse.ArgumentParser(prog="python -m logai2.bands")
    ap.add_argument("cmd", choices=["tune"])
    ap.add_argument("config", nargs="?")
    ap.add_argument("--test-days", type=int, default=3)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    conn = connect(cfg.database.dsn)
    res = tune(conn, cfg, datetime.now(TZ).date(), a.test_days)
    for kind, kd in res["kinds"].items():
        print(kind, kd["keys"], "keys", {m: (r["k"], r["flag"]) for m, r in kd["methods"].items()})
    print(f"{res['seconds']:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

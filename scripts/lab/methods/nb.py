"""Methods #2 (negative-binomial tail) and #3 (the same with outliers clipped at rebuild), plus `run_day`,
the minute loop the band methods #4 and #7 share.

nb/nb_clip take sigma's `*total` drop guards (no drop on a borrowed weekend or a young baseline) but have no
young-baseline widening on the upper side (a method difference)."""
from __future__ import annotations

import math
from array import array
from datetime import date
from functools import lru_cache

from logai2.owner.baseline import DAY_CAP, Baseline, Stat, full_after
from logai2.owner.detect import VIEW_ONLY_KEYS
from logai2.timeutil import daytype
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, Spec, apply_d4
from scripts.lab.methods.sigma import rebuild_stats

NAN = float("nan")
TAILS = (1e-2, 3e-3, 1e-3, 3e-4, 1e-4, 1e-5)


def run_day(model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg, bands) -> dict[float, DayOut]:
    """bands(model, hour) -> (expected, {knob: (low, high)}[, drop_ok]) or None (NaN band, score 0, no flag).
    drop_ok=False keeps the band but suppresses the `*total` drop flag.
    Minutes before the 02:00 commit use prev_model, as sigma does."""
    h, m = cfg.baseline.commit_time.split(":")
    switch = int(h) * 60 + int(m)
    old = prev_model or model
    counts, level = sd.minutes[key][day], sd.level(key)
    view_only = key in VIEW_ONLY_KEYS
    outs = {k: (array("d", [NAN] * 1440), array("d", [NAN] * 1440), array("d", [NAN] * 1440),
                array("d", [0.0] * 1440), bytearray(1440)) for k in knobs}
    for hour in range(24):
        for mdl, lo, hi in ((old, hour * 60, min(switch, hour * 60 + 60)), (model, max(switch, hour * 60), hour * 60 + 60)):
            b = bands(mdl, hour) if lo < hi else None
            if b is None:
                continue
            exp, kb, *rest = b
            drop_ok = rest[0] if rest else True
            for slot in range(lo, hi):
                n = counts[slot]
                if n == UNOBSERVED:
                    continue
                for k, (e, l, h_, s, f) in outs.items():
                    low, high = kb[k]
                    e[slot], l[slot], h_[slot] = exp, low, high
                    s[slot] = n / high if high > 0 else float(n > 0)
                    f[slot] = apply_d4(n, low if drop_ok else None, high, exp, key, level, cfg) and not view_only
    return {k: DayOut(*o) for k, o in outs.items()}


def nb_quantiles(mu: float, var: float, p: float) -> tuple[int, int]:
    return _quantiles(round(mu, 6), round(var, 6), p)


@lru_cache(maxsize=None)
def _quantiles(mu: float, var: float, p: float) -> tuple[int, int]:
    """(low, high): high = smallest c with P(X > c) <= p; low = largest c with P(X < c) <= p (0 when none).
    NB(mean mu, r = mu^2 / (var - mu)); Poisson when var <= mu (or r is so large that NB is Poisson)."""
    if mu <= 0:
        return 0, 0
    r = mu * mu / (var - mu) if var > mu else math.inf
    if r > 1e6:
        r, var = math.inf, mu
    limit = int(mu + 50 * math.sqrt(var) + 50)
    cum = 0.0                      # P(X < k)
    low, high = 0, limit
    for k in range(limit + 1):
        if cum <= p:
            low = k
        if r is math.inf:
            lp = -mu + k * math.log(mu) - math.lgamma(k + 1)
        else:
            lp = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                  + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu)))
        cum += math.exp(lp)
        if 1 - cum <= p:
            high = k
            break
    return low, high


class NB:
    spec = Spec(name="nb", number="#2", label="negative-binomial tail", kinds=frozenset({"M1", "M2", "M3", "M4", "M5", "M6"}),
                knobs=TAILS, default_knob=1e-3, band=True, heavy=False, cost=1, min_hour_days=1)

    def fit(self, sd: ServiceData, day: date, cfg) -> Baseline | None:
        model = rebuild_stats(sd, day)
        return model if model.days else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        doc = key.startswith("doc:")

        def bands(bl, hour):
            lk = bl.lookup_doc(sd.service, sd.docs[key], day, hour) if doc else bl.lookup(sd.service, key, day, hour)
            stats = [s for s in (lk.this, lk.next) if s is not None]
            if not stats:
                return None
            exp = lk.this.mean if lk.this else NAN
            kb = {}
            for p in knobs:
                qs = [nb_quantiles(s.mean, s.std ** 2, p) for s in stats]
                kb[p] = (min(q[0] for q in qs), max(q[1] for q in qs))
            return exp, kb, not lk.upper_only and lk.days >= full_after(cfg, lk.daytype)

        return run_day(model, prev_model, sd, key, day, knobs, cfg, bands)


def _picks(sd: ServiceData, day: date) -> dict[tuple[str, int], list[date]]:
    """(daytype, hour) -> the days sigma's rebuild picks (same rule as `rebuild_stats`; it does not expose them)."""
    committed = sorted({d for d, _ in sd.n_minutes if d < day}, reverse=True)
    out: dict[tuple[str, int], list[date]] = {}
    for dt in DAY_CAP:
        for d in [d for d in committed if daytype(d) == dt][:DAY_CAP[dt]]:
            for h in range(24):
                if sd.n_minutes.get((d, h), 0) > 0 and (d, h) not in sd.excluded:
                    out.setdefault((dt, h), []).append(d)
    return out


class NBClip(NB):
    spec = Spec(name="nb_clip", number="#3", label="negative-binomial tail, outliers clipped", kinds=NB.spec.kinds,
                knobs=TAILS, default_knob=1e-3, band=True, heavy=False, cost=2, min_hour_days=1)

    def fit(self, sd: ServiceData, day: date, cfg) -> Baseline | None:
        first = rebuild_stats(sd, day)
        if not first.days:
            return None
        stats = dict(first.stats)
        picks = _picks(sd, day)
        for key, per_day in sd.minutes.items():
            for (dt, hour), ds in picks.items():
                st = first.stats.get((sd.service, key, dt, hour))
                arrs = [per_day.get(d) for d in ds]
                if st is None or any(a is None for a in arrs):
                    continue                                    # no per-minute data: keep the unclipped hourly stat
                cap = nb_quantiles(st.mean, st.std ** 2, 1e-4)[1]
                vals = [min(c, cap) for a in arrs for c in a[hour * 60:(hour + 1) * 60] if c != UNOBSERVED]
                if vals:
                    mean = sum(vals) / len(vals)
                    stats[(sd.service, key, dt, hour)] = Stat(mean, math.sqrt(max(sum(v * v for v in vals) / len(vals) - mean ** 2, 0)))
        return Baseline(stats, first.days)

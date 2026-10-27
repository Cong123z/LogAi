"""Method #5, error ratio: a key's share of `*total` per (daytype, hour), tested with an exact binomial tail."""
from __future__ import annotations

import math
from array import array
from datetime import date
from functools import lru_cache

from logai2.owner.detect import VIEW_ONLY_KEYS
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, Spec, apply_d4
from scripts.lab.methods.nb import TAILS
from scripts.lab.methods.sigma import rebuild_stats

NAN = float("nan")


def binom_quantiles(n: int, ph: float, p: float) -> tuple[int, int]:
    return _quantiles(n, round(ph, 6), p)


@lru_cache(maxsize=None)
def _quantiles(n: int, ph: float, p: float) -> tuple[int, int]:
    """(low, high) of Binomial(n, ph): high = smallest c with P(X > c) <= p; low = largest c with P(X < c) <= p."""
    if ph <= 0:
        return 0, 0
    if ph >= 1:
        return n, n
    lq, lp, lc = math.log(ph), math.log1p(-ph), math.lgamma(n + 1)
    cum, low, high = 0.0, 0, n          # cum = P(X < k)
    for k in range(n + 1):
        if cum <= p:
            low = k
        cum += math.exp(lc - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lq + (n - k) * lp)
        if 1 - cum <= p:
            high = k
            break
    return low, high


class Ratio:
    spec = Spec(name="ratio", number="#5", label="error ratio vs *total (binomial)", kinds=frozenset({"M2", "M5"}),
                knobs=TAILS, default_knob=1e-3, band=True, heavy=False, cost=1, min_hour_days=1)

    def fit(self, sd: ServiceData, day: date, cfg):
        model = rebuild_stats(sd, day)      # no drop rule for M2/M5: sigma's drop guards do not matter here
        return model if model.days else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        h, m = cfg.baseline.commit_time.split(":")
        switch = int(h) * 60 + int(m)
        old = prev_model or model
        counts, totals, level = sd.minutes[key][day], sd.minutes["*total"][day], sd.level(key)
        view_only = key in VIEW_ONLY_KEYS
        shares: dict = {}

        def share(bl, hour):
            if (id(bl), hour) not in shares:
                lk = (bl.lookup_doc(sd.service, sd.docs[key], day, hour) if key.startswith("doc:")
                      else bl.lookup(sd.service, key, day, hour))
                tot = bl.lookup(sd.service, "*total", day, hour)
                ok = lk.this is not None and tot.this is not None and tot.this.mean > 0
                shares[id(bl), hour] = min(lk.this.mean / tot.this.mean, 1.0) if ok else None
            return shares[id(bl), hour]

        outs = {k: (array("d", [NAN] * 1440), array("d", [NAN] * 1440), array("d", [NAN] * 1440),
                    array("d", [0.0] * 1440), bytearray(1440)) for k in knobs}
        for slot in range(1440):
            c, n = counts[slot], totals[slot]
            ph = share(old if slot < switch else model, slot // 60)
            if c == UNOBSERVED or n == UNOBSERVED or n <= 0 or ph is None:
                continue
            for k, (e, l, hi, s, f) in outs.items():
                low, high = binom_quantiles(n, ph, k)
                e[slot], l[slot], hi[slot] = n * ph, low, high
                s[slot] = c / high if high > 0 else float(c > 0)
                f[slot] = apply_d4(c, low, high, n * ph, key, level, cfg) and not view_only
        return {k: DayOut(*o) for k, o in outs.items()}

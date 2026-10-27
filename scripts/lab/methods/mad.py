"""Method #4, median +- k * MAD of the last 7 days of per-minute counts, per key and hour of day."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import Spec
from scripts.lab.methods.nb import run_day


class MadModel:
    def __init__(self, sd: ServiceData, days: list[date]):
        self.sd, self.days, self.cache = sd, days, {}

    def stat(self, key: str, hour: int):
        """(median, MAD) of the observed minutes of `hour` on the model's days, None when there are none."""
        if (key, hour) not in self.cache:
            vals = [c for d in self.days if (a := self.sd.minutes.get(key, {}).get(d)) is not None
                    for c in a[hour * 60:(hour + 1) * 60] if c != UNOBSERVED]
            if vals:
                m = median(vals)
                self.cache[key, hour] = (m, median(abs(v - m) for v in vals))
            else:
                self.cache[key, hour] = None
        return self.cache[key, hour]


class Mad:
    spec = Spec(name="mad", number="#4", label="median +- k MAD", kinds=frozenset({"M3", "M4", "M6"}),
                knobs=(3, 4, 5, 6, 8, 10), default_knob=5, band=True, heavy=False, cost=1, min_minute_days=7)

    def fit(self, sd: ServiceData, day: date, cfg) -> MadModel | None:
        days = [day - timedelta(days=i) for i in range(1, 8)]
        return MadModel(sd, days) if all(d in sd.observed and any(sd.observed[d]) for d in days) else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        def bands(mdl, hour):
            st = mdl.stat(key, hour)
            if st is None:
                return None
            m, mad = st
            return m, {k: (max(0.0, m - k * 1.4826 * max(mad, 1)), m + k * 1.4826 * max(mad, 1)) for k in knobs}

        return run_day(model, prev_model, sd, key, day, knobs, cfg, bands)

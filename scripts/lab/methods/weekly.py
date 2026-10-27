"""Method #7, the hour-of-week profile (mean +- k * std per minute) with Vietnamese holidays."""
from __future__ import annotations

import math
from datetime import date, timedelta

from scripts.lab.data import ServiceData
from scripts.lab.holidays import is_holiday
from scripts.lab.methods import Spec
from scripts.lab.methods.nb import run_day

WINDOW, MAX_DAYS = 28, 4


class WeeklyModel:
    def __init__(self, sd: ServiceData, cutoff: date, cfg):
        self.sd, self.cutoff, self.cfg, self.cache = sd, cutoff, cfg, {}
        self.history = sorted({d for d, _ in sd.n_minutes if d < cutoff}, reverse=True)
        self.holidays = [d for d in self.history if is_holiday(d, cfg)]

    def profile(self, day: date) -> tuple:
        """('holiday',) from past holidays, else the same-weekday profile (Sunday's for a holiday with none)."""
        if is_holiday(day, self.cfg):
            if self.holidays:
                return "holiday", tuple(self.holidays[:MAX_DAYS])
            wd = 6
        else:
            wd = day.weekday()
        near = [d for d in self.history if d.weekday() == wd and d >= self.cutoff - timedelta(days=WINDOW)
                and not is_holiday(d, self.cfg)]
        return wd, tuple(near[:MAX_DAYS])

    def stat(self, key: str, day: date, hour: int):
        """(mean, std) per minute, None when the profile has fewer than 2 days (1 for a holiday profile)."""
        prof, ds = self.profile(day)
        k = (key, prof, hour)
        if k not in self.cache:
            picks = [d for d in ds if self.sd.n_minutes.get((d, hour), 0) > 0 and (d, hour) not in self.sd.excluded]
            if len(picks) < (1 if prof == "holiday" else 2):
                self.cache[k] = None
            else:
                n = sum(self.sd.n_minutes[d, hour] for d in picks)
                s = sum(self.sd.hourly.get((key, d, hour), (0, 0))[0] for d in picks)
                sq = sum(self.sd.hourly.get((key, d, hour), (0, 0))[1] for d in picks)
                self.cache[k] = (s / n, math.sqrt(max(sq / n - (s / n) ** 2, 0)))
        return self.cache[k]


class Weekly:
    spec = Spec(name="weekly", number="#7", label="weekly profile + holidays", kinds=frozenset({"M1", "M2"}),
                knobs=(2.0, 2.5, 3.0, 3.5, 4.0, 5.0), default_knob=3.0, band=True, heavy=False, cost=1, min_hour_days=14)

    def fit(self, sd: ServiceData, day: date, cfg) -> WeeklyModel | None:
        return WeeklyModel(sd, day, cfg) if len({d for d, _ in sd.n_minutes if d < day}) >= 14 else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        def bands(mdl, hour):
            st = mdl.stat(key, day, hour)
            return None if st is None else (st[0], {k: (st[0] - k * st[1], st[0] + k * st[1]) for k in knobs})

        return run_day(model, prev_model, sd, key, day, knobs, cfg, bands)

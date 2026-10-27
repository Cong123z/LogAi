"""Method #8: Holt-Winters / ETS on the key's hourly series (additive, no trend, seasonal 24 h or 168 h)."""
from __future__ import annotations

import math
import warnings
from datetime import date

from scripts.lab.data import ServiceData
from scripts.lab.methods import Spec, Unavailable
from scripts.lab.methods import heavy

NAN = float("nan")


class _Model:
    def __init__(self, days: int):
        self.days = days
        self.cache: dict[str, tuple[list[float], float] | None] = {}


class Ets:
    spec = Spec(name="ets", number="#8", label="Holt-Winters / ETS", kinds=frozenset({"M1", "M2", "M3"}),
                knobs=(0.98, 0.99, 0.995, 0.999, 0.9999), default_knob=0.995, band=True, heavy=True, cost=3,
                min_hour_days=14)

    def __init__(self):
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
        except ImportError as e:
            raise Unavailable("statsmodels not installed") from e
        self._es = ExponentialSmoothing

    def fit(self, sd: ServiceData, day: date, cfg):
        n = heavy.hist_days(sd, day)
        return _Model(min(n, 28)) if n >= self.spec.min_hour_days else None

    def _forecast(self, model: _Model, sd: ServiceData, key: str, day: date):
        """(24 hourly expected, residual std) from one refit on the series before `day`; None = no forecast."""
        y = heavy.fill(heavy.hourly_series(sd, key, day, model.days))
        if any(math.isnan(v) for v in y):
            return None
        period = 168 if model.days >= 21 else 24
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = self._es(y, trend=None, seasonal="add", seasonal_periods=period,
                               initialization_method="estimated").fit()
                exp = [max(float(v), 0.0) for v in res.forecast(24)]
                sdv = float(res.resid.std())
        except Exception:      # a series the optimiser cannot fit has no forecast: NaN band, never flagged
            return None
        return None if any(math.isnan(v) for v in exp) or math.isnan(sdv) else (exp, sdv)

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        if key not in model.cache:
            model.cache[key] = self._forecast(model, sd, key, day)
        fc = model.cache[key]
        if fc is None:
            return {k: heavy.nan_day() for k in knobs}
        exp, sdv = fc
        sh = heavy.shape(sd, key, day)
        var = [heavy.within_var(sd, key, day, h) for h in range(24)]
        out = {}
        for k in knobs:
            z = heavy.z_of(k)
            w = [heavy.widen(z * sdv, z, v) for v in var]
            out[k] = heavy.to_day(sd, key, day, exp, [e - x for e, x in zip(exp, w)], [e + x for e, x in zip(exp, w)],
                                  sh, cfg)
        return out

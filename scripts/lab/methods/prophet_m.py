"""Method #9: Prophet on the key's hourly series: daily + weekly seasonality, config holidays."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from scripts.lab.data import ServiceData
from scripts.lab.methods import Spec, Unavailable
from scripts.lab.methods import heavy


class _Model:
    def __init__(self, days: int):
        self.days = days
        self.cache: dict[str, tuple | None] = {}


class ProphetM:
    spec = Spec(name="prophet", number="#9", label="Prophet", kinds=frozenset({"M1", "M2", "M5"}),
                knobs=(0.98, 0.99, 0.995, 0.999, 0.9999), default_knob=0.995, band=True, heavy=True, cost=5,
                min_hour_days=14)

    def __init__(self):
        try:
            import numpy
            import pandas
            from prophet import Prophet
        except ImportError as e:
            raise Unavailable("prophet not installed") from e
        for name in ("prophet", "cmdstanpy"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self._np, self._pd, self._prophet = numpy, pandas, Prophet

    def fit(self, sd: ServiceData, day: date, cfg):
        n = heavy.hist_days(sd, day)
        return _Model(min(n, 28)) if n >= self.spec.min_hour_days else None

    def _forecast(self, model: _Model, sd: ServiceData, key: str, day: date, cfg):
        """(24 rows of yhat, lower, upper, per knob via interval_width set at predict time); None = no forecast."""
        pd = self._pd
        y = heavy.hourly_series(sd, key, day, model.days)
        t0 = datetime.combine(day - timedelta(model.days), datetime.min.time())
        df = pd.DataFrame({"ds": [t0 + timedelta(hours=i) for i in range(len(y))], "y": y}).dropna()
        if len(df) < 48:
            return None
        hol = pd.DataFrame({"holiday": "vn", "ds": pd.to_datetime(list(cfg.lab.holidays))}) if cfg.lab.holidays else None
        m = self._prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality=False,
                          holidays=hol, uncertainty_samples=300)
        try:
            m.fit(df)
        except Exception:      # a series Stan cannot fit has no forecast: NaN band, never flagged
            return None
        future = pd.DataFrame({"ds": [datetime.combine(day, datetime.min.time()) + timedelta(hours=h) for h in range(24)]})
        return m, future

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        if key not in model.cache:
            model.cache[key] = self._forecast(model, sd, key, day, cfg)
        fc = model.cache[key]
        if fc is None:
            return {k: heavy.nan_day() for k in knobs}
        m, future = fc
        sh = heavy.shape(sd, key, day)
        var = [heavy.within_var(sd, key, day, h) for h in range(24)]
        out = {}
        for k in knobs:
            m.interval_width = k
            self._np.random.seed(7)      # its sampled interval is reproducible
            p = m.predict(future)
            z = heavy.z_of(k)
            exp = [max(float(v), 0.0) for v in p["yhat"]]
            lo = [e - heavy.widen(max(e - float(l), 0.0), z, v) for e, l, v in zip(exp, p["yhat_lower"], var)]
            hi = [e + heavy.widen(max(float(u) - e, 0.0), z, v) for e, u, v in zip(exp, p["yhat_upper"], var)]
            out[k] = heavy.to_day(sd, key, day, exp, lo, hi, sh, cfg)
        return out

"""Method #10: one LightGBM quantile model per fit day and service over every key's hourly rows.

Day-ahead features (nothing from the scored day): hour, weekday, holiday, lag 1 h = the last hour before the day
(23:00 of D-1, the same for all 24 hours: the day is forecast before it starts), lag 24 h = same hour D-1, lag 168 h =
same hour D-7, log of the key's mean, key class. Missing lags are NaN, so a key with no history (M6) still gets a forecast.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from scripts.lab.data import ServiceData
from scripts.lab.holidays import is_holiday
from scripts.lab.methods import Spec, Unavailable
from scripts.lab.methods import heavy

NAN = float("nan")
TAILS = (0.01, 0.005, 0.0025, 0.001, 0.0005)
TRAIN_DAYS = 28
PARAMS = dict(objective="quantile", num_leaves=15, learning_rate=0.1, min_data_in_leaf=10, verbose=-1, num_threads=4,
              seed=7)
ROUNDS = 100                    # <= 200 trees, <= 31 leaves: keep the nightly Job small


def _klass(sd: ServiceData, key: str) -> float:
    base = {"*total": 0, "*error": 1}.get(key, 2 if key.startswith("doc:") else 3)
    return base * 4 + {"INFO": 0, "WARN": 1, "ERROR": 2}.get(sd.level(key).upper(), 0)


class _Model:
    def __init__(self, models):
        self.models = models      # models: {"med": m, (q, "lo"|"hi"): m}


class Lgbm:
    spec = Spec(name="lgbm", number="#10", label="LightGBM quantile", kinds=frozenset({"M1", "M2", "M3", "M4", "M5", "M6"}),
                knobs=TAILS, default_knob=0.0025, band=True, heavy=True, cost=4, min_hour_days=14)

    def __init__(self):
        try:
            import lightgbm
            import numpy
        except (ImportError, OSError) as e:       # OSError: the shared library (libgomp) is missing
            raise Unavailable("lightgbm not installed") from e
        self._lgb, self._np = lightgbm, numpy

    def _row(self, sd, key, day, h, hist, mean, cfg):
        d1, d7 = day - timedelta(1), day - timedelta(7)
        return [h, day.weekday(), float(is_holiday(day, cfg)), hist.get((d1, 23), NAN), hist.get((d1, h), NAN),
                hist.get((d7, h), NAN), math.log1p(mean) if mean == mean else NAN, _klass(sd, key)]

    def _history(self, sd, key, day):
        """{(date, hour): hourly mean} of the key before `day` (observed hours only), and its mean."""
        hist = {}
        for d in {d for (d, _) in sd.n_minutes if day - timedelta(TRAIN_DAYS + 7) <= d < day}:
            for h in range(24):
                v = heavy.hourly_mean(sd, key, d, h)
                if v == v:
                    hist[(d, h)] = v
        return hist, (sum(hist.values()) / len(hist) if hist else NAN)

    def fit(self, sd: ServiceData, day: date, cfg):
        if heavy.hist_days(sd, day) < self.spec.min_hour_days:
            return None
        np = self._np
        keys = sorted({k for (k, d, _) in sd.hourly if d < day})
        X, y = [], []
        for key in keys:
            hist, mean = self._history(sd, key, day)
            for (d, h), v in hist.items():
                if d >= day - timedelta(TRAIN_DAYS):
                    X.append(self._row(sd, key, d, h, hist, mean, cfg))
                    y.append(v)
        X, y = np.array(X, dtype=float), np.array(y, dtype=float)
        ds = self._lgb.Dataset(X, y, free_raw_data=False)
        mk = lambda alpha: self._lgb.train({**PARAMS, "alpha": alpha}, ds, num_boost_round=ROUNDS)
        models = {"med": mk(0.5)}
        for q in TAILS:
            models[(q, "lo")], models[(q, "hi")] = mk(q), mk(1 - q)
        return _Model(models)

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        np = self._np
        hist, mean = self._history(sd, key, day)
        X = np.array([self._row(sd, key, day, h, hist, mean, cfg) for h in range(24)], dtype=float)
        exp = [max(float(v), 0.0) for v in model.models["med"].predict(X)]
        sh = heavy.shape(sd, key, day)
        var = [heavy.within_var(sd, key, day, h) for h in range(24)]
        out = {}
        for q in knobs:
            z = heavy.z_of(1 - 2 * q)         # the band between the q and 1-q quantiles is 1-2q wide
            lo_q, hi_q = model.models[(q, "lo")].predict(X), model.models[(q, "hi")].predict(X)
            lo = [e - heavy.widen(max(e - float(a), 0.0), z, v) for e, a, v in zip(exp, lo_q, var)]
            hi = [e + heavy.widen(max(float(b) - e, 0.0), z, v) for e, b, v in zip(exp, hi_q, var)]
            out[q] = heavy.to_day(sd, key, day, exp, lo, hi, sh, cfg)
        return out

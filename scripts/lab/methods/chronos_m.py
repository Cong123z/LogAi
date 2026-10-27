"""Method #11: Chronos-Bolt-small, zero-shot, on the key's last 7 days of hourly means (horizon 24 h).

Bolt returns quantiles 0.1/0.5/0.9; the requested tails are extrapolated with a normal fitted to the 0.1-0.9 spread.
Besides *total/*error, only a seeded 20 % sample of the other keys (M6) is forecast; the rest get a NaN band.
"""
from __future__ import annotations

import os
import zlib
from datetime import date
from pathlib import Path

from scripts.lab.data import ServiceData
from scripts.lab.methods import Spec, Unavailable
from scripts.lab.methods import heavy

Z90 = 1.2815515655446004          # inv_cdf(0.9)
SEED = 7


def sampled(service: str, key: str) -> bool:
    return key in ("*total", "*error") or zlib.crc32(f"{SEED}\0{service}\0{key}".encode()) % 5 == 0


class _Model:
    def __init__(self):
        self.cache: dict[str, tuple | None] = {}


class ChronosM:
    spec = Spec(name="chronos", number="#11", label="Chronos-Bolt small", kinds=frozenset({"M1", "M2", "M6"}),
                knobs=(0.01, 0.005, 0.0025, 0.001, 0.0005), default_knob=0.0025, band=True, heavy=True, cost=6)

    def __init__(self):
        try:
            import torch
            from chronos import BaseChronosPipeline
        except ImportError as e:
            raise Unavailable("chronos-forecasting not installed") from e
        path = os.environ.get("LOGAI2_CHRONOS_PATH", "/models/chronos-bolt-small")
        if not Path(path).is_dir():
            raise Unavailable(f"chronos weights not found at {path}")
        torch.set_num_threads(4)
        self._torch, self._path, self._pipe = torch, path, None
        self._pipeline_cls = BaseChronosPipeline

    def fit(self, sd: ServiceData, day: date, cfg):
        return _Model()

    def _forecast(self, sd: ServiceData, key: str, day: date):
        """(median, sd) per hour of the day; None = no usable context."""
        ctx = heavy.hourly_series(sd, key, day, 7)
        if sum(v == v for v in ctx) < 24:
            return None
        if self._pipe is None:
            self._pipe = self._pipeline_cls.from_pretrained(self._path, device_map="cpu", torch_dtype=self._torch.float32)
        q, _ = self._pipe.predict_quantiles(self._torch.tensor(ctx, dtype=self._torch.float32).unsqueeze(0),
                                            prediction_length=24, quantile_levels=[0.1, 0.5, 0.9])
        q = q[0].tolist()
        return [max(r[1], 0.0) for r in q], [max(r[2] - r[0], 0.0) / (2 * Z90) for r in q]

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        if not sampled(sd.service, key):
            return {k: heavy.nan_day() for k in knobs}
        if key not in model.cache:
            model.cache[key] = self._forecast(sd, key, day)
        fc = model.cache[key]
        if fc is None:
            return {k: heavy.nan_day() for k in knobs}
        exp, sdv = fc
        sh = heavy.shape(sd, key, day)
        var = [heavy.within_var(sd, key, day, h) for h in range(24)]
        out = {}
        for q in knobs:
            z = heavy.z_of(1 - 2 * q)
            w = [heavy.widen(z * s, z, v) for s, v in zip(sdv, var)]
            out[q] = heavy.to_day(sd, key, day, exp, [e - x for e, x in zip(exp, w)], [e + x for e, x in zip(exp, w)],
                                  sh, cfg)
        return out

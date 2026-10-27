"""Method #13, PCA on the service's template mix (Xu et al. 2009), pure Python power iteration.

Fit on the 7 days before D: the (at most 100 busiest) templates with any count, vectors log1p(count) on observed
minutes, every 5th minute, centred; top components until 90 % of the variance (max 10). Q = squared residual; the knob is
the quantile of the fit vectors' Q used as threshold. The model is refit at most every 7 days (the instance caches it per
service and window; it only ever uses days before the day it was fitted for) and is used for all minutes of a day
(no 02:00 switch to the previous model). The sink does not store top_templates yet.
"""
from __future__ import annotations

import math
from array import array
from datetime import date, timedelta
from operator import mul

from scripts.lab.data import MIX, UNOBSERVED, ServiceData, slot_of
from scripts.lab.methods import DayOut, Spec, apply_d4


def sumprod(a, b) -> float:      # math.sumprod needs Python 3.12; the lab image is 3.11
    return sum(map(mul, a, b))


MAX_TEMPLATES, STEP, MAX_COMPS, VARIANCE, REFIT_DAYS = 100, 5, 10, 0.9, 7


class PcaModel:
    def __init__(self, names, mean, comps, qs):
        self.names, self.mean, self.comps, self.qs = names, mean, comps, qs

    def residual(self, x: list[float]) -> list[float]:
        """x (already centred) minus its projection on the components."""
        r = list(x)
        for v in self.comps:
            c = sumprod(x, v)
            r = [ri - c * vi for ri, vi in zip(r, v)]
        return r

    def q(self, x: list[float]) -> float:
        return sumprod(x, x) - sum(sumprod(x, v) ** 2 for v in self.comps)

    def threshold(self, quantile: float) -> float:
        return self.qs[min(len(self.qs) - 1, int(quantile * len(self.qs)))]


def _vector(names, sd: ServiceData, day: date, slot: int) -> list[float]:
    out = []
    for n in names:
        a = sd.minutes[n].get(day)
        out.append(math.log1p(max(a[slot], 0)) if a is not None else 0.0)
    return out


def _components(rows: list[list[float]]):
    d, n = len(rows[0]), len(rows)
    cols = list(zip(*rows))
    cov = [[0.0] * d for _ in range(d)]
    for i in range(d):
        for j in range(i, d):
            cov[i][j] = cov[j][i] = sumprod(cols[i], cols[j]) / (n - 1)
    total, got, comps = sum(cov[i][i] for i in range(d)), 0.0, []
    while total > 0 and len(comps) < MAX_COMPS and got < VARIANCE * total:
        v = [1.0 + (i % 7) * 0.1 for i in range(d)]
        lam = 0.0
        for _ in range(300):
            w = [sumprod(row, v) for row in cov]
            norm = math.sqrt(sumprod(w, w))
            if norm < 1e-12:
                return comps
            w = [x / norm for x in w]
            done = max(abs(a - b) for a, b in zip(w, v)) < 1e-9
            v, lam = w, norm
            if done:
                break
        comps.append(v)
        got += lam
        cov = [[cov[i][j] - lam * v[i] * v[j] for j in range(d)] for i in range(d)]
    return comps


class Pca:
    spec = Spec(name="pca", number="#13", label="PCA on the template mix", kinds=frozenset({"M7"}),
                knobs=(0.99, 0.995, 0.999, 0.9995, 0.9999), default_knob=0.999, band=False, heavy=False, cost=2,
                min_minute_days=7)

    def __init__(self):
        self._last: tuple | None = None        # ((service, first day, last day), fit day, model)

    def fit(self, sd: ServiceData, day: date, cfg) -> PcaModel | None:
        ident = (sd.service, sd.days[0], sd.days[-1])
        if self._last and self._last[0] == ident and timedelta(0) <= day - self._last[1] < timedelta(REFIT_DAYS):
            return self._last[2]
        days = [day - timedelta(days=i) for i in range(1, 8)]
        if not all(d in sd.observed and any(sd.observed[d]) for d in days):
            return None
        busy = {}
        for key, per in sd.minutes.items():
            if key.startswith("*") or key.startswith("doc:") or key.endswith(":unparsed"):
                continue
            t = sum(c for d in days if d in per for c in per[d] if c > 0)
            if t > 0:
                busy[key] = t
        names = sorted(busy, key=lambda k: (-busy[k], k))[:MAX_TEMPLATES]
        rows = [_vector(names, sd, d, s) for d in days for s in range(0, 1440, STEP) if sd.observed[d][s]]
        if len(names) < 2 or len(rows) < 20:
            return None
        mean = [sum(c) / len(rows) for c in zip(*rows)]
        rows = [[x - m for x, m in zip(r, mean)] for r in rows]
        model = PcaModel(names, mean, _components(rows), [])
        model.qs = sorted(model.q(r) for r in rows)
        self._last = (ident, day, model)
        return model

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        counts, level = sd.minutes[key][day], sd.level(key)
        thr = {k: model.threshold(k) for k in knobs}
        scores = {k: array("d", [0.0] * 1440) for k in knobs}
        flags = {k: bytearray(1440) for k in knobs}
        for slot in range(1440):
            if counts[slot] == UNOBSERVED:
                continue
            x = [v - m for v, m in zip(_vector(model.names, sd, day, slot), model.mean)]
            q = model.q(x)
            for k in knobs:
                scores[k][slot] = q
                flags[k][slot] = apply_d4(counts[slot], None, thr[k], None, key, level, cfg, score=q)
        return {k: DayOut(None, None, None, scores[k], flags[k]) for k in knobs}


def top_templates(model: PcaModel, sd: ServiceData, minute: int, n: int = 3) -> list[tuple[str, float]]:
    """The templates that contribute most to the minute's Q: (template_id, squared residual)."""
    day, slot = slot_of(minute)
    x = [v - m for v, m in zip(_vector(model.names, sd, day, slot), model.mean)]
    r = model.residual(x)
    return sorted(zip(model.names, (v * v for v in r)), key=lambda t: -t[1])[:n]

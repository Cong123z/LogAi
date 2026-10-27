"""Method #12: Matrix Profile (STUMPY), window 60, on the last 7 days + the scored day's per-minute series.

Score = z-normalised distance of the window ending at that minute to its nearest other window. Score method: no band.
Threshold relative to the key's own history (like pca): the knob is a quantile of the matrix-profile distances of the
7 history days alone (no data of the scored day), so a noisy key gets a high threshold and a smooth one a low one.
Only the top 200 non-doc keys per service (by mean over the last 7 days) are scored; the others get a zero score.
"""
from __future__ import annotations

from array import array
from datetime import date, timedelta

from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, Spec, Unavailable, apply_d4

M = 60
TOP = 200


class _Model:
    def __init__(self, top: set[str]):
        self.top = top
        self.dist: dict[str, list[float]] = {}      # key -> sorted history distances (filled on first use)


class MProfile:
    spec = Spec(name="mprofile", number="#12", label="Matrix Profile (STUMPY)", kinds=frozenset({"M1", "M3"}),
                knobs=(0.99, 0.995, 0.999, 0.9995, 0.9999), default_knob=0.999, band=False, heavy=True, cost=5, min_minute_days=7)

    def __init__(self):
        try:
            import numpy
            import stumpy
        except ImportError as e:
            raise Unavailable("stumpy not installed") from e
        self._np, self._stump = numpy, stumpy.stump

    def _prior(self, sd: ServiceData, day: date) -> list[date]:
        return [d for d in (day - timedelta(k) for k in range(7, 0, -1)) if d in sd.observed]

    def fit(self, sd: ServiceData, day: date, cfg):
        prior = self._prior(sd, day)
        if len(prior) < self.spec.min_minute_days:
            return None
        mean = {}
        for key, per in sd.minutes.items():
            if key.startswith("doc:") or (key.startswith("*") and key != "*total"):
                continue
            xs = [x for d in prior for x in per.get(d, ()) if x != UNOBSERVED]
            mean[key] = sum(xs) / len(xs) if xs else 0.0
        return _Model({"*total"} | set(sorted((k for k in mean if k != "*total"), key=lambda k: (-mean[k], k))[:TOP]))

    def _profile(self, series) -> "numpy.ndarray":
        """Matrix-profile distances of window M; unobserved (-1) minutes are linearly filled first, no neighbour = 0."""
        np = self._np
        raw = np.asarray(series, dtype=float)
        seen = raw != UNOBSERVED
        t = np.interp(np.arange(len(raw)), np.flatnonzero(seen), raw[seen])
        return np.nan_to_num(self._stump(t, M)[:, 0].astype(float), nan=0.0, posinf=0.0), seen

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        np = self._np
        score = array("d", [0.0] * 1440)
        flags = {k: bytearray(1440) for k in knobs}
        counts = sd.minutes.get(key, {}).get(day)
        thr = {k: float("inf") for k in knobs}
        if key in model.top and counts is not None:
            per, prior = sd.minutes[key], self._prior(sd, day)
            hist = np.concatenate([np.array(per[d], dtype=float) for d in prior])
            if key not in model.dist:
                if (hist != UNOBSERVED).sum() < 2 * M:
                    model.dist[key] = []
                else:
                    model.dist[key] = sorted(self._profile(hist)[0].tolist())
            ds = model.dist[key]
            if ds:
                p, seen = self._profile(np.concatenate([hist, np.array(counts, dtype=float)]))
                start = len(hist)
                s = p[start - M + 1:start - M + 1 + 1440]
                score = array("d", np.where(seen[start:], s, 0.0).tolist())
                thr = {k: ds[min(len(ds) - 1, int(k * len(ds)))] for k in knobs}
        level = sd.level(key)
        for k in knobs:
            for slot in range(1440):
                if counts is not None and counts[slot] != UNOBSERVED and score[slot] > 0:
                    flags[k][slot] = apply_d4(counts[slot], None, thr[k], None, key, level, cfg, score=score[slot])
        return {k: DayOut(None, None, None, score, flags[k]) for k in knobs}

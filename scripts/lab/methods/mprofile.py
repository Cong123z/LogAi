"""Method #12: Matrix Profile (STUMPY), window 60, on the last 7 days + the scored day's per-minute series.

Score = z-normalised distance of the window ending at that minute to its nearest other window. Score method: no band.
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


class MProfile:
    spec = Spec(name="mprofile", number="#12", label="Matrix Profile (STUMPY)", kinds=frozenset({"M1", "M3"}),
                knobs=(2.0, 3.0, 4.0, 5.0, 6.0), default_knob=4.0, band=False, heavy=True, cost=5, min_minute_days=7)

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

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        np = self._np
        score = array("d", [0.0] * 1440)
        flags = {k: bytearray(1440) for k in knobs}
        counts = sd.minutes.get(key, {}).get(day)
        if key in model.top and counts is not None:
            per = sd.minutes[key]
            raw = np.concatenate([np.array(per[d], dtype=float) for d in self._prior(sd, day) + [day]])
            seen = raw != UNOBSERVED
            if seen.sum() >= 2 * M:
                t = np.interp(np.arange(len(raw)), np.flatnonzero(seen), raw[seen])     # unobserved: linear fill
                p = self._stump(t, M)[:, 0].astype(float)
                p = np.nan_to_num(p, nan=0.0, posinf=0.0)
                start = len(raw) - 1440
                s = p[start - M + 1:start - M + 1 + 1440]
                score = array("d", np.where(seen[start:], s, 0.0).tolist())
        level = sd.level(key)
        for k in knobs:
            for slot in range(1440):
                if counts is not None and counts[slot] != UNOBSERVED and score[slot] > 0:
                    flags[k][slot] = apply_d4(counts[slot], None, k, None, key, level, cfg, score=score[slot])
        return {k: DayOut(None, None, None, score, flags[k]) for k in knobs}

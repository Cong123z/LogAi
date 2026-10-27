"""E1, 2-of-3 vote of nb, cusum and weekly at their default knobs. The heavy forecasters are not used (this task is
pure Python); weekly stands in for the forecaster. A voter with no model does not vote; with fewer than two voters left
the vote cannot be reached, so fit returns None. Weekly votes on every key it is given (its own kinds are M1/M2)."""
from __future__ import annotations

from array import array
from datetime import date

from scripts.lab.data import ServiceData
from scripts.lab.methods import DayOut, Spec
from scripts.lab.methods.cusum import Cusum
from scripts.lab.methods.nb import NB
from scripts.lab.methods.weekly import Weekly


class Vote:
    spec = Spec(name="vote", number="E1", label="2-of-3 vote (nb, cusum, weekly)", kinds=frozenset({"M1", "M2", "M3", "M4", "M5"}),
                knobs=(2,), default_knob=2, band=False, heavy=False, cost=3)

    def __init__(self):
        self.voters = {"nb": NB(), "cusum": Cusum(), "weekly": Weekly()}

    def fit(self, sd: ServiceData, day: date, cfg):
        models = {n: mdl for n, v in self.voters.items() if (mdl := v.fit(sd, day, cfg)) is not None}
        return models if len(models) >= 2 else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        prev = prev_model or {}
        flags = []
        for n, mdl in model.items():
            v = self.voters[n]
            flags.append(v.day(mdl, prev.get(n), sd, key, day, (v.spec.default_knob,), cfg)[v.spec.default_knob].high_flag)
        votes = array("d", [float(sum(f[i] for f in flags)) for i in range(1440)])
        return {k: DayOut(None, None, None, votes, bytearray(int(v >= k) for v in votes)) for k in knobs}

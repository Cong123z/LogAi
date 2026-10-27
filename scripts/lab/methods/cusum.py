"""Method #6, Poisson CUSUM: S = max(0, S + x ln(l1/l0) - (l1 - l0)) with l0 = sigma's profile mean, l1 = 1.5 l0.

State does not carry across days: S starts at 0 each day. After S > h it resets to 0 (one alarm per crossing).
Minutes with no profile or l0 <= 0 are skipped (the log-ratio is undefined), as are unobserved minutes.
Default h = 7: at h = 4 a clean Poisson key of mean 20 alarmed 5.6 times a day (simulation in the bk09 PR file).
"""
from __future__ import annotations

import math
from array import array
from datetime import date

from logai2.owner.detect import VIEW_ONLY_KEYS
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, Spec, apply_d4
from scripts.lab.methods.sigma import rebuild_stats

RATIO = 1.5
LN = math.log(RATIO)


class Cusum:
    spec = Spec(name="cusum", number="#6", label="Poisson CUSUM", kinds=frozenset({"M1", "M2", "M3", "M4", "M5"}),
                knobs=(3.0, 5.0, 7.0, 10.0, 14.0, 20.0), default_knob=7.0, band=False, heavy=False, cost=1, min_hour_days=1)

    def fit(self, sd: ServiceData, day: date, cfg):
        model = rebuild_stats(sd, day)
        return model if model.days else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        h, m = cfg.baseline.commit_time.split(":")
        switch = int(h) * 60 + int(m)
        old = prev_model or model
        counts, level = sd.minutes[key][day], sd.level(key)
        view_only = key in VIEW_ONLY_KEYS
        lam: dict = {}

        def lam0(bl, hour):
            if (id(bl), hour) not in lam:
                lk = (bl.lookup_doc(sd.service, sd.docs[key], day, hour) if key.startswith("doc:")
                      else bl.lookup(sd.service, key, day, hour))
                lam[id(bl), hour] = lk.this.mean if lk.this else 0.0
            return lam[id(bl), hour]

        outs = {k: (array("d", [0.0] * 1440), bytearray(1440)) for k in knobs}
        s = dict.fromkeys(knobs, 0.0)
        for slot in range(1440):
            x = counts[slot]
            l0 = lam0(old if slot < switch else model, slot // 60)
            if x == UNOBSERVED or l0 <= 0:
                continue
            step = x * LN - (RATIO - 1) * l0
            for k, (score, flag) in outs.items():
                s[k] = max(0.0, s[k] + step)
                score[slot] = s[k]
                flag[slot] = apply_d4(x, None, k, None, key, level, cfg, score=s[k]) and not view_only
                if s[k] > k:
                    s[k] = 0.0
        return {k: DayOut(None, None, None, sc, fl) for k, (sc, fl) in outs.items()}

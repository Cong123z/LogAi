"""Method #1, today's sigma: the live engine's Baseline and judge, replayed on recorded counts.

Known limit: the lab uses templates.level and the doc links as of the load; live uses the most severe level seen so far
and the links of each minute (tests/lab/test_lab_sigma_adversarial.py::test_level_escalation_parity documents it).
"""
from __future__ import annotations

import math
from array import array
from dataclasses import replace
from datetime import date

from logai2.owner.baseline import DAY_CAP, Baseline, Stat, judge
from logai2.owner.detect import VIEW_ONLY_KEYS
from logai2.timeutil import daytype
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, Spec

NAN = float("nan")


def rebuild_stats(sd: ServiceData, day: date) -> Baseline:
    """What `commit()` (jobs/commit.py REBUILD) puts in `baseline` when it runs on owner day `day`. Pure."""
    committed = sorted({d for d, _ in sd.n_minutes if d < day}, reverse=True)      # MARK_DAYS: days with service_hours
    by_dt = {dt: [d for d in committed if daytype(d) == dt] for dt in DAY_CAP}
    days = {(sd.service, dt): min(len(ds), DAY_CAP[dt]) for dt, ds in by_dt.items() if ds}
    # hours[(dt, hour)] -> [(day, n_minutes)] of the picked days; n_minutes > 0 and not excluded.
    # ponytail: today's Review exclusions apply backwards (the table keeps no history of when they were made)
    hours: dict[tuple[str, int], list[tuple[date, int]]] = {}
    for dt, ds in by_dt.items():
        for d in ds[:DAY_CAP[dt]]:
            for h in range(24):
                n = sd.n_minutes.get((d, h), 0)
                if n > 0 and (d, h) not in sd.excluded:
                    hours.setdefault((dt, h), []).append((d, n))
    picked = {(d, h) for (_, h), hs in hours.items() for d, _ in hs}
    keys = {k for k, d, h in sd.hourly if (d, h) in picked}
    born = {}                                    # view-only level keys are new: earlier days are unknown, not zero
    for k, d, _ in sd.hourly:
        if k in VIEW_ONLY_KEYS:
            born[k] = min(born.get(k, d), d)
    stats = {}
    for key in keys:
        for (dt, h), hs in hours.items():
            hs = [(d, n) for d, n in hs if d >= born.get(key, date.min)]
            if not hs:
                continue
            n = sum(n for _, n in hs)
            s = sum(sd.hourly.get((key, d, h), (0, 0))[0] for d, _ in hs)
            sq = sum(sd.hourly.get((key, d, h), (0, 0))[1] for d, _ in hs)
            mean = s / n
            stats[(sd.service, key, dt, h)] = Stat(mean, math.sqrt(max(sq / n - mean ** 2, 0)))
    return Baseline(stats, days)


def _slot_of_commit(cfg) -> int:
    h, m = cfg.baseline.commit_time.split(":")
    return int(h) * 60 + int(m)


class Sigma:
    spec = Spec(name="sigma", number="#1", label="mean ± kσ (today)", kinds=frozenset({"M1", "M2", "M3", "M4", "M5", "M6"}),
                knobs=(2.0, 2.5, 3.0, 3.5, 4.0, 5.0), default_knob=3.0, band=True, heavy=False, cost=0, min_hour_days=1)

    def fit(self, sd: ServiceData, day: date, cfg) -> Baseline | None:
        model = rebuild_stats(sd, day)
        return model if model.days else None

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg):
        b = cfg.baseline
        ratio = b.young_sigma_multiplier / b.sigma_multiplier
        cfgs = {k: replace(cfg, baseline=replace(b, sigma_multiplier=k, young_sigma_multiplier=k * ratio)) for k in knobs}
        counts = sd.minutes[key][day]
        level = sd.level(key)
        switch = _slot_of_commit(cfg)
        old = prev_model or model
        view_only = key in VIEW_ONLY_KEYS
        outs = {k: (array("d", [NAN] * 1440), array("d", [NAN] * 1440), array("d", [NAN] * 1440),
                    array("d", [0.0] * 1440), bytearray(1440)) for k in knobs}
        for hour in range(24):
            for bl, lo, hi in ((old, hour * 60, min(switch, hour * 60 + 60)), (model, max(switch, hour * 60), hour * 60 + 60)):
                if lo >= hi:
                    continue
                lk = (bl.lookup_doc(sd.service, sd.docs[key], day, hour) if key.startswith("doc:")
                      else bl.lookup(sd.service, key, day, hour))
                exp = lk.this.mean if lk.this else NAN
                for slot in range(lo, hi):
                    n = counts[slot]
                    if n == UNOBSERVED:
                        continue
                    for k, (e, l, h, s, f) in outs.items():
                        v = judge(n, key, level, lk, cfgs[k])
                        e[slot] = exp
                        if v.band_high is not None:
                            l[slot], h[slot] = v.band_low, v.band_high
                        s[slot] = v.score
                        f[slot] = v.high and not view_only
        return {k: DayOut(*o) for k, o in outs.items()}

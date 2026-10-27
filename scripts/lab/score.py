"""The scoring sink (decision D4/D5, spec 9): Test B metrics, episodes and the drawn bands, streamed from the runner.

Memory: the sink keeps only accumulators (kind x knob x part, plus kind x knob x test day), one episode Tracker per
(key, knob) with its episodes, and the day rows of the drawn keys (at most 3 per kind plus 5 incident keys, 1440 values
on a day with a high flag, 288 otherwise). Nothing else of the runner's per-day output is kept; every write is in end().
"""
from __future__ import annotations

import math
from datetime import timedelta

from logai2.links import level_class
from scripts.lab import store
from scripts.lab.data import UNOBSERVED, minute_at, slot_of
from scripts.lab.episodes import Tracker
from scripts.lab.kinds import has_new, is_new

ALPHA = 0.005
MARGIN = 15                # minutes around an incident that are not normal
FLAP_SHORT, FLAP_GAP = 5, 30
LO, HI, BINS = 1e-4, 1e4, 600          # sharpness histogram: log-spaced bins over [LO, HI) plus under/overflow
NBINS = BINS + 2
_LOG = math.log(HI / LO)
LEVEL_KEY = {"ERROR": "*error", "WARN": "*warn", "INFO": "*info"}
N_DRAWN, N_DRAWN_INCIDENT = 3, 5


def winkler(y, low, high, alpha) -> float:
    return (high - low) + (2 / alpha) * max(low - y, 0) + (2 / alpha) * max(y - high, 0)


def bin_of(v: float) -> int:
    if v < LO:
        return 0
    if v >= HI:
        return NBINS - 1
    return 1 + min(int(math.log(v / LO) / _LOG * BINS), BINS - 1)


def median_from_hist(hist) -> float | None:
    """Approximate median: the geometric middle of the bin holding the middle value (error under 2 %, 3 % bin width);
    under/overflow report the range ends."""
    total = sum(hist)
    if not total:
        return None
    cum = 0
    for i, c in enumerate(hist):
        cum += c
        if 2 * cum >= total:
            return LO if i == 0 else HI if i == NBINS - 1 else LO * math.exp(_LOG * (i - 0.5) / BINS)


class Acc:
    """Sums for one (kind, knob, part), or one test day (hist=False)."""
    __slots__ = ("kind", "n", "normal", "covered", "wink", "mase_n", "e7", "a7", "n7", "e1", "a1", "n1",
                 "episodes", "flaps", "hist")

    def __init__(self, kind="", hist=False):
        self.kind = kind
        self.n = self.normal = self.covered = self.mase_n = self.n7 = self.n1 = self.episodes = self.flaps = 0
        self.wink = self.e7 = self.a7 = self.e1 = self.a1 = 0.0
        self.hist = [0] * NBINS if hist else None


def naive_of(acc: Acc) -> str | None:
    """7d when every minute with an expected value had it, else 1d if any minute did."""
    return "7d" if acc.mase_n and acc.n7 == acc.mase_n else "1d" if acc.n1 else None


def _naive_sums(acc: Acc, naive):
    return (acc.e7, acc.a7) if naive == "7d" else (acc.e1, acc.a1) if naive == "1d" else (0.0, 0.0)


def metrics_from(acc: Acc) -> dict:
    """Pure: the metrics dict M of one accumulator (null where undefined)."""
    naive = naive_of(acc)
    e, a = _naive_sums(acc, naive)
    key_days = acc.normal / 1440
    per, scale = (acc.normal / 10080, 1) if acc.kind == "M1" else (key_days, 100)     # M1: per service-week
    return {"n_minutes": acc.n,
            "coverage": acc.covered / acc.n if acc.n else None,
            "interval_score": acc.wink / acc.n if acc.n else None,
            "sharpness": median_from_hist(acc.hist) if acc.hist else None,
            "mase": e / a if naive and a > 0 else None, "mase_naive": naive,
            "alert_rate": acc.episodes / per * scale if per else None,
            "flap_share": acc.flaps / acc.episodes if acc.episodes else None,
            "episodes": acc.episodes, "key_days": key_days}


def day_from(acc: Acc, naive) -> dict:
    e, a = _naive_sums(acc, naive)
    return {"n": acc.n, "covered": acc.covered, "winkler": acc.wink, "abs_err": e, "abs_naive": a,
            "episodes": acc.episodes, "flaps": acc.flaps, "key_days": acc.normal / 1440}


def _mean5(vals):
    ok = [v for v in vals if v == v]
    return sum(ok) / len(ok) if ok else None


class ScoreSink:
    def __init__(self, conn, run_id, service, method, spec, kinds, ctx):
        self.conn, self.run_id, self.service, self.method, self.spec = conn, run_id, service, method, spec
        self.kinds, self.ctx, self.sd = kinds, ctx, ctx.sd
        self.knobs = tuple(spec.knobs)
        self.acc = {(kd, k, p): Acc(kd, hist=spec.band) for kd in spec.kinds for k in self.knobs
                    for p in ("tune", "test")}
        self.day_acc: dict[tuple, Acc] = {}
        self.trackers: dict[tuple, Tracker] = {}
        self.episodes: dict[tuple, list] = {}
        self.band_rows: dict[tuple, list] = {}
        self.last_day: dict[str, object] = {}
        self.added: set[str] = set()
        self.nb = {kd: [0, 0] for kd in spec.kinds}            # no-band minutes, shared normal minutes
        self._mean: dict[str, float] = {}
        self._win: dict[str, list] = {}
        self._built = None
        self.keys_of = {kd: [k for k, v in kinds.items() if v == kd or (kd == "M6" and has_new(self.sd, k))]
                        for kd in spec.kinds}
        self.drawn = self._drawn()

    # -- per key facts ------------------------------------------------------------------------------------------
    def mean_count(self, key) -> float:
        """Mean count per observed minute over the whole window (below 1 -> 1): the Winkler normalizer."""
        if key not in self._mean:
            tot = obs = 0
            for a in self.sd.minutes[key].values():
                u = a.count(UNOBSERVED)
                tot, obs = tot + sum(a) + u, obs + len(a) - u
            self._mean[key] = tot / obs if obs else 0.0
        return self._mean[key]

    def windows(self, key) -> list:
        """[lo, hi) minute ranges around the incidents that cover `key` (an incident of NULL or *total covers all)."""
        if key not in self._win:
            sd, w = self.sd, []
            for inc in sd.incidents:
                lvl = LEVEL_KEY[level_class(sd.levels.get(inc.key))] if inc.key else None
                if (inc.key in (None, "*total", key) or key == "*total" or key == lvl
                        or (key.startswith("doc:") and inc.key in sd.docs.get(key, []))):
                    w.append((inc.start - MARGIN, inc.end + MARGIN))
            self._win[key] = w
        return self._win[key]

    def bad_mask(self, key, day) -> bytearray:
        """1 where the minute is not shared-normal for reasons other than being unobserved (excluded hour, incident)."""
        bad, base = bytearray(1440), minute_at(day, 0)
        for h in range(24):
            if (day, h) in self.sd.excluded:
                bad[h * 60:h * 60 + 60] = b"\1" * 60
        for lo, hi in self.windows(key):
            a, b = max(lo - base, 0), min(hi - base, 1440)
            if a < b:
                bad[a:b] = b"\1" * (b - a)
        return bad

    def kind_at(self, key, minute) -> str | None:
        if is_new(self.sd, key, minute):
            return "M6"
        base = self.kinds.get(key)
        return None if base == "M6" else base

    def _drawn(self) -> set:
        sd, drawn = self.sd, set()
        for kd in self.spec.kinds:
            top = sorted(self.keys_of[kd], key=lambda k: (-self.mean_count(k), k))[:N_DRAWN]
            drawn.update(top)
        walked = {k for ks in self.keys_of.values() for k in ks}
        named = []
        for inc in sorted(sd.incidents, key=lambda i: i.start):
            if inc.key in walked and inc.key not in named:
                named.append(inc.key)
        drawn.update(named[:N_DRAWN_INCIDENT])
        return drawn

    # -- the sink contract --------------------------------------------------------------------------------------
    def skip(self, day) -> None:
        pass

    def add(self, key, day, outs) -> None:
        assert key not in self.last_day or day > self.last_day[key], f"{key}: day {day} out of order"
        self.last_day[key] = day
        self.added.add(key)
        sd, spec = self.sd, self.spec
        arr = sd.minutes[key][day]
        base0 = minute_at(day, 0)
        bad = self.bad_mask(key, day)
        n7a = sd.minutes[key].get(day - timedelta(7))
        n1a = sd.minutes[key].get(day - timedelta(1))
        part = self.ctx.part(key, day)
        mean = max(self.mean_count(key), 1.0)
        observed, scored = [], []
        for slot in range(1440):
            y = arr[slot]
            if y == UNOBSERVED:
                continue
            observed.append(slot)
            if not bad[slot]:
                kd = self.kind_at(key, base0 + slot)
                if kd in spec.kinds:
                    scored.append((slot, kd, y, n7a[slot] if n7a is not None else UNOBSERVED,
                                   n1a[slot] if n1a is not None else UNOBSERVED))
        c = 2 / ALPHA
        for knob, o in outs.items():
            accs = {kd: (self.acc[kd, knob, part], self._day(kd, knob, day) if part == "test" else None)
                    for kd in spec.kinds}
            tr = self.trackers.get((key, knob))
            if tr is None:
                tr = self.trackers[key, knob] = Tracker(self.ctx.cfg.alert)
                self.episodes[key, knob] = []
            for slot in observed:
                ep = tr.step(base0 + slot, bool(o.high_flag[slot]), o.score[slot])
                if ep:
                    self.episodes[key, knob].append(ep)
            count_nb = knob == spec.default_knob or knob == self.knobs[0] and spec.default_knob not in outs
            for slot, kd, y, n7, n1 in scored:
                a, d = accs[kd]
                a.normal += 1
                if d:
                    d.normal += 1
                if not spec.band:
                    continue
                if count_nb:
                    self.nb[kd][1] += 1
                lo, hi = o.low[slot], o.high[slot]
                if lo != lo or hi != hi:
                    if count_nb:
                        self.nb[kd][0] += 1
                    continue
                cov = lo <= y <= hi
                w = ((hi - lo) + c * max(lo - y, 0) + c * max(y - hi, 0)) / mean
                for t in (a, d):
                    if t:
                        t.n += 1
                        t.covered += cov
                        t.wink += w
                ex = o.expected[slot]
                if ex != ex:
                    continue
                a.hist[bin_of((hi - lo) / max(ex, 1e-9))] += 1
                err = abs(ex - y)
                for t in (a, d):
                    if t:
                        t.mase_n += 1
                        if n7 != UNOBSERVED:
                            t.n7, t.e7, t.a7 = t.n7 + 1, t.e7 + err, t.a7 + abs(n7 - y)
                        if n1 != UNOBSERVED:
                            t.n1, t.e1, t.a1 = t.n1 + 1, t.e1 + err, t.a1 + abs(n1 - y)
        if key in self.drawn:
            self._draw(key, day, arr, outs)

    def _day(self, kind, knob, day) -> Acc:
        a = self.day_acc.get((kind, knob, day))
        if a is None:
            a = self.day_acc[kind, knob, day] = Acc(kind)
        return a

    def _draw(self, key, day, arr, outs) -> None:
        counts5 = None
        for knob, o in outs.items():
            rows = self.band_rows.setdefault((key, knob), [])
            if any(o.high_flag):
                f = [bool(x) for x in o.high_flag]
                nan = lambda v: None if v != v else float(v)       # noqa: E731
                rows.append((day, 1, list(arr), None if o.expected is None else [nan(v) for v in o.expected],
                             None if o.low is None else [nan(v) for v in o.low],
                             None if o.high is None else [nan(v) for v in o.high], f))
                continue
            if counts5 is None:
                counts5 = []
                for i in range(0, 1440, 5):
                    seen = [x for x in arr[i:i + 5] if x != UNOBSERVED]
                    counts5.append(round(sum(seen) / 5) if seen else UNOBSERVED)
            mean5 = lambda a: None if a is None else [_mean5(a[i:i + 5]) for i in range(0, 1440, 5)]   # noqa: E731
            rows.append((day, 5, counts5, mean5(o.expected), mean5(o.low), mean5(o.high),
                         [any(o.high_flag[i:i + 5]) for i in range(0, 1440, 5)]))

    # -- results ------------------------------------------------------------------------------------------------
    def _count_episode(self, key, knob, ep, prev_end) -> None:
        day, slot = slot_of(ep.start)
        kd = self.kind_at(key, ep.start)
        if kd not in self.spec.kinds or self.bad_mask(key, day)[slot]:
            return
        flap = ep.end - ep.start < FLAP_SHORT or (prev_end is not None and ep.start - prev_end < FLAP_GAP)
        part = self.ctx.part(key, day)
        targets = [self.acc[kd, knob, part]] + ([self._day(kd, knob, day)] if part == "test" else [])
        for t in targets:
            t.episodes += 1
            t.flaps += flap

    def build(self) -> dict:
        """kind -> the lab_results metrics. Closes the open episodes at the window end; call once."""
        if self._built is not None:
            return self._built
        end = minute_at(self.sd.days[-1], 0) + 1440
        for (key, knob), tr in self.trackers.items():
            eps = self.episodes[key, knob]
            if (ep := tr.close(end)):
                eps.append(ep)
            prev_end = None
            for ep in eps:
                self._count_episode(key, knob, ep, prev_end)
                prev_end = ep.end
        out = {}
        for kd in sorted(self.spec.kinds):
            knobs, days = {}, {}
            for k in self.knobs:
                knobs[str(k)] = {p: metrics_from(self.acc[kd, k, p]) for p in ("tune", "test")}
                naive = naive_of(self.acc[kd, k, "test"])
                days[str(k)] = {d.isoformat(): day_from(a, naive) for (kind, kn, d), a in sorted(self.day_acc.items())
                                if kind == kd and kn == k}
            nb, den = self.nb[kd]
            out[kd] = {"n_keys": len(self.keys_of[kd]), "no_band_share": nb / den if self.spec.band and den else None,
                       "knobs": knobs, "days": days}
        self._built = out
        return out

    def end(self) -> None:
        out, conn = self.build(), self.conn
        with conn.transaction():
            for kd, m in out.items():
                ok = any(k in self.added for k in self.keys_of[kd])
                store.put_result(conn, self.run_id, self.method, kd, self.service, None, "ok" if ok else "skipped", m)
            for (key, knob), eps in sorted(self.episodes.items()):
                if eps:
                    store.put_episodes(conn, self.run_id, self.method, knob, self.service, key,
                                       [(e.start, e.end, e.peak_score, e.alerted) for e in eps])
            for (key, knob), rows in sorted(self.band_rows.items()):
                store.put_bands(conn, self.run_id, self.method, knob, self.service, key, rows)


def score_collect(results_conn, run_id, service, method, spec, kinds, ctx) -> ScoreSink:
    return ScoreSink(results_conn, run_id, service, method, spec, kinds, ctx)

"""bk08: #2 nb, #3 nb_clip, #4 mad, #7 weekly on hand-made ServiceData (no DB)."""
import math
import random
from array import array
from datetime import date, timedelta

import pytest

import scripts.lab.methods as methods
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.holidays import is_holiday
from scripts.lab.methods.nb import nb_quantiles
from scripts.lab.methods.sigma import Sigma

D = date(2026, 10, 12)          # Monday
START = date(2026, 9, 14)       # Monday, 28 days of history before D
LEVELS = {"E": "ERROR"}
ALL = ["nb", "nb_clip", "mad", "weekly"]


def poisson(rng, lam):
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def make(means, start=START, end=D + timedelta(days=3), mods=(), seed=1, excluded=()):
    """means: key -> fn(day, slot) -> lambda. mods: (key, day, lo, hi, lambda) overrides, or (key, day, slot, None, count)."""
    rng = random.Random(seed)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    minutes = {k: {d: array("i", [poisson(rng, fn(d, s)) for s in range(1440)]) for d in days} for k, fn in means.items()}
    for key, d, lo, hi, v in mods:
        if hi is None:
            minutes[key][d][lo] = v
        else:
            for s in range(lo, hi):
                minutes[key][d][s] = poisson(rng, v)
    hourly = {}
    for k, per in minutes.items():
        for d, a in per.items():
            for h in range(24):
                seg = a[h * 60:(h + 1) * 60]
                hourly[(k, d, h)] = (sum(seg), sum(x * x for x in seg))
    return ServiceData("s", days, minutes, {d: array("i", [1] * 1440) for d in days}, hourly,
                       {(d, h): 60 for d in days for h in range(24)}, set(), set(excluded), dict(LEVELS), {}, {})


def flat(lam):
    return lambda d, s: lam


def run(name, sd, key, day, cfg, knob=None):
    m = methods.METHODS[name]()
    model = m.fit(sd, day, cfg)
    assert model is not None
    knob = m.spec.default_knob if knob is None else knob
    return m.day(model, None, sd, key, day, (knob,), cfg)[knob]


def test_registered():
    for name, num in zip(ALL, ("#2", "#3", "#4", "#7")):
        assert name in methods.METHODS and name in methods.GROUPS["core"]
        sp = methods.METHODS[name]().spec
        assert sp.number == num and sp.band and not sp.heavy and len(sp.knobs) == 6 and sp.default_knob in sp.knobs
    assert methods.METHODS["nb"]().spec.default_knob == 1e-3 and methods.METHODS["nb_clip"]().spec.cost == 2
    assert methods.METHODS["mad"]().spec.min_minute_days == 7 and methods.METHODS["weekly"]().spec.min_hour_days == 14
    assert methods.METHODS["mad"]().spec.default_knob == 5 and methods.METHODS["weekly"]().spec.kinds == {"M1", "M2"}


@pytest.mark.parametrize("name", ALL)
def test_clean_poisson_flags_at_most_one_percent(name, cfg):
    sd = make({"E": flat(5)})
    o = run(name, sd, "E", D, cfg)
    assert sum(o.high_flag) <= 14


@pytest.mark.parametrize("name", ALL)
def test_spike_x3_is_flagged(name, cfg):
    sd = make({"E": flat(20)}, mods=[("E", D, 600, 610, 60)])
    assert sum(run(name, sd, "E", D, cfg).high_flag[600:610]) >= 8


@pytest.mark.parametrize("name", ["nb", "nb_clip", "weekly"])
def test_total_drop_is_flagged(name, cfg):
    sd = make({"*total": flat(30)}, mods=[("*total", D, 600, 630, 9)])
    assert sum(run(name, sd, "*total", D, cfg).high_flag[600:630]) >= 24


@pytest.mark.parametrize("name,days", [("nb", 0), ("nb_clip", 0), ("mad", 5), ("weekly", 10)])
def test_not_enough_history_is_none(name, days, cfg):
    sd = make({"E": flat(5)}, start=D - timedelta(days=days), end=D)
    assert methods.METHODS[name]().fit(sd, D, cfg) is None


@pytest.mark.parametrize("name", ALL)
def test_unobserved_minute_and_view_only(name, cfg):
    sd = make({"E": flat(5), "*warn": flat(5)}, mods=[("*warn", D, 600, 610, 500)])
    sd.minutes["E"][D][7] = UNOBSERVED
    o = run(name, sd, "E", D, cfg)
    assert math.isnan(o.low[7]) and math.isnan(o.high[7]) and math.isnan(o.expected[7])
    assert o.score[7] == 0 and o.high_flag[7] == 0 and not math.isnan(o.high[8])
    assert not any(run(name, sd, "*warn", D, cfg).high_flag)


@pytest.mark.parametrize("name", ALL)
def test_no_look_ahead(name, cfg):
    a = make({"E": flat(8)})
    b = make({"E": flat(8)}, mods=[("E", D + timedelta(days=i), s, None, 777) for i in (0, 1, 2) for s in range(0, 1440, 5)])
    oa, ob = run(name, a, "E", D, cfg), run(name, b, "E", D, cfg)
    for f in ("expected", "low", "high"):
        assert repr(getattr(oa, f).tolist()) == repr(getattr(ob, f).tolist())


@pytest.mark.parametrize("name", ["nb", "nb_clip", "mad", "weekly"])
def test_every_knob_returned_and_wider_for_stricter_knob(name, cfg):
    sd = make({"E": flat(20)})
    m = methods.METHODS[name]()
    out = m.day(m.fit(sd, D, cfg), None, sd, "E", D, m.spec.knobs, cfg)
    assert set(out) == set(m.spec.knobs)
    ks = m.spec.knobs
    hi = [out[k].high[300] for k in ks]
    assert (hi == sorted(hi)) or (hi == sorted(hi, reverse=True))      # monotone in the knob
    assert hi[0] != hi[-1]


# --- #2 / #3 --------------------------------------------------------------------------------------------

def brute(mu, var, p):
    """Cumulative sums from a different pmf formula (direct, via exp(lgamma) in a plain loop)."""
    if var > mu:
        r = mu * mu / (var - mu)
        pmf = [math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)) * (r / (r + mu)) ** r * (mu / (r + mu)) ** k
               for k in range(4000)]
    else:
        pmf = [math.exp(-mu) * mu ** k / math.factorial(k) for k in range(170)]
    high = next(c for c in range(len(pmf)) if sum(pmf[c + 1:]) <= p)
    low = max(c for c in range(len(pmf)) if sum(pmf[:c]) <= p)
    return low, high


@pytest.mark.parametrize("mu,var,p", [(5, 12, 1e-3), (0.4, 3.0, 1e-2), (50, 200, 1e-4), (3, 40, 3e-3), (12, 20, 1e-5)])
def test_nb_quantiles_match_brute_force(mu, var, p):
    assert nb_quantiles(mu, var, p) == brute(mu, var, p)


@pytest.mark.parametrize("var", [3.0, 5.0, 0.0])
def test_poisson_fallback_when_var_not_above_mean(var):
    assert nb_quantiles(5.0, var, 1e-3) == brute(5.0, 5.0, 1e-3)


def test_nb_quantile_zero_mean():
    assert nb_quantiles(0.0, 0.0, 1e-3) == (0, 0)


def test_nb_no_stats_key_has_nan_band(cfg):
    sd = make({"E": flat(5), "new": flat(5)})
    sd.hourly = {k: v for k, v in sd.hourly.items() if k[0] != "new"}
    o = run("nb", sd, "new", D, cfg)
    assert math.isnan(o.high[10]) and math.isnan(o.expected[10]) and not any(o.high_flag)


def test_nb_expected_is_profile_mean(cfg):
    o = run("nb", make({"E": flat(20)}), "E", D, cfg)
    assert o.expected[300] == pytest.approx(20, abs=1.0)


def test_nb_clip_narrows_after_past_incident(cfg):
    sd = make({"E": flat(5)}, mods=[("E", D - timedelta(days=3), 600, None, 60)])
    plain, clipped = run("nb", sd, "E", D, cfg), run("nb_clip", sd, "E", D, cfg)
    assert clipped.high[600] < plain.high[600]
    assert clipped.high[100] <= plain.high[100] + 1      # other hours barely change


def test_nb_clip_falls_back_without_minute_data(cfg):
    sd = make({"E": flat(5)})
    sd.minutes["E"].pop(D - timedelta(days=1))
    sd.minutes["E"].pop(D - timedelta(days=3))
    sd.minutes["E"].pop(D - timedelta(days=4))
    # picked weekdays now partly lack per-minute data: #3 equals #2 for those hours
    assert run("nb_clip", sd, "E", D, cfg).high[300] == run("nb", sd, "E", D, cfg).high[300]


# --- #4 -------------------------------------------------------------------------------------------------

def test_mad_ignores_one_huge_minute_but_sigma_mean_moves(cfg):
    clean = make({"E": flat(5)})
    dirty = make({"E": flat(5)}, mods=[("E", D - timedelta(days=3), 605, None, 10000)])
    assert run("mad", dirty, "E", D, cfg).expected[600] == run("mad", clean, "E", D, cfg).expected[600]
    sig = lambda sd: Sigma().day(Sigma().fit(sd, D, cfg), None, sd, "E", D, (3.0,), cfg)[3.0].expected[600]
    assert sig(dirty) - sig(clean) > 10


def test_mad_band_formula_and_floor_at_zero(cfg):
    sd = make({"E": flat(0)})
    for a in sd.minutes["E"].values():       # constant 4 per minute: MAD 0 -> max(MAD, 1)
        for i in range(1440):
            a[i] = 4
    o = run("mad", sd, "E", D, cfg, knob=3)
    assert o.expected[10] == 4 and o.high[10] == pytest.approx(4 + 3 * 1.4826) and o.low[10] == 0.0       # 4 - 4.45 clipped
    o = run("mad", sd, "E", D, cfg, knob=1)
    assert o.low[10] == pytest.approx(4 - 1.4826)


# --- #7 -------------------------------------------------------------------------------------------------

def test_is_holiday_accepts_date_and_iso_string(cfg):
    cfg.lab.holidays = [date(2026, 9, 2), "2026-04-30"]
    assert is_holiday(date(2026, 9, 2), cfg) and is_holiday(date(2026, 4, 30), cfg)
    assert not is_holiday(date(2026, 5, 1), cfg)
    assert is_holiday(date(2026, 5, 1), cfg, extra=("2026-05-01",)) and is_holiday(date(2026, 5, 2), cfg, extra=[date(2026, 5, 2)])


def test_weekly_monday_and_thursday_profiles_differ(cfg):
    def mean(d, s):
        return 40 if d.weekday() == 3 and 600 <= s < 660 else 20
    sd = make({"*total": mean})
    mon = run("weekly", sd, "*total", D, cfg)
    thu = run("weekly", sd, "*total", D + timedelta(days=3), cfg)
    assert thu.expected[630] > mon.expected[630] + 10
    assert abs(thu.expected[100] - mon.expected[100]) < 5


def week_total(d, s):
    return 12 if d.weekday() == 6 else 30      # Sundays are quiet


def test_weekly_holiday_uses_sunday_profile_and_stays_quiet_but_nb_flags(cfg):
    cfg.lab.holidays = [D]
    sd = make({"*total": week_total}, mods=[("*total", D, 0, 1440, 12)])
    w = run("weekly", sd, "*total", D, cfg)
    assert sum(w.high_flag) == 0 and w.expected[300] == pytest.approx(12, abs=1.5)
    assert sum(run("nb", sd, "*total", D, cfg).high_flag) >= 1000          # #2 calls the whole day a drop


def test_weekly_uses_past_holiday_profile(cfg):
    past = date(2026, 10, 7)             # a Wednesday holiday, quiet
    cfg.lab.holidays = [past, D]
    sd = make({"*total": lambda d, s: 12 if d == past else 30}, mods=[("*total", D, 0, 1440, 12)])
    w = run("weekly", sd, "*total", D, cfg)
    assert w.expected[300] == pytest.approx(12, abs=1.5)
    # the holiday is not part of the Wednesday profile
    wed = run("weekly", sd, "*total", D + timedelta(days=2), cfg)
    assert wed.expected[300] == pytest.approx(30, abs=1.5)


def test_weekly_excluded_hour_is_left_out(cfg):
    ex = {(D - timedelta(days=7), 5)}
    sd = make({"E": flat(20)}, mods=[("E", D - timedelta(days=7), 300, 360, 200)], excluded=ex)
    assert run("weekly", sd, "E", D, cfg).expected[310] == pytest.approx(20, abs=2)


def test_commit_time_switch_uses_prev_model(cfg):
    sd = make({"E": flat(5)})
    m = methods.METHODS["nb"]()
    new, old = m.fit(sd, D, cfg), m.fit(sd, D - timedelta(days=1), cfg)
    mid = methods.METHODS["nb"]().day(new, old, sd, "E", D, (1e-3,), cfg)[1e-3]
    a = methods.METHODS["nb"]().day(old, None, sd, "E", D, (1e-3,), cfg)[1e-3]
    b = methods.METHODS["nb"]().day(new, None, sd, "E", D, (1e-3,), cfg)[1e-3]
    assert mid.high[60] == a.high[60] and mid.high[1000] == b.high[1000]


# --- fix round 1: drop guards and _picks ----------------------------------------------------------------

@pytest.mark.parametrize("name", ["nb", "nb_clip"])
@pytest.mark.parametrize("start,day,flagged", [
    (date(2026, 9, 14), D, True),                    # full weekday baseline
    (date(2026, 10, 12), date(2026, 10, 17), False),  # Saturday with no weekend history: borrows weekdays
    (date(2026, 10, 8), D, False),                   # 2 weekdays: young baseline
])
def test_total_drop_guards(name, start, day, flagged, cfg):
    sd = make({"*total": flat(30)}, start=start, end=day + timedelta(days=1), mods=[("*total", day, 600, 630, 9)])
    o = run(name, sd, "*total", day, cfg)
    assert (sum(o.high_flag[600:630]) >= 24) == flagged and (flagged or sum(o.high_flag) == 0)
    assert not math.isnan(o.low[610])                # the plotted band keeps its low


def test_picks_equal_sigma_picks(cfg):
    from scripts.lab.methods.nb import _picks
    from scripts.lab.methods.sigma import rebuild_stats
    from logai2.timeutil import daytype
    sd = make({"k": flat(1)}, start=date(2026, 9, 28), end=date(2026, 10, 14))
    for d in (date(2026, 10, 1), date(2026, 10, 6)):             # gaps
        for h in range(24):
            sd.n_minutes.pop((d, h), None)
    for d, h in ((date(2026, 10, 7), 9), (date(2026, 10, 3), 4)):  # n_minutes = 0
        sd.n_minutes[d, h] = 0
    sd.excluded |= {(date(2026, 10, 8), 10), (date(2026, 10, 4), 4)}
    # one distinct hourly sum per (day, hour): the profile mean tells which days sigma used
    for (k, d, h) in list(sd.hourly):
        sd.hourly[k, d, h] = (d.toordinal() * 24 + h, 0)
    for day in (date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 14)):
        got = rebuild_stats(sd, day).stats
        picks = _picks(sd, day)
        want = {("s", "k", dt, h): sum(sd.hourly["k", d, h][0] for d in ds) / sum(sd.n_minutes[d, h] for d in ds)
                for (dt, h), ds in picks.items()}
        assert {k: v.mean for k, v in got.items() if k[1] == "k"} == pytest.approx(want, abs=1e-9, rel=0) and want
        assert all(d < day and daytype(d) == dt and (d, h) not in sd.excluded and sd.n_minutes[d, h] > 0
                   for (dt, h), ds in picks.items() for d in ds)

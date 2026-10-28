"""Heavy methods #8-#12 (bk11). Package-bound tests skip where the package is missing (the app venv)."""
import importlib.util
import math
import subprocess
import sys
from array import array
from datetime import date, timedelta

import pytest

import scripts.lab.methods as methods
from logai2.config import AppConfig
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.methods import DayOut, Unavailable, heavy

NAN = float("nan")
D = date(2026, 10, 26)
PKG = {"ets": "statsmodels", "prophet": "prophet", "lgbm": "lightgbm", "chronos": "chronos", "mprofile": "stumpy"}
HEAVY = list(PKG)
BAND = ["ets", "prophet", "lgbm", "chronos"]
CFG = AppConfig()


def have(name):
    return importlib.util.find_spec(PKG[name]) is not None


# ---- registry / availability (app venv) ----------------------------------------------------------------------

def test_registered():
    assert methods.GROUPS["heavy"] == HEAVY
    assert all(n in methods.METHODS for n in HEAVY)


@pytest.mark.parametrize("name", HEAVY)
def test_factory_unavailable(name):
    if have(name):
        pytest.skip("package installed")
    with pytest.raises(Unavailable, match="not installed"):
        methods.METHODS[name]()


def test_check_lists_all_unavailable(capsys):
    if any(have(n) for n in HEAVY):
        pytest.skip("a heavy package is installed")
    from scripts.lab.run import main
    assert main(["--site", "laptop", "--test", "B", "--methods", "heavy", "--days", "1", "--check"]) == 1
    out = capsys.readouterr().out
    assert all(f"{n}: unavailable" in out for n in HEAVY)


def test_core_imports_no_heavy_package():
    code = ("import sys, scripts.lab.methods as m\n"
            "[m.METHODS[n]() for n in m.GROUPS['core']]\n"
            "bad = {'statsmodels', 'prophet', 'lightgbm', 'torch', 'chronos', 'stumpy', 'numpy', 'pandas'} & set(sys.modules)\n"
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)


# ---- heavy.py (pure) -----------------------------------------------------------------------------------------

def tiny_sd(minutes=None, hourly=None, n=None, excluded=()):
    days = [D - timedelta(k) for k in range(8, -1, -1)]
    return ServiceData("s", days, minutes or {}, {}, hourly or {}, n or {}, set(), set(excluded), {}, {}, {})


def test_fill_interpolates_and_extends():
    assert heavy.fill([NAN, 2.0, NAN, NAN, 8.0, NAN]) == [2.0, 2.0, 4.0, 6.0, 8.0, 8.0]
    assert all(math.isnan(x) for x in heavy.fill([NAN, NAN]))


def test_hourly_series_marks_missing_and_excluded_and_never_reads_day():
    d1 = D - timedelta(1)
    n = {(d1, 0): 60, (d1, 1): 60, (d1, 2): 0, (d1, 3): 60, (D, 0): 60}
    h = {("k", d1, 0): (120, 0), ("k", d1, 3): (60, 0), ("k", D, 0): (9999, 0)}
    sd = tiny_sd(hourly=h, n=n, excluded=[(d1, 3)])
    s = heavy.hourly_series(sd, "k", D, 1)
    assert len(s) == 24
    assert s[0] == 2.0 and s[1] == 0.0                   # absent key-hour in an observed hour = 0
    assert math.isnan(s[2]) and math.isnan(s[3]) and math.isnan(s[23])   # no minutes / excluded / nothing


def test_hist_days():
    sd = tiny_sd(n={(D - timedelta(1), 3): 60, (D - timedelta(2), 3): 0, (D - timedelta(3), 5): 10, (D, 1): 60})
    assert heavy.hist_days(sd, D) == 2


def test_shape_flat_without_history_and_normalised_with():
    sd = tiny_sd(minutes={"k": {}})
    assert heavy.shape(sd, "k", D) == [1.0] * 1440
    a = array("i", [0] * 1440)
    for s in range(60, 120):
        a[s] = 10 if s < 90 else 30                       # hour 1: mean 20 -> shape 0.5 / 1.5
    a[0] = UNOBSERVED
    sd = tiny_sd(minutes={"k": {D - timedelta(1): a}})
    sh = heavy.shape(sd, "k", D)
    assert sh[60] == pytest.approx(0.5) and sh[119] == pytest.approx(1.5)
    assert sh[0] == 1.0 and sh[200] == 1.0                # hour 0 mean is 0 -> flat; hour 3 empty -> flat


def test_widen():
    assert heavy.widen(3.0, 2.0, 4.0) == pytest.approx(math.sqrt(9 + 16))
    assert heavy.widen(0.0, 2.0, 0.0) == 0.0


def test_to_day_scales_flags_and_nan():
    a = array("i", [10] * 1440)
    a[5] = 400                                            # way above
    a[6] = UNOBSERVED
    sd = tiny_sd(minutes={"*error": {D: a}})
    sh = [1.0] * 1440
    sh[7] = 2.0
    exp, lo, hi = [10.0] * 24, [5.0] * 24, [20.0] * 24
    exp[1] = lo[1] = hi[1] = NAN                          # no forecast for hour 1
    out = heavy.to_day(sd, "*error", D, exp, lo, hi, sh, CFG)
    assert isinstance(out, DayOut)
    assert out.expected[0] == 10.0 and out.low[7] == 10.0 and out.high[7] == 40.0
    assert out.high_flag[5] == 1 and out.high_flag[0] == 0
    assert math.isnan(out.expected[6]) and out.high_flag[6] == 0            # unobserved
    assert all(math.isnan(out.expected[s]) and out.high_flag[s] == 0 for s in range(60, 120))
    assert out.score[5] > 1 > out.score[0]


def test_to_day_all_nan_without_minutes():
    out = heavy.to_day(tiny_sd(minutes={"*error": {}}), "*error", D, [1.0] * 24, [0.0] * 24, [2.0] * 24, [1.0] * 1440, CFG)
    assert all(math.isnan(x) for x in out.expected) and not any(out.high_flag)


# ---- per-method behaviour (needs the package) ----------------------------------------------------------------

def make_sd(base=30.0, days=21, seed=0, d_scale=1.0, spike=None, keys=("*total", "*error"), batch=False, flat=None, levels=None):
    np = pytest.importorskip("numpy")
    all_days = [D - timedelta(k) for k in range(days, -1, -1)]
    minutes, hourly, n_min = {}, {}, {}
    for ki, key in enumerate(keys):
        minutes[key] = {}
        for di, d in enumerate(all_days):
            rng = np.random.default_rng([seed, ki, di])      # independent per key and day
            lam = base * (1 + 0.5 * np.sin(2 * np.pi * (np.arange(1440) / 60 - 6) / 24))
            if d == D:
                lam = lam * d_scale
            if batch:                                    # an hourly batch: x3 in the first 10 minutes of every hour
                lam = lam * np.where(np.arange(1440) % 60 < 10, 3.0, 1.0)
            c = rng.poisson(lam).astype("int64")
            if d == D and flat:                          # a flat line where traffic should oscillate
                c[flat[0]:flat[1]] = int(base)
            if d == D and spike == key:
                c[600:630] *= 3
            minutes[key][d] = array("i", c.tolist())
            for h in range(24):
                blk = c[h * 60:(h + 1) * 60]
                hourly[(key, d, h)] = (int(blk.sum()), int((blk ** 2).sum()))
    for d in all_days:
        for h in range(24):
            n_min[(d, h)] = 60
    obs = {d: array("i", [1] * 1440) for d in all_days}
    return ServiceData("s", all_days, minutes, obs, hourly, n_min, set(all_days[:-1]), set(), levels or {}, {}, {})


def run_day(name, sd, key="*total", knob=None):
    m = methods.METHODS[name]()
    model = m.fit(sd, D, CFG)
    assert model is not None
    knob = m.spec.default_knob if knob is None else knob
    return m, m.day(model, None, sd, key, D, (knob,), CFG)[knob]


@pytest.mark.parametrize("name", HEAVY)
def test_spec(name):
    pytest.importorskip(PKG[name])
    s = methods.METHODS[name]().spec
    assert s.heavy and s.name == name and s.default_knob in s.knobs and len(s.knobs) == 5
    assert s.band == (name != "mprofile")


@pytest.mark.parametrize("name", ["ets", "prophet", "lgbm", "mprofile"])      # chronos needs no history
def test_not_enough_history(name):
    pytest.importorskip(PKG[name])
    m = methods.METHODS[name]()
    sd = make_sd(days=3)
    assert m.fit(sd, D, CFG) is None


@pytest.mark.parametrize("name", BAND)
def test_band_covers_clean_day(name):
    pytest.importorskip(PKG[name])
    sd = make_sd()
    _, out = run_day(name, sd)
    c = sd.minutes["*total"][D]
    ok = sum(out.low[i] <= c[i] <= out.high[i] for i in range(1440))
    assert ok / 1440 >= 0.98, ok


@pytest.mark.parametrize("name", BAND)
def test_spike_is_flagged(name):
    pytest.importorskip(PKG[name])
    sd = make_sd(spike="*error")
    _, out = run_day(name, sd, key="*error")
    assert sum(out.high_flag[600:630]) >= 20
    assert sum(out.high_flag) - sum(out.high_flag[600:630]) <= 15


@pytest.mark.parametrize("name", HEAVY)
def test_no_look_ahead(name):
    pytest.importorskip(PKG[name])
    a = run_day(name, make_sd())[1]
    b = run_day(name, make_sd(d_scale=5.0))[1]          # D's own minutes and hours differ wildly
    if a.expected is not None:
        assert list(a.expected) == pytest.approx(list(b.expected), nan_ok=True)
        assert list(a.high) == pytest.approx(list(b.high), nan_ok=True)
    if name == "mprofile":                              # its history distances (the thresholds) never see day D
        def dist(sd):
            m = methods.METHODS["mprofile"]()
            model = m.fit(sd, D, CFG)
            m.day(model, None, sd, "*total", D, (0.999,), CFG)
            return model.dist["*total"]
        assert dist(make_sd()) == dist(make_sd(d_scale=5.0))


def test_mprofile_scores_the_left_profile_only():
    pytest.importorskip("stumpy")
    def scores(late):
        sd = make_sd()
        c = sd.minutes["*total"][D]
        for i in range(900, 1440):
            c[i] = c[i] * late                           # day D changes from minute 900 on
        m = methods.METHODS["mprofile"]()
        return m.day(m.fit(sd, D, CFG), None, sd, "*total", D, (0.999,), CFG)[0.999].score
    a, b = scores(1), scores(5)
    assert list(a[:900]) == pytest.approx(list(b[:900]))        # earlier minutes never match later windows of D
    assert list(a[900:]) != list(b[900:])


def test_mprofile_key_without_an_array_on_a_prior_day():
    pytest.importorskip("stumpy")
    sd = make_sd()
    del sd.minutes["*total"][D - timedelta(3)]           # observed day, but no array for this key
    m = methods.METHODS["mprofile"]()
    out = m.day(m.fit(sd, D, CFG), None, sd, "*total", D, (0.999,), CFG)[0.999]
    assert len(out.score) == 1440


def mp_day(**kw):
    sd = make_sd(base=20.0, batch=True, keys=("*total", "t1"), levels={"t1": "ERROR"}, **kw)
    m = methods.METHODS["mprofile"]()
    model = m.fit(sd, D, CFG)
    outs = m.day(model, None, sd, "t1", D, (0.99, 0.999, 0.9999), CFG)
    ds = model.dist["t1"]
    return outs, {q: ds[int(q * len(ds))] for q in outs}


def test_mprofile_threshold_is_a_history_quantile():
    pytest.importorskip("stumpy")
    outs, thr = mp_day()
    assert outs[0.999].expected is None and outs[0.999].low is None and outs[0.999].high is None
    assert thr[0.99] <= thr[0.999] <= thr[0.9999]
    assert sum(outs[0.999].high_flag) / 1440 <= 0.01          # clean day: about 1 - 0.999 flagged
    assert sum(outs[0.99].high_flag) >= sum(outs[0.9999].high_flag)


def test_mprofile_flags_a_flat_line():
    pytest.importorskip("stumpy")
    outs, thr = mp_day(flat=(600, 700))
    out = outs[0.999]
    assert max(out.score[600:760]) > thr[0.999]
    assert sum(out.high_flag[600:760]) >= 1
    assert sum(out.high_flag) <= 144


def test_chronos_samples_m6():
    pytest.importorskip("chronos")
    m = methods.METHODS["chronos"]()
    sd = make_sd(keys=("*total", "*error", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9", "t10"))
    model = m.fit(sd, D, CFG)
    picked = [k for k in sd.minutes if any(x == x for x in m.day(model, None, sd, k, D, (0.0025,), CFG)[0.0025].expected)]
    assert "*total" in picked and "*error" in picked
    assert len(picked) < len(sd.minutes)

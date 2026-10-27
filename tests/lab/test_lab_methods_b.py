"""bk09: #5 ratio, #6 cusum, #13 pca, E1 vote, and the M7 `*mix` key, on hand-made ServiceData (DB only for the run test)."""
import math
from datetime import date, timedelta

import pytest

import scripts.lab.methods as methods
from scripts.lab.data import MIX, UNOBSERVED, minute_at
from scripts.lab.kinds import assign
from scripts.lab.load import load_service
from scripts.lab.methods import apply_d4
from scripts.lab.methods.pca import top_templates
from scripts.lab.methods.ratio import binom_quantiles
from test_lab_load import put
from test_lab_methods import D, START, flat, make, run

NEW = ["ratio", "cusum", "pca", "vote"]


def test_registered():
    for name, num in zip(NEW, ("#5", "#6", "#13", "E1")):
        assert name in methods.METHODS and name in methods.GROUPS["core"]
        sp = methods.METHODS[name]().spec
        assert sp.number == num and not sp.heavy and sp.default_knob in sp.knobs
    r, c, p, v = (methods.METHODS[n]().spec for n in NEW)
    assert r.kinds == {"M2", "M5"} and r.band and r.default_knob == 1e-3 and len(r.knobs) == 6
    assert c.kinds == {"M1", "M2", "M3", "M4", "M5"} and not c.band and c.knobs == (3, 5, 7, 10, 14, 20) and c.default_knob == 7
    assert p.kinds == {"M7"} and p.cost == 2 and p.min_minute_days == 7 and p.default_knob == 0.999 and len(p.knobs) == 5
    assert v.knobs == (2,) and v.cost == 3 and not v.band


# ---- ratio ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("n,ph,p", [(10, 0.3, 0.01), (50, 0.1, 1e-3), (200, 0.02, 1e-3), (5, 0.5, 0.1)])
def test_binomial_tails_match_brute_force(n, ph, p):
    pmf = [math.comb(n, k) * ph ** k * (1 - ph) ** (n - k) for k in range(n + 1)]
    high = min(c for c in range(n + 1) if sum(pmf[c + 1:]) <= p)
    low = max(c for c in range(n + 2) if sum(pmf[:c]) <= p)
    assert binom_quantiles(n, ph, p) == (low, high)


def ratio_sd(**mods):
    return make({"*total": flat(400), "*error": flat(20)}, mods=[(k, D, 600, 660, v) for k, v in mods.items()])


def test_ratio_traffic_and_errors_together_not_flagged(cfg):
    o = run("ratio", ratio_sd(**{"*total": 800, "*error": 40}), "*error", D, cfg)
    assert sum(o.high_flag[600:660]) <= 2


def test_ratio_errors_alone_flagged(cfg):
    o = run("ratio", ratio_sd(**{"*error": 50}), "*error", D, cfg)
    assert sum(o.high_flag[600:660]) >= 50
    assert o.expected[600] == pytest.approx(400 * 0.05, rel=0.1)


def test_ratio_zero_traffic_has_no_band(cfg):
    o = run("ratio", ratio_sd(**{"*total": 0}), "*error", D, cfg)
    assert math.isnan(o.high[600]) and math.isnan(o.low[600]) and o.high_flag[600] == 0 and not math.isnan(o.high[700])


# ---- cusum ---------------------------------------------------------------------------------------------------

def test_cusum_small_persistent_rise_flagged_nb_misses(cfg):
    sd = make({"E": flat(20)}, mods=[("E", D, 600, 660, 24)])
    assert sum(run("cusum", sd, "E", D, cfg).high_flag[600:720]) >= 1
    assert sum(run("nb", sd, "E", D, cfg).high_flag[600:660]) == 0


def test_cusum_clean_at_most_one_alarm_a_day(cfg):
    for seed in (1, 2, 3):
        o = run("cusum", make({"E": flat(20)}, seed=seed), "E", D, cfg)
        assert sum(o.high_flag) <= 1


def test_cusum_resets_after_alarm(cfg):
    sd = make({"E": flat(20)}, mods=[("E", D, 600, None, 45), ("E", D, 601, None, 28)])
    o = run("cusum", sd, "E", D, cfg)
    assert o.high_flag[600] == 1 and o.high_flag[601] == 0 and 0 < o.score[601] < 7      # 28*ln1.5-10 = 1.35 after reset
    assert o.score[600] > 7


def test_cusum_knobs_and_unobserved(cfg):
    sd = make({"E": flat(20)}, mods=[("E", D, 600, 660, 40)])
    sd.minutes["E"][D][610] = UNOBSERVED
    m = methods.METHODS["cusum"]()
    outs = m.day(m.fit(sd, D, cfg), None, sd, "E", D, (3.0, 20.0), cfg)
    assert sum(outs[3.0].high_flag) >= sum(outs[20.0].high_flag) and outs[3.0].score[610] == 0 and not outs[3.0].high_flag[610]
    assert outs[3.0].expected is None and outs[3.0].high is None


# ---- pca -----------------------------------------------------------------------------------------------------

def pca_sd(swap=None, **kw):
    means = {f"T{i}": flat(3 + (i * 7) % 10) for i in range(14)}
    means.update(T0=flat(30), T1=flat(4), **{"*total": flat(100)})
    mods = [] if swap is None else [("T0", D, swap[0], swap[1], 4), ("T1", D, swap[0], swap[1], 30)]   # T0 and T1 exchange rates
    sd = make(means, mods=mods, **kw)
    sd.minutes[MIX] = sd.minutes["*total"]
    return sd


def test_pca_mix_swap_scored_clean_rarely(cfg):
    sd = pca_sd(swap=(600, 720))
    o = run("pca", sd, MIX, D, cfg)
    assert sum(o.high_flag[600:720]) >= 60
    assert sum(o.high_flag[:600]) + sum(o.high_flag[720:]) <= 20
    assert o.expected is None and o.score[650] > o.score[100]


def test_pca_top_templates_name_the_swapped_pair(cfg):
    sd = pca_sd(swap=(600, 720))
    m = methods.METHODS["pca"]()
    model = m.fit(sd, D, cfg)
    top = [t for t, _ in top_templates(model, sd, minute_at(D, 650))]
    assert len(top) == 3 and {"T0", "T1"} & set(top)


def test_pca_refit_cadence_and_no_future_data(cfg):
    sd = pca_sd()
    m = methods.METHODS["pca"]()
    first = m.fit(sd, D, cfg)
    assert m.fit(sd, D + timedelta(days=3), cfg) is first and m.fit(sd, D + timedelta(days=6), cfg) is first
    assert m.fit(sd, D + timedelta(days=7), cfg) is not first
    spoiled = pca_sd()
    for d in spoiled.days:
        if d >= D:
            spoiled.minutes["T3"][d] = type(spoiled.minutes["T3"][d])("i", [500] * 1440)
    other = methods.METHODS["pca"]().fit(spoiled, D, cfg)
    assert other.names == first.names and other.mean == first.mean and other.qs == first.qs


def test_pca_needs_a_week(cfg):
    assert methods.METHODS["pca"]().fit(pca_sd(start=D - timedelta(days=5)), D, cfg) is None


# ---- vote ----------------------------------------------------------------------------------------------------

def test_vote_two_of_three(cfg):
    big = make({"E": flat(20)}, mods=[("E", D, 600, 610, 80)])
    o = run("vote", big, "E", D, cfg)
    assert sum(o.high_flag[600:610]) >= 8 and max(o.score[600:610]) == 3
    mild = make({"E": flat(20)}, mods=[("E", D, 600, 720, 24)])
    o = run("vote", mild, "E", D, cfg)
    each = [run(n, mild, "E", D, cfg) for n in ("nb", "cusum", "weekly")]
    assert all(o.score[i] == sum(e.high_flag[i] for e in each) and bool(o.high_flag[i]) == (o.score[i] >= 2) for i in range(1440))
    assert sum(o.high_flag) < sum(each[1].high_flag)                      # cusum alone sees the rise; the vote needs a second
    assert sum(run("vote", make({"E": flat(20)}), "E", D, cfg).high_flag) <= 3


def test_vote_with_missing_voter(cfg):
    short = make({"E": flat(20)}, start=D - timedelta(days=10), mods=[("E", D, 600, 610, 80)])
    assert methods.METHODS["weekly"]().fit(short, D, cfg) is None
    o = run("vote", short, "E", D, cfg)
    assert sum(o.high_flag[600:610]) >= 8 and max(o.score) == 2           # nb + cusum are the two voters left


# ---- M7 walking ----------------------------------------------------------------------------------------------

def test_apply_d4_mix_has_no_floor(cfg):
    assert apply_d4(0, None, 1.0, None, MIX, "INFO", cfg, score=2.0)
    assert not apply_d4(100, None, 1.0, None, MIX, "INFO", cfg, score=1.0)
    assert not apply_d4(100, None, None, None, MIX, "INFO", cfg, score=5.0)


def test_assign_gives_mix_m7(cfg):
    sd = pca_sd()
    assert assign(sd, sd.days[:7])[MIX] == "M7"


def seed_pca(conn):
    for d in [date(2026, 10, n) for n in range(1, 15)]:
        for h in range(24):
            put(conn, "*minutes", d, h, [1] * 60)
            put(conn, "*total", d, h, [6] * 60)
            for t, k in (("a", 1), ("b", 2), ("c", 3)):
                put(conn, t, d, h, [(i * k + h + d.day) % 5 for i in range(60)])
    conn.commit()


def test_load_exposes_mix_and_run_writes_m7(conn, cfg):
    from scripts.lab import run as labrun
    seed_pca(conn)
    sd = load_service(conn, "s", date(2026, 10, 1), date(2026, 10, 14), 35)
    assert sd.minutes[MIX] is sd.minutes["*total"] and sd.level(MIX) == "INFO"
    a = labrun.parse_args(["--site", "laptop", "--test", "B", "--methods", "pca", "--from", "2026-10-01", "--to", "2026-10-14"])
    assert labrun.run(a, cfg, conn, conn) == 0
    rows = conn.execute("SELECT kind, status, metrics FROM lab_results WHERE method = 'pca'").fetchall()
    assert [(r["kind"], r["status"]) for r in rows] == [("M7", "ok")] and rows[0]["metrics"]["n_keys"] == 1

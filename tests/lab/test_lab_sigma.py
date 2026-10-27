import math
from array import array
from datetime import date

import pytest

import scripts.lab.methods as methods
from logai2.jobs.commit import commit
from logai2.owner.baseline import Baseline, Stat
from scripts.lab.data import UNOBSERVED, ServiceData
from scripts.lab.load import load_service
from scripts.lab.methods.sigma import Sigma, rebuild_stats

LAST = date(2026, 10, 18)      # Sunday


def seed(conn):
    """Oct 5 (Mon) .. Oct 18 (Sun), two services, hours 8-10 (+ 23 for s)."""
    for svc in ("s", "t"):
        for n in range(14):
            d = date(2026, 10, 5 + n)
            for h in (8, 9, 10, 23):
                if h == 23 and svc == "t":
                    continue
                nm = 30 if (svc, n, h) == ("s", 2, 8) else 0 if (svc, n, h) == ("s", 3, 10) else 60
                conn.execute("INSERT INTO service_hours VALUES (%s, %s, %s, %s)", (svc, d, h, nm))
                for key in ("a", "b", "*total", "*warn"):
                    if key == "b" and h == 9 and n % 2:
                        continue                                  # absent in some picked hours: counts 0
                    if key == "*warn" and n < 5:
                        continue                                  # born on Oct 10
                    s = (n * 7 + h * 3 + len(key) + (5 if svc == "t" else 0)) % 23 + 1
                    conn.execute("INSERT INTO hourly_counts VALUES (%s, %s, %s, %s, %s, %s)",
                                 (svc, key, d, h, s, s * s + n))
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) VALUES "
                 "('s', '2026-10-08', 9, 10, 'exclude'), ('s', '2026-10-09', 8, 9, 'keep')")
    conn.commit()


@pytest.mark.parametrize("D", [date(2026, 10, 11), date(2026, 10, 12), date(2026, 10, 15), date(2026, 10, 19)])
def test_rebuild_stats_equals_sql_rebuild(conn, cfg, D):
    seed(conn)
    commit(conn, cfg, D)
    live = Baseline.load(conn)
    for svc in ("s", "t"):
        sd = load_service(conn, svc, LAST, LAST, 20)
        got = rebuild_stats(sd, D)
        want = {k: v for k, v in live.stats.items() if k[0] == svc}
        assert set(got.stats) == set(want) and want
        for k, v in want.items():
            assert got.stats[k].mean == pytest.approx(v.mean, abs=1e-9, rel=0), k
            assert got.stats[k].std == pytest.approx(v.std, abs=1e-9, rel=0), k
        assert got.days == {k: v for k, v in live.days.items() if k[0] == svc}
    # the born rule and the exclusion really did something
    assert ("s", "a", "weekday", 23) in live.stats
    if D >= date(2026, 10, 15):    # picked weekdays include Oct 8-9, before *warn was born on Oct 10
        assert ("s", "*warn", "weekday", 8) in live.stats


def test_fit_none_without_committed_day(conn, cfg):
    seed(conn)
    sd = load_service(conn, "s", LAST, LAST, 20)
    assert Sigma().fit(sd, date(2026, 10, 5), cfg) is None
    assert Sigma().fit(sd, date(2026, 10, 6), cfg) is not None


# --- day() on a hand-built service ---------------------------------------------------------------------

SAT, MON = date(2026, 10, 17), date(2026, 10, 19)


def sd_with(day, **keys):
    """keys: name -> {slot: count}; every other slot is 0, slot 7 is unobserved."""
    minutes, obs = {}, array("i", [1] * 1440)
    obs[7] = 0
    for name, over in keys.items():
        a = array("i", [0] * 1440)
        a[7] = UNOBSERVED
        for slot, n in over.items():
            a[slot] = n
        minutes[name] = {day: a}
    return ServiceData("s", [day], minutes, {day: obs}, {}, {}, set(), set(),
                       {"E": "ERROR", "W": "WARN", "I": "INFO"}, {}, {"doc:d": ["E", "I"]})


def base(stats, days):
    return Baseline({("s", k, dt, h): Stat(*v) for k, dt, v in stats for h in range(24)}, days)


def full(key, mean, std, dt="weekday", n=5):
    return base([(key, dt, (mean, std))], {("s", dt): n})


def run(model, sd, key, D, cfg, prev=None, knobs=(2.0, 3.0)):
    return Sigma().day(model, prev, sd, key, D, knobs, cfg)


def test_registered(cfg):
    assert "sigma" in methods.METHODS and "sigma" in methods.GROUPS["core"]
    sp = methods.METHODS["sigma"]().spec
    assert (sp.number, sp.knobs, sp.default_knob, sp.band, sp.heavy) == ("#1", (2.0, 2.5, 3.0, 3.5, 4.0, 5.0), 3.0, True, False)


def test_band_floor_and_knob_scaling(cfg):
    sd = sd_with(MON, E={10: 17, 11: 16, 12: 5})
    out = run(full("E", 10.0, 2.0), sd, "E", MON, cfg)
    o2, o3 = out[2.0], out[3.0]
    assert o3.high[10] == 16.0 and o2.high[10] == 14.0 and o3.low[10] == 4.0 and o2.low[10] == 6.0   # k=2 is narrower
    assert o3.expected[10] == 10.0
    assert (o3.high_flag[10], o3.high_flag[11], o3.high_flag[12]) == (1, 0, 0)       # 17 > 16, 16 is not > 16
    assert (o2.high_flag[10], o2.high_flag[11], o2.high_flag[12]) == (1, 1, 0)       # 5 is under the band and the floor
    assert o3.score[10] == pytest.approx(17 / 16)


def test_young_baseline_uses_scaled_multiplier(cfg):
    sd = sd_with(MON, E={})
    young = run(full("E", 10.0, 2.0, n=2), sd, "E", MON, cfg)
    assert young[3.0].high[0] == pytest.approx(10 + 4.0 * 2.0)                      # live young 4 at k=3
    assert young[2.0].high[0] == pytest.approx(10 + 2.0 * 4 / 3 * 2.0)              # k x (4/3)
    assert cfg.baseline.sigma_multiplier == 3.0                                       # cfg never mutated
    assert run(full("E", 10.0, 2.0), sd, "E", MON, cfg)[3.0].high[0] == 16.0


def test_unobserved_minute_has_no_band_and_no_flag(cfg):
    sd = sd_with(MON, E={})
    o = run(full("E", 10.0, 2.0), sd, "E", MON, cfg)[3.0]
    assert math.isnan(o.expected[7]) and math.isnan(o.low[7]) and math.isnan(o.high[7])
    assert o.score[7] == 0 and o.high_flag[7] == 0 and not math.isnan(o.high[8])


def test_no_stats_means_nan_band_and_floor_flag(cfg):
    sd = sd_with(MON, E={3: 9, 4: 4})
    o = run(Baseline({}, {}), sd, "E", MON, cfg)[3.0]
    assert math.isnan(o.high[3]) and math.isnan(o.expected[3])
    assert (o.high_flag[3], o.high_flag[4]) == (1, 0)                              # count >= floor 5, as judge


def test_weekend_with_one_committed_day_borrows_weekday_and_never_drops(cfg):
    model = base([("*total", "weekday", (100.0, 5.0))], {("s", "weekday"): 5, ("s", "weekend"): 1})
    sd = sd_with(SAT, **{"*total": {20: 10}})
    o = run(model, sd, "*total", SAT, cfg)[3.0]
    assert o.high[20] == 115.0 and o.high_flag[20] == 0                            # upper_only: no drop alert
    sd2 = sd_with(MON, **{"*total": {20: 10}})
    assert run(model, sd2, "*total", MON, cfg)[3.0].high_flag[20] == 1             # weekday: drop (10 < 85, mean >= 20)
    two = base([("*total", "weekday", (100.0, 5.0)), ("*total", "weekend", (100.0, 5.0))],
               {("s", "weekday"): 5, ("s", "weekend"): 4})
    assert run(two, sd, "*total", SAT, cfg)[3.0].high_flag[20] == 1                # a full weekend profile: drops count again


def test_doc_key_band_is_sum_of_templates_and_level_is_worst(cfg):
    model = Baseline({("s", "E", "weekday", h): Stat(3.0, 1.0) for h in range(24)}
                     | {("s", "I", "weekday", h): Stat(4.0, 2.0) for h in range(24)}, {("s", "weekday"): 5})
    sd = sd_with(MON, **{"doc:d": {30: 17, 31: 16}}, E={}, I={})
    o = run(model, sd, "doc:d", MON, cfg)[3.0]
    assert o.expected[30] == 7.0 and o.high[30] == 7 + 3 * 3 and o.low[30] == 7 - 9
    assert (o.high_flag[30], o.high_flag[31]) == (1, 0)                              # 17 > 16, ERROR floor from the worst level


def test_warn_and_info_keys_are_never_flagged(cfg):
    sd = sd_with(MON, **{"*warn": {5: 500}, "*info": {5: 500}})
    for k in ("*warn", "*info"):
        assert not any(run(full(k, 1.0, 0.1), sd, k, MON, cfg)[3.0].high_flag)


def test_commit_time_switch(cfg):
    sd = sd_with(MON, E={})
    new, old = full("E", 100.0, 0.0), full("E", 50.0, 0.0)
    o = run(new, sd, "E", MON, cfg, prev=old)[3.0]
    assert o.high[119] == 50.0 and o.high[120] == 100.0 and o.high[0] == 50.0 and o.high[1439] == 100.0
    assert run(new, sd, "E", MON, cfg, prev=None)[3.0].high[0] == 100.0

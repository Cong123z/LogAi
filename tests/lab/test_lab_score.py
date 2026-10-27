"""The scoring sink (bk05): Test B metrics, episodes and drawn bands. Unit tests use hand-made ServiceData."""
import json
from array import array
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

import scripts.lab.methods as methods
from logai2.config import AlertConfig
from logai2.timeutil import TZ
from scripts.lab import run as labrun
from scripts.lab import score, store
from scripts.lab.data import UNOBSERVED, Incident, ServiceData, minute_at
from scripts.lab.methods import DayOut, Spec
from scripts.lab.methods.sigma import Sigma
from scripts.lab.run import RunCtx
from test_lab_load import put
from tests.test_links import tmpl

NAN = float("nan")
D0 = date(2026, 10, 1)


def spec(kinds=("M3",), knobs=(1.0,), band=True):
    return Spec("t", "T", "t", frozenset(kinds), tuple(knobs), knobs[0], band, False, 0)


def make_sd(counts, key="k", **kw):
    """counts: {day: list of 1440 ints} for `key`; a day may also be a ready array."""
    days = sorted(counts)
    mins = {key: {d: array("i", c) for d, c in counts.items()}}
    mins.update(kw.pop("extra", {}))
    obs = {d: array("i", [int(x != UNOBSERVED) for x in mins[key][d]]) for d in days}
    base = dict(service="s", days=days, minutes=mins, observed=obs, hourly={}, n_minutes={}, committed=set(),
                excluded=set(), levels={}, first_seen={}, docs={}, incidents=[])
    base.update(kw)
    return ServiceData(**base)


def ctx_for(sd, part=lambda key, day: "test", cfg=None):
    return RunCtx(sd.days, [], "days", part, cfg or _cfg(), sd)


def _cfg():
    from logai2.config import load_config
    return load_config(None)


def out(expected=5.0, low=0.0, high=10.0, flags=(), score_=0.0):
    f = bytearray(1440)
    for i in flags:
        f[i] = 1
    return DayOut(array("d", [expected] * 1440), array("d", [low] * 1440), array("d", [high] * 1440),
                  array("d", [score_] * 1440), f)


def sink_for(sd, kinds, outs_by_day, sp=None, key="k", part=None):
    sp = sp or spec(set(kinds.values()))
    ctx = ctx_for(sd, part) if part else ctx_for(sd)
    s = score.ScoreSink(None, 1, "s", "t", sp, kinds, ctx)
    for d in sorted(outs_by_day):
        s.add(key, d, outs_by_day[d])
    return s


def metrics(s):
    return s.build()


def test_coverage_995_of_1000():
    counts = [UNOBSERVED] * 1440
    for i in range(1000):
        counts[i] = 5
    for i in range(995, 1000):
        counts[i] = 50                                         # outside the band [0, 10]
    sd = make_sd({D0: counts})
    s = sink_for(sd, {"k": "M3"}, {D0: {1.0: out()}})
    m = metrics(s)["M3"]["knobs"]["1.0"]["test"]
    assert m["n_minutes"] == 1000 and m["coverage"] == 0.995
    assert m["key_days"] == pytest.approx(1000 / 1440)


def test_interval_score_one_miss_on_a_key_of_mean_one():
    counts = [1] * 1440
    counts[10], counts[20] = 2, 0                              # mean exactly 1; 2 is above high=1 by d=1; 0 is inside
    sd = make_sd({D0: counts})
    o = {D0: {1.0: out(expected=1.0, low=0.0, high=1.0)}}
    m = metrics(sink_for(sd, {"k": "M3"}, o))["M3"]["knobs"]["1.0"]["test"]
    assert m["interval_score"] == pytest.approx(1.0 + (2 / score.ALPHA) * 1 / 1440)
    assert m["coverage"] == pytest.approx(1439 / 1440)


def test_interval_score_is_normalized_by_the_keys_mean_count():
    res = []
    for mean in (1, 10):
        sd = make_sd({D0: [mean] * 1440})
        o = {D0: {1.0: out(expected=mean, low=mean * 0.5, high=mean * 1.5)}}
        res.append(metrics(sink_for(sd, {"k": "M3"}, o))["M3"]["knobs"]["1.0"]["test"]["interval_score"])
    assert res[0] == pytest.approx(1.0) and res[1] == pytest.approx(res[0])
    # a key whose mean is below 1 is divided by 1
    sd = make_sd({D0: [0] * 1439 + [1]})
    m = metrics(sink_for(sd, {"k": "M3"}, {D0: {1.0: out(expected=0.0, low=0.0, high=2.0)}}))["M3"]
    assert m["knobs"]["1.0"]["test"]["interval_score"] == pytest.approx(2.0)


def test_winkler_and_median_helpers():
    assert score.winkler(5, 0, 10, 0.005) == 10
    assert score.winkler(-1, 0, 10, 0.005) == 10 + 400
    assert score.winkler(12, 0, 10, 0.005) == 10 + 800
    h = [0] * score.NBINS
    assert score.median_from_hist(h) is None
    h[score.bin_of(2.0)] = 5
    assert score.median_from_hist(h) == pytest.approx(2.0, rel=0.02)
    h2 = [0] * score.NBINS
    h2[score.bin_of(0.5)], h2[score.bin_of(8.0)] = 3, 2
    assert score.median_from_hist(h2) == pytest.approx(0.5, rel=0.02)
    assert score.median_from_hist([1] + [0] * (score.NBINS - 1)) == pytest.approx(1e-4)
    assert score.median_from_hist([0] * (score.NBINS - 1) + [1]) == pytest.approx(1e4)


def test_sharpness_is_the_median_relative_width():
    sd = make_sd({D0: [5] * 1440})
    m = metrics(sink_for(sd, {"k": "M3"}, {D0: {1.0: out(expected=5.0, low=4.0, high=9.0)}}))["M3"]
    assert m["knobs"]["1.0"]["test"]["sharpness"] == pytest.approx(1.0, rel=0.02)


def mase_world(unobserved_hour_on_day0=False):
    days = [D0 + timedelta(n) for n in range(8)]
    counts = {d: [10] * 1440 for d in days}
    counts[days[0]] = [8] * 1440                               # 7 days before the scored day: |naive - y| = 2
    counts[days[6]] = [13] * 1440                              # 1 day before: |naive - y| = 3
    if unobserved_hour_on_day0:
        counts[days[0]][300:360] = [UNOBSERVED] * 60
    return days, make_sd(counts)


@pytest.mark.parametrize("hole, naive, want", [(False, "7d", 0.5), (True, "1d", 1 / 3)])
def test_mase_picks_7d_else_1d(hole, naive, want):
    days, sd = mase_world(hole)
    last = days[7]
    s = sink_for(sd, {"k": "M3"}, {last: {1.0: out(expected=9.0, low=0.0, high=20.0)}},
                 part=lambda key, d: "test")
    m = metrics(s)["M3"]["knobs"]["1.0"]["test"]
    assert m["mase_naive"] == naive and m["mase"] == pytest.approx(want)


def test_mase_null_without_any_naive_and_for_score_methods():
    sd = make_sd({D0: [10] * 1440})
    m = metrics(sink_for(sd, {"k": "M3"}, {D0: {1.0: out()}}))["M3"]["knobs"]["1.0"]["test"]
    assert m["mase"] is None and m["mase_naive"] is None
    sp = spec(band=False)
    o = DayOut(None, None, None, array("d", [0.0] * 1440), bytearray(1440))
    m = metrics(sink_for(sd, {"k": "M3"}, {D0: {1.0: o}}, sp=sp))["M3"]
    t = m["knobs"]["1.0"]["test"]
    assert t["coverage"] is None and t["interval_score"] is None and t["sharpness"] is None and t["mase"] is None
    assert t["alert_rate"] == 0 and t["episodes"] == 0 and t["key_days"] == pytest.approx(1.0)
    assert m["no_band_share"] is None


def test_no_band_minutes_are_counted_and_skipped():
    sd = make_sd({D0: [5] * 1440})
    o = out()
    for i in range(100):
        o.low[i] = o.high[i] = o.expected[i] = NAN
    m = metrics(sink_for(sd, {"k": "M3"}, {D0: {1.0: o}}))["M3"]
    assert m["no_band_share"] == pytest.approx(100 / 1440)
    assert m["knobs"]["1.0"]["test"]["n_minutes"] == 1340 and m["knobs"]["1.0"]["test"]["coverage"] == 1.0
    assert m["knobs"]["1.0"]["test"]["key_days"] == pytest.approx(1.0)       # the alert rate counts them


CFG = AlertConfig()


def flags_for(spans):
    f = []
    for a, b in spans:
        f += range(a, b)
    return f


def test_episodes_flaps_and_alert_rates():
    # episode A 100..106 (5 highs, 3 lows: ends 107), B 120..124 (3 highs, 3 lows: ends 125, 13 min after A: flap),
    # C 1437..1439 open at the window end (length 3 < 5: flap)
    hs = flags_for([(100, 105), (120, 123), (1437, 1440)])
    sd = make_sd({D0: [1] * 1440})
    for kind, want in (("M3", 300.0), ("M1", 21.0)):
        s = sink_for(sd, {"k": kind}, {D0: {1.0: out(flags=hs, score_=2.0)}}, sp=spec({kind}))
        m = metrics(s)[kind]["knobs"]["1.0"]["test"]
        assert m["episodes"] == 3 and m["flap_share"] == pytest.approx(2 / 3)
        assert m["alert_rate"] == pytest.approx(want)
    eps = s.episodes[("k", 1.0)]
    assert [(e.start - minute_at(D0, 0), e.end - minute_at(D0, 0)) for e in eps] == [(100, 107), (120, 125), (1437, 1440)]


def test_episode_state_survives_midnight_with_one_tracker_per_key_and_knob():
    d1 = D0 + timedelta(1)
    sd = make_sd({D0: [1] * 1440, d1: [1] * 1440})
    a = out(flags=range(1437, 1440))                           # ALERTING reached at 23:59
    b = out(flags=())                                          # three lows on day 2 end the one episode
    m = metrics(sink_for(sd, {"k": "M3"}, {D0: {1.0: a}, d1: {1.0: b}}))["M3"]["knobs"]["1.0"]["test"]
    assert m["episodes"] == 1 and m["key_days"] == pytest.approx(2.0)


def test_out_of_order_days_assert():
    sd = make_sd({D0: [1] * 1440, D0 + timedelta(1): [1] * 1440})
    s = score.ScoreSink(None, 1, "s", "t", spec(), {"k": "M3"}, ctx_for(sd))
    s.add("k", D0 + timedelta(1), {1.0: out()})
    with pytest.raises(AssertionError):
        s.add("k", D0, {1.0: out()})


def test_episode_counts_only_when_it_starts_on_a_shared_normal_minute():
    sd = make_sd({D0: [1] * 1440}, incidents=[Incident("k", minute_at(D0, 500), minute_at(D0, 510), "synthetic")])
    # episode 1 starts at 490 (inside incident +-15: not normal), episode 2 at 700
    hs = flags_for([(490, 495), (700, 705)])
    s = sink_for(sd, {"k": "M3"}, {D0: {1.0: out(flags=hs)}})
    m = metrics(s)["M3"]["knobs"]["1.0"]["test"]
    assert m["episodes"] == 1
    assert len(s.episodes[("k", 1.0)]) == 2                    # both are stored for lab_episodes


def test_shared_normal_minutes():
    day = D0
    total = [1] * 1440
    total[1380:] = [UNOBSERVED] * 60
    mk = lambda: array("i", total)                              # noqa: E731
    extra = {key: {day: mk()} for key in ("T", "other", "*error", "doc:D")}
    sd = make_sd({day: total}, key="*total", extra=extra, excluded={(day, 3)}, levels={"T": "ERROR", "other": "ERROR"},
                 docs={"doc:D": ["T"]}, incidents=[
                     Incident("T", minute_at(day, 600), minute_at(day, 610), "synthetic"),
                     Incident(None, minute_at(day, 1000), minute_at(day, 1001), "synthetic")])
    kinds = {"*total": "M1", "*error": "M2", "T": "M3", "other": "M4", "doc:D": "M5"}
    sp = spec(set(kinds.values()))
    res = []
    for exp in (5.0, 6.0):                                      # two different methods: the same minutes
        ctx = ctx_for(sd)
        s = score.ScoreSink(None, 1, "s", "t", sp, kinds, ctx)
        for k in kinds:
            s.add(k, day, {1.0: out(expected=exp, flags=(), low=0.0, high=2.0)})
        res.append({kd: v["knobs"]["1.0"]["test"]["n_minutes"] for kd, v in s.build().items()})
    assert res[0] == res[1]
    n = 1440 - 60 - 60                                          # excluded hour 3, unobserved tail
    both = 40 + 31                                              # incident 585..624 and 985..1015 (overlap-free)
    assert res[0] == {"M1": n - both, "M2": n - both, "M3": n - both, "M5": n - both, "M4": n - 31}


def test_incident_on_total_covers_every_key_and_level_class_key():
    day = D0
    c = [1] * 1440
    extra = {"T": {day: array("i", c)}, "*warn": {day: array("i", c)}, "*error": {day: array("i", c)}}
    sd = make_sd({day: c}, key="*total", extra=extra, levels={"T": "WARN"},
                 incidents=[Incident("*total", minute_at(day, 100), minute_at(day, 101), "t")])
    kinds = {"*total": "M1", "T": "M3", "*warn": "M4", "*error": "M2"}
    s = score.ScoreSink(None, 1, "s", "t", spec(set(kinds.values())), kinds, ctx_for(sd))
    for k in kinds:
        s.add(k, day, {1.0: out()})
    n = {kd: v["knobs"]["1.0"]["test"]["n_minutes"] for kd, v in s.build().items()}
    assert n == {"M1": 1440 - 31, "M2": 1440 - 31, "M3": 1440 - 31, "M4": 1440 - 31}
    # an incident on T does not touch *error (T is WARN) but covers *warn and *total
    sd2 = make_sd({day: c}, key="*total", extra=extra, levels={"T": "WARN"},
                  incidents=[Incident("T", minute_at(day, 100), minute_at(day, 101), "t")])
    s = score.ScoreSink(None, 1, "s", "t", spec(set(kinds.values())), kinds, ctx_for(sd2))
    for k in kinds:
        s.add(k, day, {1.0: out()})
    n = {kd: v["knobs"]["1.0"]["test"]["n_minutes"] for kd, v in s.build().items()}
    assert n == {"M1": 1440 - 31, "M2": 1440, "M3": 1440 - 31, "M4": 1440 - 31}


def test_m6_attribution():
    d1, d2 = D0, D0 + timedelta(1)
    first = minute_at(d1, 600)
    sd = make_sd({d1: [1] * 1440, d2: [1] * 1440}, key="n", first_seen={"n": first})
    sp = spec({"M3", "M6"})
    s = sink_for(sd, {"n": "M3"}, {d1: {1.0: out()}, d2: {1.0: out()}}, sp=sp, key="n")
    m = s.build()
    assert m["M6"]["knobs"]["1.0"]["test"]["n_minutes"] == 1440
    assert m["M3"]["knobs"]["1.0"]["test"]["n_minutes"] == 1440      # 600 before first_seen + 840 after the first day
    # an M6-only key: later minutes are not scored
    sd = make_sd({d1: [1] * 1440, d2: [1] * 1440}, key="n", first_seen={"n": minute_at(d1, 0)})
    s = sink_for(sd, {"n": "M6"}, {d1: {1.0: out()}, d2: {1.0: out()}}, sp=spec({"M3", "M6"}), key="n")
    m = s.build()
    assert m["M6"]["knobs"]["1.0"]["test"]["n_minutes"] == 1440
    assert m["M3"]["knobs"]["1.0"]["test"]["n_minutes"] == 0 and m["M3"]["n_keys"] == 0 and m["M6"]["n_keys"] == 1


def test_only_spec_kinds_are_scored_and_parts_split():
    d1, d2 = D0, D0 + timedelta(1)
    sd = make_sd({d1: [1] * 1440, d2: [1] * 1440})
    part = lambda key, d: "tune" if d == d1 else "test"           # noqa: E731
    s = sink_for(sd, {"k": "M3"}, {d1: {1.0: out()}, d2: {1.0: out()}}, part=part)
    m = s.build()
    assert set(m) == {"M3"} and m["M3"]["knobs"]["1.0"]["tune"]["n_minutes"] == 1440
    assert m["M3"]["knobs"]["1.0"]["test"]["n_minutes"] == 1440
    assert set(m["M3"]["days"]["1.0"]) == {d2.isoformat()}          # test days only


def test_days_aggregates_sum_to_the_test_totals():
    days = [D0 + timedelta(n) for n in range(9)]
    counts = {d: [(i * 7 + n) % 9 for i in range(1440)] for n, d in enumerate(days)}
    sd = make_sd(counts)
    part = lambda key, d: "test" if d >= days[2] else "tune"       # noqa: E731
    outs = {d: {1.0: out(expected=4.0, low=1.0, high=7.0, flags=range(100 + n, 106 + n))} for n, d in enumerate(days)}
    m = sink_for(sd, {"k": "M3"}, outs, part=part).build()["M3"]
    t, ds = m["knobs"]["1.0"]["test"], m["days"]["1.0"]
    assert len(ds) == 7
    assert sum(x["n"] for x in ds.values()) == t["n_minutes"]
    assert sum(x["covered"] for x in ds.values()) / t["n_minutes"] == pytest.approx(t["coverage"])
    assert sum(x["winkler"] for x in ds.values()) / t["n_minutes"] == pytest.approx(t["interval_score"])
    assert sum(x["abs_err"] for x in ds.values()) / sum(x["abs_naive"] for x in ds.values()) == pytest.approx(t["mase"])
    assert sum(x["episodes"] for x in ds.values()) == t["episodes"] > 0
    assert sum(x["flaps"] for x in ds.values()) / t["episodes"] == pytest.approx(t["flap_share"])
    assert sum(x["key_days"] for x in ds.values()) == pytest.approx(t["key_days"])
    json.dumps(m)


def test_metrics_from_is_pure_on_an_empty_accumulator():
    m = score.metrics_from(score.Acc("M3"))
    assert m["n_minutes"] == 0 and m["coverage"] is None and m["interval_score"] is None and m["alert_rate"] is None
    assert m["episodes"] == 0 and m["flap_share"] is None and m["key_days"] == 0


# --- drawn bands and the DB ---------------------------------------------------------------------------------------

def test_drawn_keys_per_kind_and_incident_keys():
    day = D0
    keys = {f"t{i}": [i + 1] * 1440 for i in range(5)}
    sd = make_sd({day: [1] * 1440}, key="*total", extra={k: {day: array("i", c)} for k, c in keys.items()},
                 incidents=[Incident("t0", 10, 20, "s"), Incident("nope", 10, 20, "s"), Incident(None, 1, 2, "s")])
    kinds = {"*total": "M1", **{k: "M3" for k in keys}}
    s = score.ScoreSink(None, 1, "s", "t", spec(set(kinds.values())), kinds, ctx_for(sd))
    assert s.drawn == {"*total", "t4", "t3", "t2", "t0"}


def test_band_rows_step_1_on_flagged_days_else_5():
    d1, d2 = D0, D0 + timedelta(1)
    sd = make_sd({d1: [1] * 1440, d2: [1] * 1440})
    s = sink_for(sd, {"k": "M3"}, {d1: {1.0: out(flags=range(100, 105))}, d2: {1.0: out(expected=3.0)}})
    rows = {r[0]: r for r in s.band_rows[("k", 1.0)]}
    r1, r2 = rows[d1], rows[d2]
    assert r1[1] == 1 and len(r1[2]) == len(r1[3]) == len(r1[4]) == len(r1[5]) == len(r1[6]) == 1440
    assert r1[6][100] is True and r1[6][99] is False
    assert r2[1] == 5 and len(r2[2]) == 288 and r2[3][0] == 3.0 and r2[2][0] == 1 and r2[6][0] is False


def test_band_rows_downsample_means_and_flags():
    sd = make_sd({D0: [UNOBSERVED] * 5 + [2, 3, 4, 5, 6] + [1] * 1430})
    o = out(expected=2.0, low=1.0, high=4.0)
    o.expected[5] = NAN
    o.high_flag[7] = 0
    s = sink_for(sd, {"k": "M3"}, {D0: {1.0: o}})
    (_, step, counts, exp, low, high, flags) = s.band_rows[("k", 1.0)][0]
    assert step == 5 and counts[0] == -1 and counts[1] == 4 and exp[1] == 2.0 and low[1] == 1.0 and high[1] == 4.0


def sigma_world(conn, spike_day):
    days = [date(2026, 10, 13) + timedelta(n) for n in range(14)]
    tmpl(conn, "busy", "ERROR")
    for d in [date(2026, 10, 6) + timedelta(n) for n in range(7)] + days:
        for h in range(24):
            if d in days:
                put(conn, "*minutes", d, h, [1] * 60)
                put(conn, "*total", d, h, [6] * 60)
                c = [3] * 60
                if d == spike_day and h == 10:
                    c[10:20] = [20] * 10
                put(conn, "busy", d, h, c)
            else:
                conn.execute("INSERT INTO service_hours VALUES ('s', %s, %s, 60)", (d, h))
                conn.execute("INSERT INTO hourly_counts VALUES ('s', 'busy', %s, %s, 180, 540)", (d, h))
                conn.execute("INSERT INTO hourly_counts VALUES ('s', '*total', %s, %s, 360, 2160)", (d, h))
        if d not in days:
            conn.execute("INSERT INTO day_status VALUES ('s', %s, true, 'weekday')", (d,))
    conn.commit()
    return days


def sigma_args():
    return labrun.parse_args(["--site", "laptop", "--test", "B", "--methods", "sigma", "--from", "2026-10-13",
                              "--to", "2026-10-26"])


def test_run_sigma_writes_results_episodes_and_bands(conn, cfg):
    spike = date(2026, 10, 22)
    sigma_world(conn, spike)
    assert labrun.run(sigma_args(), cfg, conn, conn) == 0
    res = {r["kind"]: r for r in conn.execute("SELECT * FROM lab_results WHERE method = 'sigma'")}
    m3 = res["M3"]
    assert m3["status"] == "ok" and m3["scope"] == "s" and m3["knob"] is None
    mm = m3["metrics"]
    assert mm["n_keys"] == 1 and set(mm["knobs"]) == {"2.0", "2.5", "3.0", "3.5", "4.0", "5.0"}
    t = mm["knobs"]["3.0"]["test"]
    assert t["episodes"] == 1 and t["n_minutes"] > 0
    assert t["coverage"] == pytest.approx(1 - 10 / t["n_minutes"])          # the 10 spike minutes are misses
    assert set(mm["days"]["3.0"]) == {(date(2026, 10, 20) + timedelta(n)).isoformat() for n in range(7)}
    assert mm["days"]["3.0"][spike.isoformat()]["episodes"] == 1
    assert res["M4"]["status"] == "skipped" and res["M4"]["metrics"]["n_keys"] == 0
    eps = conn.execute("SELECT knob, key, start_minute, end_minute FROM lab_episodes WHERE key = 'busy' "
                       "ORDER BY knob").fetchall()
    assert [e["knob"] for e in eps] == [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
    assert eps[0]["start_minute"] == minute_at(spike, 10 * 60 + 10)
    bands = conn.execute("SELECT day, step, array_length(counts, 1) AS n, flags FROM lab_bands "
                         "WHERE key = 'busy' AND knob = 3.0 ORDER BY day").fetchall()
    assert len(bands) == 14
    for b in bands:
        assert (b["step"], b["n"]) == ((1, 1440) if b["day"] == spike else (5, 288))
    assert any(next(b for b in bands if b["day"] == spike)["flags"])
    assert conn.execute("SELECT count(*) AS n FROM lab_bands WHERE key = '*total' AND knob = 3.0").fetchone()["n"] == 14


def test_sink_write_is_one_transaction(monkeypatch, conn, cfg):
    sigma_world(conn, date(2026, 10, 22))

    def boom(*a, **k):
        raise RuntimeError("bands down")
    monkeypatch.setattr(store, "put_bands", boom)
    assert labrun.run(sigma_args(), cfg, conn, conn) == 0
    for t in ("lab_episodes", "lab_bands"):
        assert conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] == 0
    rows = conn.execute("SELECT status, metrics FROM lab_results").fetchall()
    assert rows and all(r["status"] == "failed" for r in rows) and "bands down" in rows[0]["metrics"]["error"]

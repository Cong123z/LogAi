import math
from datetime import date, datetime, timedelta, timezone
from statistics import NormalDist

import pytest

from logai2.bands import (EWMA_DAYS, METHODS, MIN_DAYS, Band, DayHour, band, floor_of, history, in_scope, kind_of,
                          nb_quantiles, tail_above, tail_below, training)
from logai2.jobs.commit import commit

KGRID = (2, 2.5, 3, 3.5, 4, 5, 6, 8)
WD = [date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19, 20)]      # Mon..Fri, Mon, Tue
TODAY = date(2026, 10, 21)


def service(conn, name="s"):
    conn.execute("INSERT INTO services VALUES (%s, 0) ON CONFLICT DO NOTHING", (name,))
    conn.commit()


def hours(conn, svc, key, day, cells):
    """cells: {hour: (sum, sumsq, n_minutes)}"""
    for hour, (s, sq, n) in cells.items():
        conn.execute("INSERT INTO service_hours VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (svc, day, hour, n))
        conn.execute("INSERT INTO hourly_counts VALUES (%s, %s, %s, %s, %s, %s)", (svc, key, day, hour, s, sq))
    conn.commit()


def committed(conn, svc, days):
    for d in days:
        conn.execute("INSERT INTO day_status VALUES (%s, %s, true, %s)", (svc, d, "weekend" if d.weekday() >= 5 else "weekday"))
    conn.commit()


def exclude(conn, svc, day, hour_from, hour_to):
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                 "VALUES (%s, %s, %s, %s, 'exclude')", (svc, day, hour_from, hour_to))
    conn.commit()


def p_of(k):
    return 0.5 * math.erfc(k / math.sqrt(2))


def rows_of(ms, vs, n=60, start=date(2026, 10, 12)):
    return [DayHour(start + timedelta(days=i), n, round(m * n), round((v + m * m) * n))
            for i, (m, v) in enumerate(zip(ms, vs))]


def test_sigma_equals_rebuild(conn, cfg):
    service(conn)
    for i, d in enumerate(WD[:6]):
        for key in ("*total", "t1"):
            hours(conn, "s", key, d, {h: (60 * (h + 2 + i) + 7 * i, 3000 * (h + 1) + 11 * i * i, 60 - i) for h in (3, 4, 5)})
    commit(conn, cfg, TODAY)
    hist = history(conn, "s", "*total", date(2026, 10, 1), date(2026, 10, 20))
    rows = conn.execute("SELECT * FROM baseline WHERE key = '*total'").fetchall()
    assert len(rows) == 3
    for r in rows:
        b = band("sigma", training(hist, TODAY, r["hour"], "sigma"), 3)
        assert b.low == pytest.approx(r["mean"] - 3 * r["std"], rel=1e-9)
        assert b.high == pytest.approx(r["mean"] + 3 * r["std"], rel=1e-9)


@pytest.mark.parametrize("mu", (0.2, 1, 3.7, 25, 400))
def test_nb_quantiles_match_lab(mu):
    from scripts.lab.methods.nb import nb_quantiles as lab
    for var in (mu, 2 * mu, 10 * mu):
        for k in KGRID:
            assert nb_quantiles(mu, var, p_of(k)) == lab(mu, var, p_of(k))


def test_nb_poisson_when_var_le_mean():
    for k in KGRID:
        assert nb_quantiles(4, 3, p_of(k)) == nb_quantiles(4, 4, p_of(k))


def test_nb_normal_branch_close_to_poisson_tail():
    lo, hi = nb_quantiles(5000, 5000, p_of(3))
    assert hi == pytest.approx(5000 + 3 * math.sqrt(5000), abs=1.5)
    assert lo == pytest.approx(5000 - 3 * math.sqrt(5000), abs=1.5)


def test_median_robust():
    rows = rows_of([2, 3, 2, 8, 3], [2, 3, 2, 10, 3])
    assert band("median", rows, 3).high == pytest.approx(9.8398, abs=1e-3)
    assert band("sigma", rows, 3).high == pytest.approx(12.62, abs=1e-2)


def test_ewma_weights():
    rows = rows_of([2, 3, 2, 8, 3], [2, 3, 2, 10, 3])
    assert band("ewma", rows, 3).high == pytest.approx(13.510, abs=1e-3)


def test_missing_counts_row_is_zero(conn):
    service(conn)
    committed(conn, "s", [WD[0]])
    hours(conn, "s", "k", WD[0], {5: (60, 60, 60)})
    conn.execute("INSERT INTO service_hours VALUES ('s', %s, 6, 60)", (WD[0],))   # hour 6: no hourly_counts row
    conn.commit()
    hist = history(conn, "s", "k", WD[0], WD[0])
    assert hist[(WD[0], 6)] == DayHour(WD[0], 60, 0, 0)
    assert (WD[0], 7) not in hist               # no service_hours row: not a training day


def test_excluded_and_empty_hours_skipped(conn):
    service(conn)
    committed(conn, "s", WD[:2])
    for d in WD[:2]:
        hours(conn, "s", "k", d, {9: (60, 60, 60), 10: (60, 60, 60), 11: (60, 60, 0)})
    exclude(conn, "s", WD[0], 10, 11)
    hist = history(conn, "s", "k", WD[0], WD[1])
    assert set(hist) == {(WD[0], 9), (WD[1], 9), (WD[1], 10)}


def test_uncommitted_day_not_in_history(conn):
    service(conn)
    committed(conn, "s", [WD[0]])
    hours(conn, "s", "k", WD[0], {9: (60, 60, 60)})
    hours(conn, "s", "k", WD[1], {9: (60, 60, 60)})
    assert set(history(conn, "s", "k", WD[0], WD[1])) == {(WD[0], 9)}


def test_flat_key_variance_clamped():
    for n in range(1, 200):
        for c in (1, 3, 7, 13):
            r = DayHour(WD[0], n, n * c, n * c * c)
            assert r.v >= 0
    rows = [DayHour(WD[i], 49, 49 * 7, 49 * 49) for i in range(5)]
    for meth in METHODS:
        b = band(meth, rows, 3)
        assert b.low <= b.high


def test_min_days_per_method():
    rows = rows_of([2, 3, 2, 8, 3], [2, 3, 2, 10, 3])
    for meth in METHODS:
        assert band(meth, rows[:MIN_DAYS[meth] - 1], 3) is None
        assert isinstance(band(meth, rows[:MIN_DAYS[meth]], 3), Band)
    assert MIN_DAYS == {"sigma": 1, "nb": 1, "median": 3, "ewma": 2}


def test_weekend_two_days_median_none_others_draw(conn):
    service(conn)
    sat, sun = date(2026, 10, 17), date(2026, 10, 18)
    committed(conn, "s", [sat, sun])
    for d in (sat, sun):
        hours(conn, "s", "k", d, {9: (60, 60, 60)})
    hist = history(conn, "s", "k", date(2026, 10, 1), sun)
    rows = training(hist, date(2026, 10, 24), 9, "sigma")
    assert len(rows) == 2
    assert band("median", rows, 3) is None and band("sigma", rows, 3) and band("ewma", rows, 3)


def test_training_picks_5_weekdays_and_28_day_ewma(conn):
    service(conn)
    day0 = date(2026, 10, 30)
    days = [day0 - timedelta(days=i) for i in range(1, 30) if (day0 - timedelta(days=i)).weekday() < 5]
    committed(conn, "s", days)
    for d in days:
        hours(conn, "s", "k", d, {9: (60, 60, 60)})
    hist = history(conn, "s", "k", date(2026, 10, 1), day0)
    five = training(hist, day0, 9, "sigma")
    assert [r.day for r in five] == sorted(days[:5])
    ew = training(hist, day0, 9, "ewma")
    assert all(day0 - timedelta(days=EWMA_DAYS) <= r.day < day0 and r.day.weekday() < 5 for r in ew)
    assert len(ew) == len([d for d in days if (day0 - d).days <= EWMA_DAYS])
    assert training(hist, day0, 9, "nb") == five and training(hist, day0, 9, "median") == five


def test_warn_info_born_rule(conn):
    service(conn)
    committed(conn, "s", WD[:5])
    for d in WD[:5]:
        hours(conn, "s", "*total", d, {9: (60, 60, 60)})
    for d in WD[2:5]:
        hours(conn, "s", "*warn", d, {9: (60, 60, 60)})
    hist = history(conn, "s", "*warn", WD[0], WD[4])
    assert training(hist, WD[2], 9, "sigma") == []
    assert [r.day for r in training(hist, WD[3], 9, "sigma")] == [WD[2]]
    # the born day still counts as one of the picked days: *total keeps all
    assert len(training(history(conn, "s", "*total", WD[0], WD[4]), WD[4], 9, "sigma")) == 4


def test_tails():
    assert tail_above(2.0, 2.0, 0) == 1.0
    assert tail_above(2, 2, 5) == pytest.approx(0.05265, abs=1e-5)
    assert tail_below(2, 2, 0) == pytest.approx(0.13534, abs=1e-5)
    assert tail_below(2, 2, -1) == 0.0
    assert tail_above(2, 6, 3) + tail_below(2, 6, 2) == pytest.approx(1.0)       # NB
    nd = NormalDist(5000, math.sqrt(5000))
    assert tail_above(5000, 5000, 5100) == pytest.approx(1 - nd.cdf(5099.5), rel=1e-6)
    assert tail_below(5000, 5000, 4900) == pytest.approx(nd.cdf(4900.5), rel=1e-6)


def test_floor_of(cfg):
    assert floor_of(cfg, "*error", None) == cfg.floors["ERROR"]
    assert floor_of(cfg, "*total", None) is None and floor_of(cfg, "*warn", None) is None
    assert floor_of(cfg, "*info", None) is None
    assert floor_of(cfg, "t1", "WARNING") == cfg.floors["WARN"]
    assert floor_of(cfg, "t1", "FATAL") == cfg.floors["ERROR"]
    assert floor_of(cfg, "t1", "INFO") is None and floor_of(cfg, "t1", None) is None


def test_kind_and_scope(conn):
    assert kind_of("*warn", 9) == "*warn/*info" and kind_of("*info", 0) == "*warn/*info"
    assert kind_of("*total", 1) == "*total" and kind_of("*error", 1) == "*error"
    assert kind_of("t1", 2.0) == "busy" and kind_of("t1", 1.99) == "sparse"
    service(conn)
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    for tid, ago in (("t23", 23), ("t25", 25)):
        conn.execute("INSERT INTO templates (service, template_id, level, text, first_seen, last_seen) "
                     "VALUES ('s', %s, 'INFO', 'x', %s, %s)", (tid, now - timedelta(hours=ago), now))
    conn.commit()
    assert in_scope(conn, "s", "*total", now) and in_scope(conn, "s", "*warn", now)
    assert not in_scope(conn, "s", "doc:x", now) and not in_scope(conn, "s", "verdict:action", now)
    assert not in_scope(conn, "s", "t23", now) and in_scope(conn, "s", "t25", now)
    assert not in_scope(conn, "s", "nope", now)

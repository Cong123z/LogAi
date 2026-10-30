from datetime import date, datetime, timedelta, timezone

from test_bands import committed, exclude, hours, service

from logai2.bands import KGRID, Band, best_k, expected_outside, stored_tune, tail_above, tune
from logai2.db import put_setting

TRAIN = [date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19)]    # Mon..Fri, Mon
TEST = date(2026, 10, 20)                                         # Tue
TODAY = date(2026, 10, 21)
H = (3, 4, 5)


def cell(m, v, n=60):
    return (round(m * n), round((v + m * m) * n), n)


def build(conn, key="*total", train=(10, 10), test=(11, 11), days=TRAIN, svc="s", hrs=H):
    service(conn, svc)
    for d in days:
        hours(conn, svc, key, d, {h: cell(*train) for h in hrs})
    hours(conn, svc, key, TEST, {h: cell(*test) for h in hrs})
    committed(conn, svc, [*days, TEST])


def method(res, kind, m):
    return res["kinds"][kind]["methods"][m]


def run(conn, cfg, **kw):
    return tune(conn, cfg, TODAY, test_days=1, **kw)


def test_best_k_smallest_reaching_target(conn, cfg):
    build(conn)
    s = method(run(conn, cfg), "*total", "sigma")
    assert s["k"] == 3.5 and s["flag"] == "" and s["coverage"] >= 0.995


def test_never_reaches_flag(conn, cfg):
    build(conn, test=(100, 100))
    s = method(run(conn, cfg), "*total", "sigma")
    assert s["k"] == 8 and s["flag"] == "never reaches 99.5 %"


def test_not_enough_days_flag(conn, cfg):
    build(conn, days=[date(2026, 10, 16), date(2026, 10, 19)])
    res = run(conn, cfg)
    assert method(res, "*total", "median")["flag"] == "not enough days"
    assert method(res, "*total", "median")["k"] is None
    assert method(res, "*total", "sigma")["k"] is not None


def test_floor_applies():
    # high 2.4 would give t = 3; the ERROR floor 5 makes t = 5
    assert expected_outside(1, 1, 60, Band(-1, 2.4), 5, False) == 60 * tail_above(1, 1, 5)
    assert expected_outside(1, 1, 60, Band(-1, 2.4), None, False) == 60 * tail_above(1, 1, 3)


def test_low_side_only_total(conn, cfg):
    build(conn, key="*total", test=(0, 0))
    for d in TRAIN:
        hours(conn, "s", "*info", d, {h: cell(10, 10) for h in H})
    hours(conn, "s", "*info", TEST, {h: cell(0, 0) for h in H})
    res = run(conn, cfg)
    assert method(res, "*total", "sigma")["k"] == 3.5       # low = 10 - k*sd > 0 until k 3.5
    assert method(res, "*warn/*info", "sigma")["k"] == 2


def test_ratio_sigma_is_one(conn, cfg):
    build(conn)
    res = run(conn, cfg)
    assert all(method(res, kd, "sigma")["ratio"] == 1.0 for kd in res["kinds"] if method(res, kd, "sigma")["k"] is not None)


def test_tune_skips_excluded_test_hour(conn, cfg):
    build(conn, test=(10, 10))
    conn.execute("DELETE FROM hourly_counts WHERE day = %s AND hour = 3", (TEST,))
    conn.execute("INSERT INTO hourly_counts VALUES ('s', '*total', %s, 3, 30000, 15000000)", (TEST,))
    conn.commit()
    assert method(run(conn, cfg), "*total", "sigma")["flag"] == "never reaches 99.5 %"
    exclude(conn, "s", TEST, 3, 4)
    assert method(run(conn, cfg), "*total", "sigma")["k"] == 3


def test_stored_once_and_versioned(conn, cfg):
    build(conn)
    assert stored_tune(conn) is None
    res = run(conn, cfg)
    assert stored_tune(conn) == res and res["version"] == 1 and res["test_days"] == [TEST.isoformat()]
    assert best_k(res, "*total", "sigma") == (3.5, True)
    assert best_k(None, "*total", "sigma") == (3.0, False)
    put_setting(conn, "bands_tune", {**res, "version": 2})
    conn.commit()
    assert stored_tune(conn) is None


def test_off_service_skipped(conn, cfg):
    build(conn)
    conn.execute("UPDATE services SET monitored = false")
    conn.commit()
    assert run(conn, cfg)["kinds"] == {}


def test_template_kinds_and_scope(conn, cfg):
    build(conn, key="t_busy", train=(5, 5), test=(5, 5))
    for k in ("t_new", "t_busy"):
        old = datetime(2026, 10, 1, tzinfo=timezone.utc) if k == "t_busy" else datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        conn.execute("INSERT INTO templates (service, template_id, level, text, first_seen, last_seen) "
                     "VALUES ('s', %s, 'INFO', 'x', %s, %s)", (k, old, old))
    for d in (*TRAIN, TEST):
        hours(conn, "s", "t_new", d, {h: cell(5, 5) for h in H})
    res = run(conn, cfg)
    assert res["kinds"]["busy"]["keys"] == 1


def test_test_days_are_last_committed_days(conn, cfg):
    build(conn)
    res = tune(conn, cfg, date(2026, 10, 22), test_days=1)       # Oct 21 has no commit: not a test day
    assert res["test_days"] == [TEST.isoformat()]

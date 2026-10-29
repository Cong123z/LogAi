import tracemalloc
from datetime import date

from logai2.owner.baseline import Baseline, Lookup, Stat, judge

LK = lambda this, nxt=None, days=5, upper_only=False, dt="weekday": Lookup(this, nxt, days, upper_only, dt)


def test_upper_needs_band_and_floor(cfg):
    assert judge(20, "s:T1", "ERROR", LK(Stat(3, 2)), cfg).high          # 20 > 9 and >= 5
    assert not judge(4, "s:T1", "ERROR", LK(Stat(0, 0.5)), cfg).high     # above band, below floor
    assert not judge(500, "s:T1", "INFO", LK(Stat(3, 2)), cfg).high      # INFO never


def test_service_keys_use_fixed_levels(cfg):
    assert not judge(500, "*total", "ERROR", LK(Stat(3, 2)), cfg).high   # *total is INFO
    assert judge(20, "*error", "INFO", LK(Stat(3, 2)), cfg).high         # *error is ERROR


def test_next_hour_widens(cfg):
    v = judge(20, "s:T1", "ERROR", LK(Stat(3, 2), Stat(15, 3)), cfg)
    assert not v.high and v.band_high == 24 and v.band_low == -3        # edge 24


def test_young_band_uses_4_sigma(cfg):
    assert not judge(10, "s:T1", "ERROR", LK(Stat(3, 2), days=2), cfg).high       # edge 11
    assert judge(12, "s:T1", "ERROR", LK(Stat(3, 2), days=2), cfg).high           # above 11, k=4 applied


def test_floor_only_without_baseline(cfg):
    v = judge(5, "s:T1", "ERROR", LK(None), cfg)
    assert v.high and v.score == 1 and v.band_high is None
    assert not judge(9, "s:T1", "WARN", LK(None), cfg).high
    assert judge(10, "s:T1", "warning", LK(None), cfg).high
    assert judge(9, "s:T1", "INFO", LK(None), cfg).score == 0


def test_drop_rules(cfg):
    assert judge(0, "*total", "INFO", LK(Stat(100, 10)), cfg).high
    assert not judge(0, "*total", "INFO", LK(Stat(100, 10), days=3), cfg).high      # young
    assert not judge(0, "*total", "INFO", LK(Stat(10, 2)), cfg).high                # mean < 20
    assert not judge(0, "*total", "INFO", LK(Stat(100, 10), upper_only=True), cfg).high
    assert not judge(0, "s:T1", "ERROR", LK(Stat(100, 10)), cfg).high               # templates never drop


def test_weekend_full_at_its_day_cap(cfg):
    # only 4 weekend days are ever kept: 4 is a full weekend baseline (k 3, drops allowed)
    v = judge(0, "*total", "INFO", LK(Stat(100, 10), days=4, dt="weekend"), cfg)
    assert v.high and v.band_high == 130
    assert not judge(0, "*total", "INFO", LK(Stat(100, 10), days=3, dt="weekend"), cfg).high   # young
    assert not judge(0, "*total", "INFO", LK(Stat(100, 10), days=4), cfg).high                 # weekday: young


def test_score(cfg):
    assert judge(18, "s:T1", "ERROR", LK(Stat(3, 2)), cfg).score == 2
    assert judge(1, "s:T1", "ERROR", LK(Stat(0, 0)), cfg).score == 1.0


def test_weekend_falls_back_to_weekday():
    b = Baseline({("s", "k", "weekday", 10): Stat(5, 1)}, {("s", "weekday"): 5, ("s", "weekend"): 1})
    lk = b.lookup("s", "k", date(2026, 10, 18), 10)          # Sunday
    assert lk.this == Stat(5, 1) and lk.upper_only and lk.days == 5 and lk.daytype == "weekday"


def test_lookup_next_hour_wraps_and_missing():
    b = Baseline({("s", "k", "weekend", 0): Stat(1, 1), ("s", "k", "weekend", 23): Stat(2, 1)},
                 {("s", "weekend"): 2})
    lk = b.lookup("s", "k", date(2026, 10, 17), 23)          # Saturday
    assert lk == Lookup(Stat(2, 1), Stat(1, 1), 2, False, "weekend")
    assert b.lookup("s", "other", date(2026, 10, 19), 5) == Lookup(None, None, 0, False)


def test_load_caps_days(conn):
    for d in range(1, 16):        # Oct 1..15 2026: 11 weekdays, 4 weekend days
        day = date(2026, 10, d)
        conn.execute("INSERT INTO day_status VALUES ('s', %s, true, %s)",
                     (day, "weekend" if day.weekday() >= 5 else "weekday"))
    conn.execute("INSERT INTO baseline VALUES ('s', 'k', 'weekday', 3, 1.5, 0.5, 5)")
    conn.commit()
    b = Baseline.load(conn)
    assert b.days == {("s", "weekday"): 5, ("s", "weekend"): 4}
    assert b.stat("s", "k", "weekday", 3) == Stat(1.5, 0.5)


def test_lookup_doc_sums_linked_templates():
    day = date(2026, 10, 20)   # Tuesday
    b = Baseline({("s", "s:T1", "weekday", 10): Stat(3, 1), ("s", "s:T2", "weekday", 10): Stat(5, 2),
                  ("s", "s:T2", "weekday", 11): Stat(7, 1)}, {("s", "weekday"): 4})
    lk = b.lookup_doc("s", ["s:T1", "s:T2", "s:T9"], day, 10)   # T9 has no stats: adds nothing
    assert lk == Lookup(Stat(8, 3), Stat(7, 1), 4, False)
    assert b.lookup_doc("s", ["s:T9"], day, 10) == Lookup(None, None, 4, False)   # floor only


def test_compact_lookup_matches_dict_form():
    stats = {("s", "k", "weekday", 10): Stat(5, 1), ("s", "k", "weekend", 23): Stat(2, 0.5)}
    b = Baseline(stats, {("s", "weekday"): 5})
    assert b.stat("s", "k", "weekday", 10) == Stat(5, 1)
    assert b.stat("s", "k", "weekend", 23) == Stat(2, 0.5)
    assert b.stat("s", "k", "weekday", 11) is None and b.stat("s", "x", "weekday", 10) is None
    assert b.has("s", "k") and not b.has("s", "x")


def test_zero_mean_and_std_are_real_stats():
    b = Baseline({("s", "k", "weekday", 1): Stat(0.0, 0.0)}, {})
    assert b.stat("s", "k", "weekday", 1) == Stat(0.0, 0.0)


def test_compact_memory_per_row():
    stats = {("s", f"s:T{t:05d}", dt, h): Stat(t, 1) for t in range(2000) for dt in ("weekday", "weekend") for h in range(24)}
    tracemalloc.start(); b = Baseline(stats, {}); cur, _ = tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert cur / len(stats) < 40   # was 331 B/row in the dict form


def _seed(conn, rows):
    for r in rows:
        conn.execute("INSERT INTO baseline VALUES (%s, %s, %s, %s, %s, %s, 5)", r)
    conn.commit()


def test_load_services_filter(conn):
    _seed(conn, [("a", "a:T1", "weekday", 1, 1.0, 1.0), ("b", "b:T1", "weekday", 1, 1.0, 1.0)])
    b = Baseline.load(conn, services=["a"])
    assert b.has("a", "a:T1") and not b.has("b", "b:T1")


def test_update_and_drop():
    b = Baseline({("a", "k", "weekday", 1): Stat(1, 1)}, {("a", "weekday"): 5})
    b.update(Baseline({("b", "k", "weekday", 1): Stat(2, 1)}, {("b", "weekday"): 3}))
    assert b.stat("b", "k", "weekday", 1) == Stat(2, 1) and b.days[("b", "weekday")] == 3
    b.drop("a")
    assert not b.has("a", "k") and ("a", "weekday") not in b.days


def test_update_replaces_same_key():
    b = Baseline({("a", "k", "weekday", 1): Stat(1, 1), ("a", "k", "weekday", 2): Stat(9, 9)}, {("a", "weekday"): 5})
    b.update(Baseline({("a", "k", "weekday", 1): Stat(2, 2)}, {("a", "weekday"): 3}))
    assert b.stat("a", "k", "weekday", 1) == Stat(2, 2) and b.stat("a", "k", "weekday", 2) is None
    assert b.days[("a", "weekday")] == 3


def test_load_parity_with_dict_form(conn):
    rows = [(s, f"{s}:T{t}", dt, h, t * 1.5 + h, 0.0 if h % 3 == 0 else h / 7)
            for s in ("a", "b") for t in range(3) for dt in ("weekday", "weekend") for h in range(0, 24, 2)]
    rows.append(("a", "*total", "weekday", 5, 0.0, 0.0))
    _seed(conn, rows)
    for d in range(5, 10):
        conn.execute("INSERT INTO day_status VALUES ('a', %s, true, 'weekday')", (date(2026, 10, d),))
    conn.commit()
    old = Baseline({(s, k, dt, h): Stat(m, sd) for s, k, dt, h, m, sd in rows}, {("a", "weekday"): 5})
    new = Baseline.load(conn)
    for s, k, dt, h, m, sd in rows:
        assert new.stat(s, k, dt, h) == Stat(m, sd)
    for day in (date(2026, 10, 20), date(2026, 10, 17)):
        for s, k in {(r[0], r[1]) for r in rows} | {("a", "zz")}:
            for h in range(24):
                assert new.lookup(s, k, day, h) == old.lookup(s, k, day, h)
    sub = Baseline.load(conn, [("a", "a:T1")])
    assert sub.has("a", "a:T1") and not sub.has("a", "a:T0")


def test_two_streamed_loads_in_one_transaction(conn):
    _seed(conn, [("a", "a:T1", "weekday", 1, 1.0, 1.0)])
    b1, b2 = Baseline.load(conn), Baseline.load(conn)
    assert b1.has("a", "a:T1") and b2.has("a", "a:T1")

from datetime import date

from logai2.owner.baseline import Baseline, Lookup, Stat, judge

LK = lambda this, nxt=None, days=5, upper_only=False: Lookup(this, nxt, days, upper_only)


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


def test_score(cfg):
    assert judge(18, "s:T1", "ERROR", LK(Stat(3, 2)), cfg).score == 2
    assert judge(1, "s:T1", "ERROR", LK(Stat(0, 0)), cfg).score == 1.0


def test_weekend_falls_back_to_weekday():
    b = Baseline({("s", "k", "weekday", 10): Stat(5, 1)}, {("s", "weekday"): 5, ("s", "weekend"): 1})
    lk = b.lookup("s", "k", date(2026, 10, 18), 10)          # Sunday
    assert lk.this == Stat(5, 1) and lk.upper_only and lk.days == 5


def test_lookup_next_hour_wraps_and_missing():
    b = Baseline({("s", "k", "weekend", 0): Stat(1, 1), ("s", "k", "weekend", 23): Stat(2, 1)},
                 {("s", "weekend"): 2})
    lk = b.lookup("s", "k", date(2026, 10, 17), 23)          # Saturday
    assert lk == Lookup(Stat(2, 1), Stat(1, 1), 2, False)
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
    assert b.stats == {("s", "k", "weekday", 3): Stat(1.5, 0.5)}

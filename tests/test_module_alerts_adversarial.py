"""Adversarial edge cases for Task 42 time math (scratch, not committed).
`at` = END of a closed minute; API minutes are STARTS; window = [start, end)."""
from datetime import datetime, timezone

import pytest

from logai2.module_alerts import module_alerts, parse_window
from logai2.timeutil import TZ
from tests.test_module_alerts import chg, mins, seed_module, t, tmpl


def basic(conn):
    """first high 08:00 (NORMAL streak 1), WARMING 08:01, ALERTING 08:02..08:05, COOLING from 08:06."""
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 2), "NORMAL", "WARMING")
    chg(conn, "a", "a:T1", t(8, 3), "WARMING", "ALERTING")
    chg(conn, "a", "a:T1", t(8, 7), "ALERTING", "COOLING")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 9, 10)


# ---- period edges vs window edges -------------------------------------------------------

def test_window_starts_at_warming_minute(conn):
    basic(conn)
    it = module_alerts(conn, "M1", t(8, 1), t(8, 15))[0]
    assert it["periods"] == [{"start": t(8, 0), "end": t(8, 6)}]      # not clipped
    assert it["alerting_minutes"] == 5 and it["module"]["total"] == 50
    assert it["module"]["series"][0]["minute"] == t(8, 1)


def test_window_ends_exactly_at_cooling_minute(conn):
    basic(conn)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 6))[0]
    assert it["alerting_minutes"] == 6 and it["module"]["total"] == 60
    assert it["periods"] == [{"start": t(8, 0), "end": None}]           # closing change is outside the query
    assert it["peak_state"] == "ALERTING"


def test_window_ends_one_after_cooling_minute(conn):
    basic(conn)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 7))[0]
    assert it["periods"] == [{"start": t(8, 0), "end": t(8, 6)}] and it["module"]["total"] == 60


def test_window_starts_exactly_at_cooling_minute(conn):
    basic(conn)
    assert module_alerts(conn, "M1", t(8, 6), t(8, 15)) == []


def test_window_is_exactly_the_last_alerting_minute(conn):
    basic(conn)
    it = module_alerts(conn, "M1", t(8, 5), t(8, 6))[0]
    assert it["alerting_minutes"] == 1 and it["module"]["total"] == 10 and it["peak_state"] == "ALERTING"


def test_window_ends_on_first_high_minute(conn):
    """Spec §3: the 1st high minute (NORMAL streak 1) is counted. Window [07:50, 08:01) holds 08:00 only;
    the NORMAL->WARMING change that reveals it is at 08:02 = end + 60."""
    basic(conn)
    items = module_alerts(conn, "M1", t(7, 50), t(8, 1))
    assert len(items) == 1 and items[0]["alerting_minutes"] == 1 and items[0]["module"]["total"] == 10
    assert items[0]["peak_state"] == "WARMING"


def test_one_minute_window_on_first_high_minute(conn):
    basic(conn)
    items = module_alerts(conn, "M1", t(8, 0), t(8, 1))
    assert len(items) == 1 and items[0]["module"]["total"] == 10


def test_one_minute_window_on_warming_minute(conn):
    basic(conn)
    it = module_alerts(conn, "M1", t(8, 1), t(8, 2))[0]
    assert it["peak_state"] == "WARMING" and it["module"]["total"] == 10
    assert it["periods"] == [{"start": t(8, 0), "end": None}]


def test_period_starts_before_and_ends_after_window(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(7, 0), "WARMING", "ALERTING")
    chg(conn, "a", "a:T1", t(9, 0), "ALERTING", "COOLING")
    mins(conn, "a", "a:T1", "M1", t(7, 55), 20, 2)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 10))[0]
    assert it["periods"] == [{"start": t(6, 59), "end": None}]
    assert it["alerting_minutes"] == 10 and it["module"]["total"] == 20 and it["peak_state"] == "ALERTING"


def test_alerting_cooling_alerting_two_periods(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 2), "NORMAL", "WARMING")      # first 08:00, W 08:01
    chg(conn, "a", "a:T1", t(8, 3), "WARMING", "ALERTING")    # A 08:02
    chg(conn, "a", "a:T1", t(8, 6), "ALERTING", "COOLING")    # C 08:05
    chg(conn, "a", "a:T1", t(8, 7), "COOLING", "ALERTING")    # A 08:06 (no back-dating)
    chg(conn, "a", "a:T1", t(8, 10), "ALERTING", "COOLING")   # C 08:09
    mins(conn, "a", "a:T1", "M1", t(8, 0), 12, 1)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert it["periods"] == [{"start": t(8, 0), "end": t(8, 5)}, {"start": t(8, 6), "end": t(8, 9)}]
    assert it["alerting_minutes"] == 8 and it["module"]["total"] == 8 and it["module"]["peak"] == 1
    assert [s["minute"] for s in it["module"]["series"]] == [t(8, m) for m in (0, 1, 2, 3, 4, 6, 7, 8)]


def test_change_exactly_at_start_plus_60(conn):
    seed_module(conn)
    for k in ("a:C", "a:W"):
        tmpl(conn, "a", k)
    chg(conn, "a", "a:C", t(7, 30), "WARMING", "ALERTING")
    chg(conn, "a", "a:C", t(8, 1), "ALERTING", "COOLING")     # minute 08:00 is COOLING
    chg(conn, "a", "a:W", t(8, 1), "NORMAL", "WARMING")       # minute 08:00 WARMING, first high 07:59
    for k in ("a:C", "a:W"):
        mins(conn, "a", k, "M1", t(7, 59), 5, 1)
    items = module_alerts(conn, "M1", t(8, 0), t(8, 4))
    assert [i["template_id"] for i in items] == ["a:W"]
    assert items[0]["periods"] == [{"start": t(7, 59), "end": None}] and items[0]["module"]["total"] == 4


def test_change_exactly_at_end_plus_60(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 5), "WARMING", "ALERTING")    # A from 08:04
    chg(conn, "a", "a:T1", t(8, 11), "ALERTING", "COOLING")   # minute 08:10 COOLING, outside [08:00, 08:10)
    mins(conn, "a", "a:T1", "M1", t(8, 0), 12, 1)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 10))[0]
    assert it["periods"] == [{"start": t(8, 4), "end": None}]
    assert it["alerting_minutes"] == 6 and it["module"]["total"] == 6


def test_backdated_start_before_window_in_window_change(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 1), "NORMAL", "WARMING")      # W 08:00, first 07:59
    mins(conn, "a", "a:T1", "M1", t(7, 59), 6, 1)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 5))[0]
    assert it["periods"] == [{"start": t(7, 59), "end": None}]
    assert it["alerting_minutes"] == 5 and it["module"]["total"] == 5


def test_backdated_start_before_window_before_row(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 0), "NORMAL", "WARMING")      # at == start: before-row; W 07:59, first 07:58
    mins(conn, "a", "a:T1", "M1", t(7, 58), 7, 1)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 5))[0]
    assert it["periods"] == [{"start": t(7, 58), "end": None}]
    assert it["alerting_minutes"] == 5 and it["module"]["total"] == 5


def test_warming_normal_warming(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 2), "NORMAL", "WARMING")      # first 08:00, W 08:01
    chg(conn, "a", "a:T1", t(8, 3), "WARMING", "NORMAL")      # N 08:02
    chg(conn, "a", "a:T1", t(8, 5), "NORMAL", "WARMING")      # first 08:03, W 08:04
    chg(conn, "a", "a:T1", t(8, 6), "WARMING", "NORMAL")      # N 08:05
    mins(conn, "a", "a:T1", "M1", t(8, 0), 7, 1)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert it["periods"] == [{"start": t(8, 0), "end": t(8, 2)}, {"start": t(8, 3), "end": t(8, 5)}]
    assert it["alerting_minutes"] == 4 and it["module"]["total"] == 4 and it["peak_state"] == "WARMING"


def test_peak_state_alerting_only_after_window(conn):
    basic(conn)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 2))[0]
    assert it["peak_state"] == "WARMING" and it["alerting_minutes"] == 2
    assert module_alerts(conn, "M1", t(8, 0), t(8, 3))[0]["peak_state"] == "ALERTING"


def test_peak_state_alerting_only_before_window(conn):
    """ALERTING 08:02..08:05 then COOLING->ALERTING never; a second WARMING run in the window must not
    inherit ALERTING from the earlier period."""
    basic(conn)
    chg(conn, "a", "a:T1", t(8, 10), "COOLING", "NORMAL")     # N 08:09
    chg(conn, "a", "a:T1", t(8, 13), "NORMAL", "WARMING")     # first 08:11, W 08:12
    mins(conn, "a", "a:T1", "M1", t(8, 9), 5, 1)
    it = module_alerts(conn, "M1", t(8, 11), t(8, 13))[0]
    assert it["peak_state"] == "WARMING" and it["periods"] == [{"start": t(8, 11), "end": None}]


# ---- module / keys ----------------------------------------------------------------------

def test_template_only_other_module_plus_doc_key_for_module(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M2", t(8, 0), 5, 3)
    chg(conn, "a", "doc:D1", t(8, 0), "WARMING", "ALERTING")
    mins(conn, "a", "doc:D1", "M1", t(8, 0), 5, 3)
    assert module_alerts(conn, "M1", t(8, 0), t(8, 15)) == []


def test_same_at_for_different_keys(conn):
    seed_module(conn)
    for k in ("a:T1", "a:T2", "a:T3"):
        tmpl(conn, "a", k)
    chg(conn, "a", "a:T1", t(8, 2), "NORMAL", "WARMING")      # first 08:00
    chg(conn, "a", "a:T2", t(8, 1), "NORMAL", "WARMING")      # first 07:59
    chg(conn, "a", "a:T2", t(8, 2), "WARMING", "ALERTING")
    chg(conn, "a", "a:T3", t(7, 30), "WARMING", "ALERTING")
    chg(conn, "a", "a:T3", t(8, 2), "ALERTING", "COOLING")    # C 08:01
    chg(conn, "a", "*error", t(8, 2), "NORMAL", "WARMING")
    for k, c in (("a:T1", 1), ("a:T2", 2), ("a:T3", 3)):
        mins(conn, "a", k, "M1", t(7, 58), 12, c)
    by = {i["template_id"]: i for i in module_alerts(conn, "M1", t(8, 0), t(8, 10))}
    assert set(by) == {"a:T1", "a:T2", "a:T3"}
    assert by["a:T1"]["periods"] == [{"start": t(8, 0), "end": None}] and by["a:T1"]["module"]["total"] == 10
    assert by["a:T1"]["peak_state"] == "WARMING"
    assert by["a:T2"]["periods"] == [{"start": t(7, 59), "end": None}] and by["a:T2"]["module"]["total"] == 20
    assert by["a:T2"]["peak_state"] == "ALERTING"
    assert by["a:T3"]["periods"] == [{"start": t(7, 29), "end": t(8, 1)}] and by["a:T3"]["module"]["total"] == 3


# ---- series granularity / days ----------------------------------------------------------

def test_one_day_minute_series_vs_one_day_plus_minute_hour_series(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 3, 1)
    day = module_alerts(conn, "M1", t(0, 0), t(0, 0, 15))[0]["module"]["series"]
    assert day == [{"minute": t(8, m), "count": 1} for m in range(3)]
    more = module_alerts(conn, "M1", t(0, 0), t(0, 1, 15))[0]["module"]["series"]
    assert more == [{"hour": t(8, 0), "count": 3}]


def test_midnight_window_only_second_day_rows(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(23, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M1", t(0, 0, 15), 5, 4)
    it = module_alerts(conn, "M1", t(23, 50), t(0, 10, 15))[0]
    assert it["module"]["total"] == 20 and it["alerting_minutes"] == 20
    assert it["module"]["series"][0] == {"minute": t(0, 0, 15), "count": 4}


def test_hour_series_buckets_local_hours(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(23, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M1", t(23, 59), 2, 1)              # 23:59 day 14, 00:00 day 15
    s = module_alerts(conn, "M1", t(0, 0), t(0, 0, 16))[0]["module"]["series"]
    assert s == [{"hour": t(23, 0), "count": 1}, {"hour": t(0, 0, 15), "count": 1}]


# ---- parse_window -----------------------------------------------------------------------

NOW = 1792461600.0                                  # 2026-10-20T02:00:00Z = 09:00 local


def pw(f, to, now=NOW, days=90):
    return parse_window({"module_code": "M", "from": f, "to": to}, now, days)


def test_from_equals_to_on_minute_boundary():
    assert pw("1792458000", "1792458000")[3:] == (1792458000.0, 1792458060.0)
    assert pw("2026-10-20T01:00:00.000Z", "2026-10-20T01:00:00.000Z")[3:] == (1792458000.0, 1792458060.0)


def test_ms_threshold():
    assert pw("100000000000", "100000000000")[1] == 1e11                 # exactly 1e11 = seconds
    assert pw("100000000001", "100000000001")[1] == pytest.approx(1e8 + 0.001)   # > 1e11 = ms
    assert pw("1792458000000", "1792458060000")[3:] == (1792458000.0, 1792458060.0)
    assert pw("1792458000000.0", "1792458000000.9")[1:3] == (1792458000.0, pytest.approx(1792458000.0009))


def test_iso_offsets():
    assert pw("2026-10-20T08:00:00+07:00", "2026-10-20T08:05:00.000+07:00")[3:] == (1792458000.0, 1792458300.0)
    assert pw("2026-10-20T01:00:00", "2026-10-20T01:05:00.000")[3:] == (1792458000.0, 1792458300.0)   # naive = UTC
    assert pw("2026-10-20T01:00:30.500Z", "2026-10-20T01:00:31.200Z")[3:] == (1792458000.0, 1792458060.0)
    with pytest.raises(ValueError):
        pw("2026-10-20T08:00:00+07:00", "2026-10-20T00:59:00Z")   # 01:00Z > 00:59Z


def test_clamp_exact_retention_boundary():
    oldest = datetime(2026, 7, 22, tzinfo=TZ).timestamp()
    assert pw(str(oldest), "1792458000")[3] == oldest
    assert pw(str(oldest - 0.001), "1792458000")[3] == oldest
    assert pw(str(oldest + 61), "1792458000")[3] == oldest + 60


def test_clamp_uses_local_date_not_utc():
    now = datetime(2026, 10, 19, 17, 30, tzinfo=timezone.utc).timestamp()   # 00:30 local on 10-20
    assert pw("0", str(now), now=now)[3] == datetime(2026, 7, 22, tzinfo=TZ).timestamp()
    now = datetime(2026, 10, 19, 16, 59, 59, tzinfo=timezone.utc).timestamp()   # 23:59:59 local on 10-19
    assert pw("0", str(now), now=now)[3] == datetime(2026, 7, 21, tzinfo=TZ).timestamp()


def test_clamp_to_now():
    assert pw("1792458000", str(NOW + 1))[4] == NOW
    assert pw("1792458000", str(NOW + 1), now=NOW + 30)[4] == NOW + 60     # now mid-minute: rounded up
    m, f, to, s, e = pw("1792458000", "1792465200")
    assert (f, to) == (1792458000.0, 1792465200.0)                         # as sent, unclamped


def test_both_before_retention_and_both_future():
    oldest = datetime(2026, 7, 22, tzinfo=TZ).timestamp()
    assert pw("0", "100")[3:] == (oldest, oldest + 60)
    s, e = pw(str(NOW + 3600), str(NOW + 7200))[3:]
    assert e - s == 60 and s >= NOW


def test_bad_values():
    for f, to in (("nan", "1"), ("inf", "1"), ("", "1"), ("1", " "), ("2026-13-01T00:00:00Z", "1")):
        with pytest.raises(ValueError):
            pw(f, to)

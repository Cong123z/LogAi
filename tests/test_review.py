from datetime import date, datetime

from logai2.review import keys_by_hour, minutes_by_hour, periods, suggest
from logai2.timeutil import TZ


def local(*args):
    return datetime(*args, tzinfo=TZ)


def chg(key, frm, to, at, service="s"):
    return {"service": service, "key": key, "from_state": frm, "to_state": to, "at": at}


def test_suggest_widens_to_whole_hours():
    ch = [chg("s:T1", "NORMAL", "WARMING", local(2026, 10, 14, 14, 20)),
          chg("s:T1", "COOLING", "NORMAL", local(2026, 10, 14, 15, 5))]
    assert [(s["day"], s["hour_from"], s["hour_to"]) for s in suggest(ch, set())] == [(date(2026, 10, 14), 14, 16)]


def test_suggest_splits_at_midnight_and_skips_decided():
    ch = [chg("s:T1", "NORMAL", "WARMING", local(2026, 10, 14, 22, 10)),
          chg("s:T1", "WARMING", "ALERTING", local(2026, 10, 14, 22, 12)),
          chg("*total", "NORMAL", "WARMING", local(2026, 10, 14, 23, 30)),
          chg("s:T1", "COOLING", "NORMAL", local(2026, 10, 15, 1, 40)),
          chg("*total", "WARMING", "NORMAL", local(2026, 10, 15, 0, 10))]
    out = suggest(ch, {("s", date(2026, 10, 14), 22)})
    assert [(s["day"], s["hour_from"], s["hour_to"], s["keys"]) for s in out] == [
        (date(2026, 10, 14), 23, 24, ["*total", "s:T1"]),
        (date(2026, 10, 15), 0, 2, ["*total", "s:T1"]),
    ]
    assert all(s["service"] == "s" for s in out)


def test_suggest_merges_overlaps_and_keeps_open_period_to_now():
    ch = [chg("s:T1", "NORMAL", "WARMING", local(2026, 10, 14, 9, 0)),
          chg("s:T1", "WARMING", "NORMAL", local(2026, 10, 14, 9, 3)),
          chg("s:T2", "NORMAL", "WARMING", local(2026, 10, 14, 9, 50)),
          chg("s:T2", "WARMING", "NORMAL", local(2026, 10, 14, 10, 30)),
          chg("s:T3", "NORMAL", "WARMING", local(2026, 10, 14, 12, 0))]   # still open
    out = suggest(ch, set(), now=local(2026, 10, 14, 13, 15))
    assert [(s["hour_from"], s["hour_to"], s["keys"]) for s in out] == [
        (9, 11, ["s:T1", "s:T2"]), (12, 14, ["s:T3"])]


def test_minutes_by_hour_is_a_union_across_keys():
    t0 = datetime(2026, 10, 20, 2, 10, tzinfo=TZ).timestamp()
    ps = [("*total", t0, t0 + 30 * 60), ("k1", t0 + 20 * 60, t0 + 70 * 60)]
    m = minutes_by_hour(ps)
    assert m[(date(2026, 10, 20), 2)] == 50 and m[(date(2026, 10, 20), 3)] == 20
    assert keys_by_hour(ps)[(date(2026, 10, 20), 3)] == {"k1"}


def test_periods_close_open_ones_at_now():
    at = datetime(2026, 10, 20, 2, 0, tzinfo=TZ)
    changes = [{"service": "s", "key": "k", "at": at, "from_state": "NORMAL", "to_state": "WARMING"}]
    assert periods(changes, at.timestamp() + 600) == [("k", at.timestamp(), at.timestamp() + 600)]


def test_periods_states_argument():
    t = lambda h, m: local(2026, 10, 14, h, m)
    ch = [chg("s:T1", "NORMAL", "WARMING", t(8, 1)), chg("s:T1", "WARMING", "ALERTING", t(8, 3)),
          chg("s:T1", "ALERTING", "COOLING", t(8, 5)), chg("s:T1", "COOLING", "ALERTING", t(8, 6))]
    s = lambda d: d.timestamp()
    assert periods(ch, s(t(9, 0)), {"WARMING", "ALERTING"}) == [("s:T1", s(t(8, 1)), s(t(8, 5))), ("s:T1", s(t(8, 6)), s(t(9, 0)))]
    assert periods(ch, s(t(9, 0)), {"ALERTING"}) == [("s:T1", s(t(8, 3)), s(t(8, 5))), ("s:T1", s(t(8, 6)), s(t(9, 0)))]

from datetime import datetime, timezone

from logai2.owner.counts import OpenMinutes
from logai2.timeutil import day_hour

# 2026-10-20 10:00 local (Asia/Ho_Chi_Minh, UTC+7)
M = int(datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc).timestamp()) // 60


def test_close_includes_empty_minutes_and_service_keys():
    om = OpenMinutes(start_minute=M)
    om.add("s", "s:T00001", "ERROR", "M1", M, 3)
    closed = om.close_before(M + 3, ["s", "t"])
    assert [c.minute for c in closed] == [M, M + 1, M + 2]
    assert (closed[0].day, closed[0].hour) == day_hour(M) and closed[0].hour == 10
    assert closed[0].keys[("s", "s:T00001")] == 3
    assert closed[0].keys[("s", "*error")] == 3 and closed[0].keys[("s", "*total")] == 3
    assert closed[0].keys[("t", "*total")] == 0 and closed[0].keys[("t", "*error")] == 0
    assert closed[1].keys[("s", "s:T00001")] == 0          # silent, same hour
    assert closed[1].keys[("s", "*total")] == 0
    assert closed[0].modules[("s", "s:T00001", "M1")] == 3
    assert closed[1].modules == {} and closed[1].levels == {}
    assert om.next_to_close == M + 3


def test_silent_key_forgotten_next_hour():
    om = OpenMinutes(M + 58)
    om.add("s", "s:T00001", "INFO", "", M + 59, 1)
    closed = om.close_before(M + 61, ["s"])
    assert ("s", "s:T00001") not in closed[0].keys
    assert closed[1].keys[("s", "s:T00001")] == 1
    assert closed[2].hour == 11 and ("s", "s:T00001") not in closed[2].keys
    assert om.hour_keys.keys() == {day_hour(M + 60)}


def test_levels_error_keys_and_most_severe():
    om = OpenMinutes(M)
    for level in ("warning", "Error", "INFO", "fatal", "WARN", "CRITICAL"):
        om.add("s", "s:T1", level, "", M, 1)
    om.add("s", "s:T2", "WARN", "", M, 2)
    om.add("s", "s:T2", "INFO", "", M, 1)
    (c,) = om.close_before(M + 1, ["s"])
    assert c.keys[("s", "*total")] == 9 and c.keys[("s", "*error")] == 3
    assert c.levels[("s", "s:T1")].upper() == "FATAL"
    assert c.levels[("s", "s:T2")] == "WARN"


def test_late_count_is_dropped():
    om = OpenMinutes(M); om.close_before(M + 1, [])
    om.add("s", "s:T1", "INFO", "", M, 1)
    assert om.late == 1
    assert om.close_before(M + 2, [])[0].keys == {}


def test_first_start_backlog_is_linear():
    om = OpenMinutes(M)
    closed = om.close_before(M + 2880, ["s"])
    assert len(closed) == 2880 and om.next_to_close == M + 2880
    assert om.close_before(M + 2880, ["s"]) == []


def test_level_class_keys_add_up_to_total():
    om = OpenMinutes(M)
    for level, n in (("ERROR", 1), ("Fatal", 2), ("WARN", 3), ("warning", 4), ("INFO", 5), ("debug", 6), ("notice", 7)):
        om.add("s", f"s:T{n}", level, "", M, n)
    c, silent = om.close_before(M + 2, ["s", "t"])
    k = c.keys
    assert (k[("s", "*error")], k[("s", "*warn")], k[("s", "*info")]) == (3, 7, 18)
    assert k[("s", "*error")] + k[("s", "*warn")] + k[("s", "*info")] == k[("s", "*total")] == 28
    for key in ("*total", "*error", "*warn", "*info"):
        assert silent.keys[("s", key)] == 0 and c.keys[("t", key)] == 0

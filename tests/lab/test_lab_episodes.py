import pytest

from logai2.config import AlertConfig
from scripts.lab.episodes import Episode, Tracker, episodes

CFG = AlertConfig()      # warm 2, alert 3, cool 3


def flags(pattern, start=100, score=None):
    return [(start + i, c == "H", (score or {}).get(i, 1.0 if c == "H" else 0.1)) for i, c in enumerate(pattern)]


def test_two_highs_warm_then_normal_is_no_episode():
    assert episodes(flags("HHLLLL"), CFG) == []


def test_five_highs_three_lows_one_episode():
    eps = episodes(flags("HHHHHLLL", score={3: 9.0}), CFG)
    # start = first high; ALERTING from the 3rd high, COOLING from the first low, NORMAL on the 3rd low (minute 107)
    assert eps == [Episode(100, 107, 9.0, 5)]


def test_start_is_the_first_high_of_the_run_that_left_normal():
    eps = episodes(flags("LLHLHHHHLLL"), CFG)       # the lone high at 2 is reset; the run starts at 4
    assert eps[0].start == 104


def test_cooling_interrupted_by_a_high_stays_one_episode():
    eps = episodes(flags("HHHLLHHLLL"), CFG)        # ALERTING m2-m4... see counts below
    assert len(eps) == 1 and eps[0].start == 100 and eps[0].end == 109
    # alerted: m2 ALERTING, m3 COOLING, m4 COOLING, m5 ALERTING, m6 ALERTING, m7 COOLING, m8 COOLING = 7
    assert eps[0].alerted == 7


def test_open_at_close_returns_with_window_end():
    t = Tracker(CFG)
    out = [t.step(m, True, 2.0) for m in range(100, 106)]
    assert out == [None] * 6
    assert t.close(106) == Episode(100, 106, 2.0, 4)
    assert t.close(107) is None                       # state was reset


def test_close_while_warming_or_normal_is_none():
    t = Tracker(CFG)
    t.step(1, True, 1.0), t.step(2, True, 1.0)        # WARMING
    assert t.close(3) is None


def test_two_episodes_in_a_row_and_changes_log():
    t = Tracker(CFG)
    out = [t.step(m, h, 1.0) for m, h in enumerate([1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0])]
    assert [e for e in out if e] == [Episode(0, 5, 1.0, 3), Episode(6, 11, 1.0, 3)]
    assert [c for c in t.changes[:4]] == [(1, "WARMING"), (2, "ALERTING"), (3, "COOLING"), (5, "NORMAL")]

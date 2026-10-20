from datetime import date

from logai2.owner.baseline import Baseline, Stat
from logai2.owner.counts import ClosedMinute
from logai2.owner.detect import AlertRow, Detector, step

DAY = date(2026, 10, 20)  # Tuesday → weekday


def cm(minute, keys):
    return ClosedMinute(minute, DAY, 10, keys)


def test_step_matches_logai_engine_sequence(cfg):
    r, seq = AlertRow("s", "k"), []
    for h in [1, 1, 1, 1, 1, 0, 0, 0]:
        r = step(r, bool(h), cfg.alert); seq.append(r.state)
    assert seq == ["NORMAL", "WARMING", "ALERTING", "ALERTING", "ALERTING",
                   "COOLING", "COOLING", "NORMAL"]   # 3 highs in a row alert, 3 lows recover


def test_step_warming_drops_on_low(cfg):
    r = step(step(AlertRow("s", "k"), True, cfg.alert), True, cfg.alert)
    assert r.state == "WARMING"
    assert step(r, False, cfg.alert).state == "NORMAL"


def test_burst_alerts_and_recovers(cfg):
    det = Detector(cfg, Baseline({("s", "s:T1", "weekday", 10): Stat(3, 1)}, {("s", "weekday"): 5}),
                   {}, {("s", "s:T1"): "ERROR"}, {})
    changes, seq = [], []
    for m in range(8):
        # zeros: the key is absent from cm.keys, as after its hour ends
        changes += det.run(cm(m, {("s", "s:T1"): 40} if m < 5 else {}), historic=False)
        seq.append(det.states[("s", "s:T1")].state)
    assert seq == ["NORMAL", "WARMING", "ALERTING", "ALERTING", "ALERTING",
                   "COOLING", "COOLING", "NORMAL"]
    assert [(c.from_state, c.to_state, c.at, c.count) for c in changes] == [
        ("NORMAL", "WARMING", 120, 40), ("WARMING", "ALERTING", 180, 40),
        ("ALERTING", "COOLING", 360, 0), ("COOLING", "NORMAL", 480, 0)]
    assert changes[0].band_high == 6
    assert det.states[("s", "s:T1")].since == 480
    assert det.not_normal() == set()


def test_not_normal_and_row_fields(cfg):
    det = Detector(cfg, Baseline({}, {}), {}, {("s", "s:T1"): "ERROR"}, {})
    for m in range(3):
        det.run(cm(m, {("s", "s:T1"): 10}), historic=False)
    row = det.states[("s", "s:T1")]
    assert det.not_normal() == {("s", "s:T1")}
    assert (row.state, row.since, row.count, row.score, row.band_high) == ("ALERTING", 180, 10, 2.0, None)


def test_historic_minute_changes_nothing(cfg):
    det = Detector(cfg, Baseline({}, {}), {}, {("s", "s:T1"): "ERROR"}, {})
    for m in range(5):
        assert det.run(cm(m, {("s", "s:T1"): 40}), historic=True) == []
    assert det.states == {}


def test_level_falls_back_to_minute_levels(cfg):
    det = Detector(cfg, Baseline({}, {}), {}, {}, {})
    c = cm(0, {("s", "s:T1"): 40, ("s", "s:T2"): 40})
    c.levels = {("s", "s:T1"): "ERROR"}
    det.run(c, historic=False)
    assert set(det.states) == {("s", "s:T1")}   # T2 has no level → INFO → never high


def test_doc_key_sums_linked_templates(cfg):
    stats = {("s", k, "weekday", 10): Stat(0, 0.5) for k in ("s:T1", "s:T2", "doc:DOC-1")}
    det = Detector(cfg, Baseline(stats, {("s", "weekday"): 5}),
                   {("s", "s:T1"): "DOC-1", ("s", "s:T2"): "DOC-1"},
                   {("s", "s:T1"): "INFO", ("s", "s:T2"): "ERROR"}, {})
    det.run(cm(0, {("s", "s:T1"): 4, ("s", "s:T2"): 4}), historic=False)
    assert set(det.states) == {("s", "doc:DOC-1")}   # each 4 < floor 5; doc 8 ≥ floor, level ERROR
    assert det.states[("s", "doc:DOC-1")].count == 8


def test_non_consecutive_highs_reset_streak(cfg):
    for gap in ({("s", "s:T1"): 1}, {}):   # a low minute, then the key absent from the minute
        det = Detector(cfg, Baseline({}, {}), {}, {("s", "s:T1"): "ERROR"}, {})
        seq = []
        for keys in ({("s", "s:T1"): 40}, gap, {("s", "s:T1"): 40}):
            det.run(cm(len(seq), keys), historic=False)
            r = det.states[("s", "s:T1")]
            seq.append((r.state, r.streak))
        assert seq == [("NORMAL", 1), ("NORMAL", 0), ("NORMAL", 1)]


def test_doc_key_band_is_sum_of_linked_bands(cfg):
    # T1 3±1, T2 5±2 → doc band high 8 + 3·3 = 17 (not the doc key's own, absent, baseline row)
    stats = {("s", "s:T1", "weekday", 10): Stat(3, 1), ("s", "s:T2", "weekday", 10): Stat(5, 2)}
    links = {("s", "s:T1"): "DOC-1", ("s", "s:T2"): "DOC-1"}
    levels = {("s", "s:T1"): "ERROR", ("s", "s:T2"): "ERROR"}
    inside = Detector(cfg, Baseline(stats, {("s", "weekday"): 5}), links, levels, {})
    inside.run(cm(0, {("s", "s:T1"): 6, ("s", "s:T2"): 11}), historic=False)       # doc 17: on the edge
    assert ("s", "doc:DOC-1") not in inside.states
    above = Detector(cfg, Baseline(stats, {("s", "weekday"): 5}), links, levels, {})
    for m in range(3):
        above.run(cm(m, {("s", "s:T1"): 6, ("s", "s:T2"): 12}), historic=False)    # doc 18 > 17
    row = above.states[("s", "doc:DOC-1")]
    assert (row.state, row.band_high, row.count) == ("ALERTING", 17, 18)

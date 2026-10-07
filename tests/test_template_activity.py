"""Recent per-template counts and each template's normal (24 h) rate."""
from __future__ import annotations

from logai.features.template_activity import TemplateActivity, empty_counts, is_active, recent

H = 3600.0


def _feed(activity, service, template_id, start, end, per_minute):
    t = start
    while t < end:
        for i in range(per_minute):
            activity.record(service, template_id, t + i * (60.0 / per_minute))
        t += 60.0


def test_counts_cover_only_the_last_15_and_30_minutes():
    activity = TemplateActivity()
    now = 100 * 60.0
    for minutes_ago, n in [(45, 9), (20, 3), (5, 2), (0, 1)]:
        for _ in range(n):
            activity.record("api", "T1", now - minutes_ago * 60)
    activity.record("web", "T1", now)
    api = activity.counts(now, "api")["T1"]
    assert (api["count_15m"], api["count_30m"], api["rate_per_min"]) == (3, 6, 0.2)
    assert activity.counts(now)["T1"]["count_15m"] == 4  # summed over services
    assert activity.counts(now + 16 * 60, "api")["T1"]["count_30m"] == 3
    assert recent({}, "T9") == empty_counts() and not is_active({}, "T9")


def test_out_of_order_event_lands_in_newest_bucket():
    activity = TemplateActivity()
    activity.record("api", "T1", 600.0)
    activity.record("api", "T1", 500.0)
    assert activity.counts(600.0, "api")["T1"]["count_15m"] == 2


def test_baseline_is_unknown_until_three_closed_hours():
    activity = TemplateActivity()
    _feed(activity, "api", "T1", 0.0, 2 * H, 2)
    entry = activity.counts(2 * H + 60, "api")["T1"]
    assert entry["baseline_per_min"] is None and entry["ratio"] is None


def test_ratio_against_own_normal_rate_and_silent_hours_count_as_zero():
    activity = TemplateActivity()
    _feed(activity, "api", "CHRONIC", 0.0, 6 * H, 2)       # 2/min every hour: normal
    _feed(activity, "api", "BURST", 0.0, 6 * H, 2)
    _feed(activity, "api", "BURST", 6 * H, 6 * H + 900, 40)  # 40/min in the last 15 min
    _feed(activity, "api", "RARE", 3 * H, 3 * H + 60, 5)     # one minute in 6 hours
    _feed(activity, "api", "RARE", 6 * H, 6 * H + 900, 1)
    counts = activity.counts(6 * H + 899, "api")
    assert counts["CHRONIC"]["baseline_per_min"] == 2.0 and counts["CHRONIC"]["ratio"] == 0.0
    assert counts["BURST"]["baseline_per_min"] == 2.0 and counts["BURST"]["ratio"] == 20.0
    # median over 6 hours with 5 silent ones is 0 -> floored, large finite ratio
    assert counts["RARE"]["baseline_per_min"] == 0.0 and counts["RARE"]["ratio"] == 10.0
    # silent now but seen today: listed with count 0 against its normal rate
    _feed(activity, "api", "GONE", 0.0, 5 * H, 3)
    gone = activity.counts(6 * H + 899, "api")["GONE"]
    assert gone["count_30m"] == 0 and gone["baseline_per_min"] == 3.0 and gone["ratio"] == 0.0


def test_save_and_load_keep_the_baseline(tmp_path):
    path = tmp_path / "template_activity.json"
    activity = TemplateActivity()
    _feed(activity, "api", "T1", 0.0, 4 * H, 1)
    activity.save(path)
    restored = TemplateActivity.load(path)
    assert restored.counts(4 * H, "api") == activity.counts(4 * H, "api")
    assert restored.counts(4 * H, "api")["T1"]["baseline_per_min"] == 1.0
    path.write_text("{nope")
    assert TemplateActivity.load(path).counts(4 * H) == {}
    assert TemplateActivity.load(tmp_path / "missing.json").counts(4 * H) == {}

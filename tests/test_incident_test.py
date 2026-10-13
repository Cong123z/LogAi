import json

from scripts.incident_test import (
    alert_onsets, doc_tick, first_state_after, held_episodes, held_mask, parse_metrics, summarize,
)


def _s(ts, phase, scores, states):
    return {"ts": ts, "phase": phase, "scores": scores, "states": states, "alerts": {}}


def test_parse_metrics_reads_scores_current_state_and_lag():
    text = "\n".join([
        '# HELP log_anomaly_score x',
        'log_anomaly_score{documented="true",group_id="G0010",service="recharge"} 0.71',
        'log_alert_state{group_id="G0010",service="recharge",state="NORMAL"} 0.0',
        'log_alert_state{group_id="G0010",service="recharge",state="ALERTING"} 1.0',
        'log_alerts_total{group_id="G0010",service="recharge"} 3.0',
        'logai_last_processed_event_timestamp_seconds 1791853518.5',
    ])
    m = parse_metrics(text)
    assert m["scores"] == {"G0010": 0.71}
    assert m["states"] == {"G0010": "ALERTING"}
    assert m["alerts"] == {"G0010": 3}
    assert m["last_event"] == 1791853518.5


def test_alert_onsets_and_time_to_state():
    states = ["NORMAL", "WARMING", "ALERTING", "ALERTING", "COOLING", "ALERTING"]
    samples = [_s(100 + 5 * i, "b", {}, {"G": st}) for i, st in enumerate(states)]
    assert alert_onsets(samples, "G") == [110, 125]
    assert alert_onsets(samples[2:], "G", prev="ALERTING") == [125]  # carried-over alert not recounted
    assert first_state_after(samples, "G", {"WARMING", "ALERTING"}, 100) == 5
    assert first_state_after(samples, "G", {"ALERTING"}, 100) == 10
    assert first_state_after(samples, "X", {"ALERTING"}, 100) is None


def test_held_runs_need_six_samples():
    scores = [0.7] * 5 + [0.1] + [0.7] * 6 + [None] + [0.7] * 7
    mask = held_mask(scores, 0.68)
    assert mask[:5] == [False] * 5          # 5 samples: too short
    assert mask[6:12] == [True] * 6         # exactly 6
    assert mask[12] is False                # missing score breaks the run
    assert mask[13:] == [True] * 7          # run ending at the last sample
    assert held_episodes(scores, 0.68) == 2
    assert held_episodes(scores, 0.75) == 0


def test_doc_tick_rules():
    window = {"kind": "window", "status": "done", "documentation_id": "DOC-001"}
    assert doc_tick(window, {"G0010": "DOC-001"}, "G0010", "burst") == "✔"
    assert doc_tick({**window, "documentation_id": "DOC-002"}, {"G0010": "DOC-001"}, "G0010", "burst") == "✘"
    assert doc_tick(window, {}, "G0003", "control") == "–"
    service = {"kind": "service", "status": "done", "health": "degraded", "issue_groups": ["G0010"]}
    assert doc_tick(service, {"G0010": "DOC-001"}, None, "burst") == "✔"
    assert doc_tick(service, {"G0010": "DOC-001", "G1": "D"}, None, "burst") == "✘"
    assert doc_tick(service, {}, None, "control") == "✘"
    assert doc_tick({**service, "status": "failed"}, {}, None, "control") == "–"


def test_summarize_counts_false_alerts_and_burst_detection(tmp_path):
    phases = [
        {"event": "phase", "name": "warmup", "kind": "warmup", "start": 0, "end": 60, "targets": {}},
        {"event": "phase", "name": "control", "kind": "control", "start": 60, "end": 120, "targets": {}},
        {"event": "phase", "name": "B1", "kind": "burst", "start": 120, "end": 180, "targets": {"G0010": "DOC-001"}},
        {"event": "phase", "name": "B1-recovery", "kind": "recovery", "start": 180, "end": 240,
         "targets": {"G0010": "DOC-001"}},
    ]
    samples = []
    for ts in range(0, 240, 5):
        phase = next(p["name"] for p in phases if p["start"] <= ts < p["end"])
        noisy = "ALERTING" if phase == "warmup" or 80 <= ts < 100 else "NORMAL"  # warm-up alert ignored
        burst = "ALERTING" if 130 <= ts < 180 or 200 <= ts < 210 else "NORMAL"
        samples.append(_s(ts, phase, {"G0010": 0.75 if phase == "B1" else 0.4, "G0003": 0.5},
                          {"G0010": burst, "G0003": noisy}))
    (tmp_path / "meta.json").write_text(json.dumps({"last_retrain": {}}))
    (tmp_path / "phases.jsonl").write_text("\n".join(json.dumps(p) for p in phases))
    (tmp_path / "samples.jsonl").write_text("\n".join(json.dumps(s) for s in samples))
    report = summarize(tmp_path)
    assert "| G0003 | 1 | 1 |" in report                       # one control false alert
    # 10 of 12 burst samples alerting; one re-alert in recovery
    assert "| B1 | G0010 | 10 s | 10 s | 83% | 0.750 | 0.750 | 0.750 | 1 |" in report
    assert "| 0.72 | 0 | 100% |" in report                      # burst held above 0.72, no noise

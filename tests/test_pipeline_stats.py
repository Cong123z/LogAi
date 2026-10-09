"""Pipeline page: engine counters reach the heartbeat runtime payload."""
from __future__ import annotations

import json

from types import SimpleNamespace

import test_incident_classifier as tic
from logai.metrics.prometheus_exporter import pipeline_snapshot


def _sample(name, value, **labels):
    return SimpleNamespace(name=name, value=value, labels=labels)


class _Registry:
    """Stand-in for a prometheus registry: other test modules replace
    prometheus_client with a MagicMock process-wide, so no real one is used."""

    def __init__(self, samples):
        self.samples = samples

    def restricted_registry(self, names):
        family = SimpleNamespace(samples=[s for s in self.samples if s.name in names])
        return SimpleNamespace(collect=lambda: [family])


def test_snapshot_sums_labelled_series():
    registry = _Registry([
        _sample("logai_events_received_total", 7),
        _sample("logai_dlq_records_total", 2, reason_code="a"),
        _sample("logai_dlq_records_total", 3, reason_code="b"),
        _sample("logai_llm_retries_total", 1, kind="window", reason="timeout"),
        _sample("logai_llm_retries_total", 1, kind="service", reason="timeout"),
        _sample("logai_llm_retries_total", 1, kind="service", reason="json"),
        _sample("logai_processing_latency_seconds_sum", 2.0),
        _sample("logai_processing_latency_seconds_count", 2),
        _sample("app_log_events_total", 99, service="x"),
    ])

    snap = pipeline_snapshot(registry)

    assert snap["events_received"] == 7
    assert snap["events_processed"] == 0
    assert snap["dlq_records"] == 5
    assert snap["llm_retries"] == {"timeout": 2, "json": 1}
    assert snap["latency_sum"] == 2.0 and snap["latency_count"] == 2


def test_heartbeat_carries_pipeline_stats(tmp_path):
    p = tic._pipeline(tmp_path)
    p._pending_predictions = [object(), object()]
    p._last_grouping_heartbeat = 0.0
    p._heartbeat_grouping()

    pipeline = p.grouping_store.load_status()["runtime"]["pipeline"]

    assert pipeline["started_at"] == p._started_at
    assert pipeline["pending_predictions"] == 2
    assert "events_received" in pipeline and "es_retries" in pipeline
    json.dumps(pipeline)

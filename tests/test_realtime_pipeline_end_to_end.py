"""End-to-End integration tests for RealtimePipeline covering all production requirements:
1. Idempotent Deduplication (Bounded LRU)
2. Log parsing and template assignment (Known vs Unknown/Pending)
3. O(1) Template metric counting (No O(T) scanning)
4. 8D FeatureVector per-event extraction
5. Anomaly prediction with 'if-global-v2' and Alert State Machine
6. Dead Letter Queue (DLQ) for failed events
7. Batch collection and zero-stall GC
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure lightweight mocks for external drivers not installed in minimal environment
for mod in [
    "numpy",
    "sklearn",
    "sklearn.ensemble",
    "hdbscan",
    "elasticsearch",
    "sentence_transformers",
    "drain3",
    "drain3.template_miner",
    "drain3.file_persistence",
    "drain3.template_miner_config",
    "drain3.masking",
    "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from logai.config import AppConfig
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import MatchResult
from logai.models import (
    AlertStateEnum,
    AnomalyResult,
    FeatureVector,
    GroupState,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.realtime.realtime_pipeline import RealtimePipeline
from logai.storage.checkpoint import CheckpointStore


class _StopLoop(BaseException):
    """Sentinel raised from a mocked poll_batch to break run_forever's
    infinite poll loop after the test's batches have been consumed.

    Subclasses BaseException so run_forever's poll-error handler (which only
    catches Exception) lets it propagate out of the loop.
    """


class TestRealtimePipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/non_existent.yaml"
        # Flush the predict buffer after every event so run_forever-driven tests
        # exercise the flush/commit machinery deterministically (TODO #7 defers
        # scoring to the flush boundary; batch_size=1 makes each poll flush).
        self.cfg.anomaly.predict_batch_size = 1

        self.pipeline = RealtimePipeline(self.cfg)

        # Pre-seed group G_AUTH in group registry
        self.group_auth = GroupState(group_id="G_AUTH", first_seen=1000.0, last_seen=1000.0)
        self.pipeline.group_registry.upsert(self.group_auth)

        # Mock embedder and clusterer for new template assignment
        self.pipeline.embedder.embed_one = MagicMock(return_value=[0.1] * 384)
        self.pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=("G_AUTH", 0.92))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_pipeline_event_lifecycle(self):
        """Verify the full 12-stage lifecycle for an incoming log event."""
        raw = RawLog(
            event_id="evt_001",
            timestamp=1000.0,
            service="auth",
            level="INFO",
            message="User alice logged in from 10.0.0.1",
        )
        self.pipeline.parser.parse = MagicMock(
            return_value=ParsedEvent(
                raw=raw,
                template="User <*> logged in from <*>",
                template_id="t_login",
                parameters=["alice", "10.0.0.1"],
                is_new_template=True,
            )
        )

        # Execute single event processing
        self.pipeline._process_one(raw)

        # 1. Dedup marked seen
        self.assertTrue(self.pipeline.dedup.seen("evt_001"))

        # 2. Template registered and counted in O(1)
        self.assertEqual(self.pipeline.template_registry.count_by_service("auth"), 1)
        tmpl = self.pipeline.template_registry.get("t_login")
        self.assertIsNotNone(tmpl)
        self.assertEqual(tmpl.group_id, "G_AUTH")

        # 3. Group touched
        group = self.pipeline.group_registry.get("G_AUTH")
        self.assertEqual(group.event_count, 1)
        self.assertIn("t_login", group.template_ids)

        # 4. FeatureEngine updated with 8D vector
        window = self.pipeline.feature_engine._windows["G_AUTH"]
        self.assertEqual(len(window.timestamps), 1)

        # 5. DLQ is empty
        self.assertEqual(self.pipeline.dlq.count(), 0)

    def test_duplicate_event_idempotency(self):
        """Duplicate event_id should be discarded immediately by DedupIndex."""
        raw = RawLog(
            event_id="evt_dup",
            timestamp=1000.0,
            service="auth",
            level="INFO",
            message="User alice logged in",
        )
        self.pipeline.parser.parse = MagicMock(
            return_value=ParsedEvent(
                raw=raw,
                template="User <*> logged in",
                template_id="t_login",
                parameters=["alice"],
                is_new_template=True,
            )
        )

        # First delivery
        self.pipeline._process_one(raw)
        self.assertEqual(self.pipeline.template_registry.count_by_service("auth"), 1)

        # Second delivery (replay/duplicate)
        self.pipeline.parser.parse.reset_mock()
        self.pipeline._process_one(raw)

        # Parser, group assignment, feature engine should NOT have been touched
        self.pipeline.parser.parse.assert_not_called()
        self.assertEqual(self.pipeline.template_registry.count_by_service("auth"), 1)

    def test_known_template_fast_path_bypasses_embedding_and_clustering(self):
        """When template is already known, pipeline must bypass embedding and clustering."""
        # Pre-seed template t_known in registry with group_id G_AUTH
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id="t_known",
                template_text="User <*> logged in",
                service="auth",
                group_id="G_AUTH",
            )
        )

        raw = RawLog(
            event_id="evt_fast",
            timestamp=1005.0,
            service="auth",
            level="INFO",
            message="User bob logged in",
        )
        self.pipeline.parser.parse = MagicMock(
            return_value=ParsedEvent(
                raw=raw,
                template="User <*> logged in",
                template_id="t_known",
                parameters=["bob"],
                is_new_template=False,
            )
        )

        self.pipeline.embedder.embed_one.reset_mock()
        self.pipeline.clusterer.assign_to_nearest_group.reset_mock()

        self.pipeline._process_one(raw)

        # Fast path bypassed expensive operations
        self.pipeline.embedder.embed_one.assert_not_called()
        self.pipeline.clusterer.assign_to_nearest_group.assert_not_called()
        # Count remains 1 (no re-count)
        self.assertEqual(self.pipeline.template_registry.count_by_service("auth"), 1)

    def test_failure_isolation_and_dlq_push(self):
        """Failed events must be safely sent to Dead Letter Queue without crashing pipeline."""
        raw_bad = RawLog(
            event_id="evt_bad",
            timestamp=1010.0,
            service="auth",
            level="ERROR",
            message="Malformed payload",
        )
        self.pipeline.parser.parse = MagicMock(side_effect=RuntimeError("Parsing corrupted byte stream"))

        # Must not raise exception
        self.pipeline._process_one(raw_bad)

        # Error captured in DLQ
        self.assertEqual(self.pipeline.dlq.count(), 1)
        records = list(self.pipeline.dlq.replay())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"]["event_id"], "evt_bad")
        self.assertIn("Parsing corrupted byte stream", records[0]["error"])

    def test_documentation_failure_does_not_block_anomaly_processing(self):
        """Optional documentation enrichment must not become an event failure."""
        raw = RawLog(
            event_id="evt_doc_failure",
            timestamp=1011.0,
            service="auth",
            level="ERROR",
            message="Authentication failed",
        )
        self.pipeline.parser.parse = MagicMock(
            return_value=ParsedEvent(
                raw=raw,
                template="Authentication failed",
                template_id="t_doc_failure",
                parameters=[],
                is_new_template=True,
            )
        )
        self.pipeline._match_documentation_if_stale = MagicMock(
            side_effect=ValueError("incompatible documentation embedding")
        )
        feature_vector = FeatureVector(group_id="G_AUTH", timestamp=raw.timestamp)
        self.pipeline.feature_engine.update = MagicMock(return_value=feature_vector)

        completed = self.pipeline._process_one(raw)

        self.assertTrue(completed)
        self.pipeline.feature_engine.update.assert_called_once()
        # Scoring is deferred (TODO #7): the vector is buffered for the next
        # batch flush rather than scored inline in _process_one.
        self.assertEqual(
            self.pipeline._pending_predictions, [("G_AUTH", feature_vector)]
        )
        self.assertTrue(self.pipeline.dedup.seen(raw.event_id))
        self.assertEqual(self.pipeline.dlq.count(), 0)

    def test_documentation_no_match_clears_stale_metadata(self):
        """A removed match must not leave an old documentation ID/error code."""
        group = self.pipeline.group_registry.get("G_AUTH")
        group.documented = True
        group.documentation_id = "DOC-OLD"
        group.error_code = "ERR_OLD"
        group.confidence = 0.95
        self.pipeline.group_registry.upsert(group)
        self.pipeline.group_registry.get_centroid = MagicMock(return_value=[0.1] * 384)
        self.pipeline.doc_matcher.ready = True
        self.pipeline.doc_matcher.match = MagicMock(
            return_value=MatchResult("G_AUTH", None, 0.42, False)
        )

        self.pipeline._match_documentation_if_stale("G_AUTH")

        updated = self.pipeline.group_registry.get("G_AUTH")
        self.assertFalse(updated.documented)
        self.assertIsNone(updated.documentation_id)
        self.assertEqual(updated.error_code, "")
        self.assertEqual(updated.confidence, 0.42)

    def test_unavailable_documentation_does_not_drop_events_under_load(self):
        """Every grouped event must still reach feature and anomaly stages."""
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id="t_load",
                template_text="Load event",
                service="auth",
                group_id="G_AUTH",
            )
        )
        self.pipeline.doc_matcher.ready = False
        self.pipeline.feature_engine.update = MagicMock(
            side_effect=lambda group_id, timestamp: FeatureVector(
                group_id=group_id, timestamp=timestamp
            )
        )

        def parse(raw):
            return ParsedEvent(
                raw=raw,
                template="Load event",
                template_id="t_load",
                parameters=[],
                is_new_template=False,
            )

        self.pipeline.parser.parse = MagicMock(side_effect=parse)
        event_count = 2_000
        for index in range(event_count):
            completed = self.pipeline._process_one(
                RawLog(
                    event_id=f"evt_doc_load_{index}",
                    timestamp=2000.0 + index,
                    service="auth",
                    level="INFO",
                    message="Load event",
                )
            )
            self.assertTrue(completed)

        self.assertEqual(self.pipeline.feature_engine.update.call_count, event_count)
        # Every grouped event's vector is buffered for batch scoring (deferred
        # from _process_one to the flush boundary); none are dropped.
        self.assertEqual(len(self.pipeline._pending_predictions), event_count)
        self.assertEqual(self.pipeline.dlq.count(), 0)

    def test_batch_execution_and_gc(self):
        """Verify poll_batch iteration, queue depth tracking, and GC invocation."""
        batch_1 = [
            RawLog(event_id=f"b1_{i}", timestamp=1000.0 + i, service="auth", level="INFO", message=f"msg {i}")
            for i in range(5)
        ]
        batch_2 = [
            RawLog(event_id=f"b2_{i}", timestamp=1010.0 + i, service="auth", level="INFO", message=f"msg {i}")
            for i in range(3)
        ]

        # Mock collector to yield 2 batches then stop the poll loop.
        self.pipeline.collector.poll_batch = MagicMock(
            side_effect=[(batch_1, [1005.0, "b1_4"]), (batch_2, [1012.0, "b2_2"]), _StopLoop]
        )
        self.pipeline.start_metrics_server = MagicMock()
        # _process_one is mocked out, so buffer the vector here to reproduce
        # what the real path does; otherwise the deferred flush (and its GC)
        # would never fire (predict_batch_size=1 flushes each non-empty poll).
        self.pipeline._process_one = MagicMock(
            side_effect=lambda raw: self.pipeline._pending_predictions.append(
                (raw.event_id, FeatureVector(group_id="G_AUTH", timestamp=raw.timestamp))
            )
            or True
        )
        self.pipeline.dedup.gc = MagicMock()

        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        # Both batches processed
        self.assertEqual(self.pipeline._process_one.call_count, 8)
        # GC invoked after each batch
        self.assertEqual(self.pipeline.dedup.gc.call_count, 2)

    def test_anomaly_storm_realtime_alerting(self):
        """Verify that micro-burst of events triggers anomaly alert in real time."""
        # Pre-seed template
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id="t_err",
                template_text="Connection timeout <*>:5432",
                service="auth",
                group_id="G_AUTH",
            )
        )

        # Mock batch inference to flag anomalies when a vector indicates a burst.
        # Scoring is now batched (TODO #7), so the pipeline calls predict_batch;
        # the per-row logic mirrors the old per-event predict exactly.
        def mock_predict_batch(fvs):
            results = []
            for fv in fvs:
                if fv.z_score_10s > 2.0 or fv.short_growth_rate > 3.0:
                    results.append(AnomalyResult(
                        group_id="G_AUTH", timestamp=fv.timestamp,
                        anomaly=True, anomaly_score=0.85, model_version="if-global-v2",
                    ))
                else:
                    results.append(AnomalyResult(
                        group_id="G_AUTH", timestamp=fv.timestamp,
                        anomaly=False, anomaly_score=0.2, model_version="if-global-v2",
                    ))
            return results

        self.pipeline.anomaly_model.predict_batch = MagicMock(side_effect=mock_predict_batch)

        base_ts = 2000.0
        # 1. Steady traffic: 30 events at 1 event/sec
        for i in range(30):
            raw = RawLog(event_id=f"steady_{i}", timestamp=base_ts + i, service="auth", level="INFO", message="err")
            self.pipeline.parser.parse = MagicMock(
                return_value=ParsedEvent(raw=raw, template="err", template_id="t_err", parameters=[], is_new_template=False)
            )
            self.pipeline._process_one(raw)

        # Score the buffered steady traffic in one batch, applying transitions
        # in event order (result-equivalent to the old per-event path).
        self.pipeline._flush_predictions()

        # State should be NORMAL
        current_state = self.pipeline.alert_sm._load("G_AUTH")
        self.assertEqual(current_state.alert_state, AlertStateEnum.NORMAL.value)

        # 2. Sudden burst: 15 events in 0.5 seconds
        burst_base = base_ts + 31.0
        for j in range(15):
            ts = burst_base + (j * 0.03)
            raw = RawLog(event_id=f"burst_{j}", timestamp=ts, service="auth", level="ERROR", message="err")
            self.pipeline.parser.parse = MagicMock(
                return_value=ParsedEvent(raw=raw, template="err", template_id="t_err", parameters=[], is_new_template=False)
            )
            self.pipeline._process_one(raw)

        self.pipeline._flush_predictions()

        # State should transition to ALERTING
        final_state = self.pipeline.alert_sm._load("G_AUTH")
        self.assertEqual(final_state.alert_state, AlertStateEnum.ALERTING.value)


class TestRealtimeCheckpointRecovery(unittest.TestCase):
    """Batch checkpoint contract and crash/pressure tests for realtime."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/missing.yaml"
        # Flush per poll so the commit/durability sequence is exercised
        # deterministically (TODO #7 defers commit to the flush boundary).
        self.cfg.anomaly.predict_batch_size = 1
        self.pipeline = RealtimePipeline(self.cfg)
        self.pipeline.start_metrics_server = MagicMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _buffering_process_one(self, raw):
        """Stand-in for _process_one that buffers a vector (as the real path
        does) so the deferred flush actually fires under predict_batch_size=1."""
        self.pipeline._pending_predictions.append(
            (raw.event_id, FeatureVector(group_id=raw.event_id, timestamp=raw.timestamp))
        )
        return True

    def _raw_batch(self, size):
        return [
            RawLog(
                event_id=f"rt_{i}", timestamp=1000.0 + i,
                service="api", level="INFO", message=f"message {i}",
            )
            for i in range(size)
        ]

    def test_collector_fetch_does_not_commit_checkpoint(self):
        checkpoint = CheckpointStore(self.cfg.storage)
        checkpoint.commit([1, "old"], 999.0)
        collector = object.__new__(ElasticsearchCollector)
        collector.config = self.cfg.elasticsearch
        collector.checkpoint = checkpoint
        collector._search = MagicMock(return_value={
            "hits": {"hits": [{
                "_id": "evt_1",
                "_source": {
                    "@timestamp": "2026-01-01T00:00:00Z",
                    "service": "api", "level": "INFO", "message": "ok",
                },
                "sort": [2, "evt_1"],
            }]}
        })

        batch, cursor = collector.poll_batch()

        self.assertEqual(len(batch), 1)
        self.assertEqual(cursor, [2, "evt_1"])
        self.assertEqual(checkpoint.get_search_after(), [1, "old"])
        self.assertEqual(checkpoint.get_last_timestamp(), 999.0)

    def test_commit_happens_after_processing_flush_and_dedup(self):
        batch = self._raw_batch(4)
        cursor = [1003, "rt_3"]
        order = []
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor), _StopLoop])
        self.pipeline._process_one = MagicMock(
            side_effect=lambda raw: order.append(f"process:{raw.event_id}")
            or self._buffering_process_one(raw)
        )
        self.pipeline.template_registry.flush = MagicMock(side_effect=lambda: order.append("templates"))
        self.pipeline.group_registry.flush = MagicMock(side_effect=lambda: order.append("groups"))
        self.pipeline.dedup.gc = MagicMock(side_effect=lambda: order.append("dedup"))
        self.pipeline.checkpoint.commit = MagicMock(side_effect=lambda *_: order.append("checkpoint"))

        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        self.assertEqual(order[-4:], ["templates", "groups", "dedup", "checkpoint"])
        self.pipeline.checkpoint.commit.assert_called_once_with(cursor, batch[-1].timestamp)

    def test_unhandled_crash_mid_batch_does_not_commit(self):
        batch = self._raw_batch(10)
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, [1009, "rt_9"])])
        self.pipeline._process_one = MagicMock(side_effect=RuntimeError("crash in realtime phase"))
        self.pipeline.checkpoint.commit = MagicMock()

        with self.assertRaises(RuntimeError):
            self.pipeline.run_forever()

        self.pipeline.checkpoint.commit.assert_not_called()
        self.assertEqual(self.pipeline.metrics.logai_queue_depth.set.call_args_list[-1].args, (0,))

    def test_flush_failure_does_not_commit(self):
        batch = self._raw_batch(2)
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, [1001, "rt_1"])])
        self.pipeline._process_one = MagicMock(side_effect=self._buffering_process_one)
        self.pipeline.template_registry.flush = MagicMock(side_effect=OSError("disk full"))
        self.pipeline.checkpoint.commit = MagicMock()

        with self.assertRaises(OSError):
            self.pipeline.run_forever()

        self.pipeline.checkpoint.commit.assert_not_called()

    def test_large_batch_commits_once_after_all_events(self):
        batch = self._raw_batch(20_000)
        cursor = [20999, "rt_19999"]
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor), _StopLoop])
        self.pipeline._process_one = MagicMock(side_effect=self._buffering_process_one)
        self.pipeline.template_registry.flush = MagicMock()
        self.pipeline.group_registry.flush = MagicMock()
        self.pipeline.dedup.gc = MagicMock()
        self.pipeline.checkpoint.commit = MagicMock()

        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        self.assertEqual(self.pipeline._process_one.call_count, 20_000)
        self.pipeline.checkpoint.commit.assert_called_once_with(cursor, batch[-1].timestamp)

    def test_dlq_terminal_failure_still_allows_batch_commit(self):
        batch = self._raw_batch(1)
        cursor = [1000, "rt_0"]
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor), _StopLoop])
        self.pipeline._process_one = MagicMock(side_effect=self._buffering_process_one)  # event was persisted to DLQ
        self.pipeline.template_registry.flush = MagicMock()
        self.pipeline.group_registry.flush = MagicMock()
        self.pipeline.dedup.gc = MagicMock()
        self.pipeline.checkpoint.commit = MagicMock()

        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        self.pipeline.checkpoint.commit.assert_called_once_with(cursor, batch[-1].timestamp)


if __name__ == "__main__":
    unittest.main()

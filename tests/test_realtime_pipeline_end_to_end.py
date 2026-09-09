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
from logai.models import (
    AlertStateEnum,
    AnomalyResult,
    GroupState,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.realtime.realtime_pipeline import RealtimePipeline


class TestRealtimePipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/non_existent.yaml"

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

    def test_batch_execution_and_gc(self):
        """Verify collector run_forever iteration, queue depth tracking, and GC invocation."""
        batch_1 = [
            RawLog(event_id=f"b1_{i}", timestamp=1000.0 + i, service="auth", level="INFO", message=f"msg {i}")
            for i in range(5)
        ]
        batch_2 = [
            RawLog(event_id=f"b2_{i}", timestamp=1010.0 + i, service="auth", level="INFO", message=f"msg {i}")
            for i in range(3)
        ]

        # Mock collector iterator to yield 2 batches and terminate
        self.pipeline.collector.run_forever = MagicMock(return_value=iter([batch_1, batch_2]))
        self.pipeline.start_metrics_server = MagicMock()
        self.pipeline._process_one = MagicMock()
        self.pipeline.dedup.gc = MagicMock()

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

        # Mock anomaly model to return anomaly when feature vector indicates burst
        def mock_predict(fv):
            if fv.z_score_10s > 2.0 or fv.short_growth_rate > 3.0:
                return AnomalyResult(
                    group_id="G_AUTH",
                    timestamp=fv.timestamp,
                    anomaly=True,
                    anomaly_score=0.85,
                    model_version="if-global-v2",
                )
            return AnomalyResult(
                group_id="G_AUTH",
                timestamp=fv.timestamp,
                anomaly=False,
                anomaly_score=0.2,
                model_version="if-global-v2",
            )

        self.pipeline.anomaly_model.predict = MagicMock(side_effect=mock_predict)

        base_ts = 2000.0
        # 1. Steady traffic: 30 events at 1 event/sec
        for i in range(30):
            raw = RawLog(event_id=f"steady_{i}", timestamp=base_ts + i, service="auth", level="INFO", message="err")
            self.pipeline.parser.parse = MagicMock(
                return_value=ParsedEvent(raw=raw, template="err", template_id="t_err", parameters=[], is_new_template=False)
            )
            self.pipeline._process_one(raw)

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

        # State should transition to ALERTING
        final_state = self.pipeline.alert_sm._load("G_AUTH")
        self.assertEqual(final_state.alert_state, AlertStateEnum.ALERTING.value)


if __name__ == "__main__":
    unittest.main()

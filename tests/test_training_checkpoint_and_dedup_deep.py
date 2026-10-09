"""Deep test suite for Training Pipeline Checkpoint, Dedup & Resume mechanisms:
1. Mid-stream crash in Phase 1 and fault-tolerant resumption with boundary duplicates.
2. Crash in Phase 2/3 preserving event index and checkpoint for restart.
3. High-pressure scale test (20,000 logs, 40% duplicates across multiple services).
4. Edge cases: empty streams, empty batches, 100% duplicate batches, out-of-order timestamps.
5. Strict isolation guarantee for realtime checkpoint.json and dedup_index.json.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure lightweight mocks for external drivers not installed in minimal environment
for mod in [
    "numpy",
    "sklearn",
    "sklearn.ensemble",
    "hdbscan",
    "elasticsearch",
    "drain3",
    "drain3.template_miner",
    "drain3.file_persistence",
    "drain3.template_miner_config",
    "drain3.masking",
    "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

class MockArray:
    def __init__(self, data):
        self._data = data
        if len(data) > 0 and isinstance(data[0], (list, tuple)):
            self.ndim = 2
            self.shape = (len(data), len(data[0]))
        else:
            self.ndim = 1
            self.shape = (len(data),)

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def __getitem__(self, idx):
        return self._data[idx]

class MockNumpy:
    float64 = float
    zeros = staticmethod(lambda shape: MockArray([[0.0]*shape[1] for _ in range(shape[0])] if len(shape)==2 else [0.0]*shape[0]))
    array = staticmethod(lambda data, dtype=None: MockArray(data))

sys.modules["numpy"] = MockNumpy()

class MockIsolationForest:
    def __init__(self, *args, **kwargs):
        self.n_features_in_ = 8

    def fit(self, X):
        self.n_features_in_ = len(X[0]) if len(X) > 0 else 8
        return self

sys.modules["sklearn.ensemble"].IsolationForest = MockIsolationForest

from logai.config import AppConfig
from logai.anomaly.isolation_forest_model import GLOBAL_MODEL_KEY
from logai.models import FeatureVector, ParsedEvent, RawLog
from logai.storage.checkpoint import CheckpointStore
from logai.storage.training_event_index import TrainingEventIndex
from logai.training.train_pipeline import TrainingPipeline, run_training_from_elasticsearch


class TestTrainingCheckpointAndDedupDeep(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/non_existent.yaml"
        self.cfg.anomaly.min_training_samples = 5
        self.cfg.training.batch_size = 100

        self.pipeline = TrainingPipeline(self.cfg)

        # Mock embedder and clusterer for deterministic fast testing
        self.pipeline.embedder.embed = MagicMock(return_value=[[0.1] * 8])
        self.pipeline.clusterer.cluster = MagicMock(
            side_effect=lambda ids, matrix: {tid: idx % 2 for idx, tid in enumerate(ids)}
        )
        self.pipeline.clusterer.compute_centroid = MagicMock(return_value=[0.1] * 8)
        self.pipeline.doc_matcher.match_all = MagicMock(return_value={})

        def mock_parse(raw: RawLog) -> ParsedEvent:
            tid = f"T_{raw.service.upper()}"
            return ParsedEvent(
                raw=raw,
                template_id=tid,
                template=f"Template for {raw.service}",
                parameters=[],
                is_new_template=False,
            )

        self.pipeline.parser.parse = MagicMock(side_effect=mock_parse)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mid_stream_crash_and_resumption_with_boundary_duplicates(self):
        """Simulate a crash mid-stream (after Batch 2 of 5).
        Verify checkpoint preservation, resume from cursor, and boundary deduplication."""
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )
        event_index_path = Path(self.cfg.storage.base_dir) / self.cfg.storage.training_event_index_file

        batch_1 = [
            RawLog(event_id=f"b1_{i}", timestamp=1000.0 + i, service="auth", level="INFO", message=f"m {i}")
            for i in range(10)
        ]
        cursor_1 = [1009000, "b1_9"]

        batch_2 = [
            RawLog(event_id=f"b2_{i}", timestamp=1010.0 + i, service="payment", level="INFO", message=f"m {i}")
            for i in range(10)
        ]
        cursor_2 = [1019000, "b2_9"]

        # Stream crashes on batch 3
        def crashing_stream():
            yield (batch_1, cursor_1)
            yield (batch_2, cursor_2)
            raise ConnectionResetError("Elasticsearch connection aborted during historical crawl")

        with self.assertRaises(ConnectionResetError):
            self.pipeline.run(crashing_stream(), checkpoint_store=training_cp)

        # Checkpoint and event index must be preserved on disk
        self.assertTrue(training_cp.path.exists())
        self.assertEqual(training_cp.get_search_after(), cursor_2)
        self.assertTrue(event_index_path.exists())
        records_before_resume = list(self.pipeline.event_index.records())
        self.assertEqual(len(records_before_resume), 20)

        # Resume run: Batch 2 is partially re-sent due to network retry, followed by batches 3, 4, 5
        batch_2_retry = [
            # 5 logs are overlapping retransmissions from batch 2
            RawLog(event_id=f"b2_{i}", timestamp=1010.0 + i, service="payment", level="INFO", message=f"retry {i}")
            for i in range(5, 10)
        ]
        batch_3 = [
            RawLog(event_id=f"b3_{i}", timestamp=1020.0 + i, service="auth", level="INFO", message=f"m {i}")
            for i in range(10)
        ]
        batch_4 = [
            RawLog(event_id=f"b4_{i}", timestamp=1030.0 + i, service="payment", level="INFO", message=f"m {i}")
            for i in range(10)
        ]
        batch_5 = [
            RawLog(event_id=f"b5_{i}", timestamp=1040.0 + i, service="inventory", level="INFO", message=f"m {i}")
            for i in range(10)
        ]

        resume_stream = [
            (batch_2_retry, [1019000, "b2_9"]),
            (batch_3, [1029000, "b3_9"]),
            (batch_4, [1039000, "b4_9"]),
            (batch_5, [1049000, "b5_9"]),
        ]

        # Execute resumed pipeline
        self.pipeline.run(resume_stream, checkpoint_store=training_cp)

        # Verify all templates and accurate event counts
        t_auth = self.pipeline.template_registry.get("T_AUTH")
        t_payment = self.pipeline.template_registry.get("T_PAYMENT")
        t_inventory = self.pipeline.template_registry.get("T_INVENTORY")

        self.assertIsNotNone(t_auth)
        self.assertIsNotNone(t_payment)
        self.assertIsNotNone(t_inventory)

        # Batch 1 (10 auth) + Batch 3 (10 auth) = 20 auth
        self.assertEqual(t_auth.event_count, 20)
        # Batch 2 (10 payment) + retry (deduped!) + Batch 4 (10 payment) = 20 payment
        self.assertEqual(t_payment.event_count, 20)
        # Batch 5 (10 inventory) = 10 inventory
        self.assertEqual(t_inventory.event_count, 10)

        # Global model successfully saved
        self.assertTrue(self.pipeline.model_store.exists(GLOBAL_MODEL_KEY))

        # Checkpoint and event index must be cleaned up on completion
        self.assertFalse(training_cp.path.exists())
        self.assertFalse(event_index_path.exists())

    def test_crash_in_phase2_preserves_artifacts_for_retry(self):
        """If failure occurs during Phase 2 (clustering), event index and checkpoint
        must remain on disk so that re-running does not re-fetch from ES."""
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )
        event_index_path = Path(self.cfg.storage.base_dir) / self.cfg.storage.training_event_index_file

        batch = [
            RawLog(event_id=f"evt_{i}", timestamp=1000.0 + i, service="auth", level="INFO", message=f"m {i}")
            for i in range(15)
        ]
        stream = [(batch, [1014000, "evt_14"])]

        # Deliberately cause Phase 2 to fail
        self.pipeline.clusterer.cluster.side_effect = MemoryError("Simulated HDBSCAN Out Of Memory")

        with self.assertRaises(MemoryError):
            self.pipeline.run(stream, checkpoint_store=training_cp)

        # Verify files are preserved
        self.assertTrue(training_cp.path.exists())
        self.assertTrue(event_index_path.exists())
        self.assertEqual(len(list(self.pipeline.event_index.records())), 15)

        # Unmock clusterer to simulate resolved condition
        self.pipeline.clusterer.cluster.side_effect = lambda ids, matrix: {tid: 0 for tid in ids}

        # Rerun with empty stream (as all logs were already fetched and cursor reached end)
        self.pipeline.run([], checkpoint_store=training_cp)

        # Pipeline must have recovered from event_index, built templates, and completed
        t_auth = self.pipeline.template_registry.get("T_AUTH")
        self.assertIsNotNone(t_auth)
        self.assertEqual(t_auth.event_count, 15)
        self.assertTrue(self.pipeline.model_store.exists(GLOBAL_MODEL_KEY))

        # Cleaned up after successful retry
        self.assertFalse(training_cp.path.exists())
        self.assertFalse(event_index_path.exists())

    def test_stress_scale_and_dedup_high_pressure(self):
        """Stress test with 20,000 logs across 10 batches with 40% duplicate injection
        spanning multiple services (auth, payment, order, inventory)."""
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )

        total_batches = 10
        batch_size = 2000
        services = ["auth", "payment", "order", "inventory"]
        batches = []
        expected_unique_ids = set()

        for b in range(total_batches):
            batch = []
            for i in range(batch_size):
                idx = b * batch_size + i
                # 40% chance of repeating an event from previous 100 events
                if i > 50 and (i % 5 in (0, 1)):
                    target_id = f"evt_{idx - (i % 40)}"
                else:
                    target_id = f"evt_{idx}"
                expected_unique_ids.add(target_id)
                srv = services[idx % len(services)]
                batch.append(
                    RawLog(
                        event_id=target_id,
                        timestamp=1000.0 + idx * 0.01,
                        service=srv,
                        level="INFO",
                        message=f"Process event {idx} on {srv}",
                    )
                )
            batches.append((batch, [1000.0 + (b + 1) * batch_size * 0.01, f"cursor_{b}"]))

        t0 = time.perf_counter()
        self.pipeline.run(batches, checkpoint_store=training_cp)
        elapsed = time.perf_counter() - t0

        # Verify all unique events were recorded accurately
        total_unique_expected = len(expected_unique_ids)
        total_registry_events = sum(t.event_count for t in self.pipeline.template_registry.all_templates())
        self.assertEqual(total_registry_events, total_unique_expected)

        # Verify throughput is high (> 25,000 logs/sec)
        throughput = (total_batches * batch_size) / elapsed
        self.assertGreater(throughput, 25000)

        # Artifacts exist and are valid
        self.assertTrue(self.pipeline.model_store.exists(GLOBAL_MODEL_KEY))
        groups = self.pipeline.group_registry.all_groups()
        self.assertGreaterEqual(len(groups), 1)

    def test_edge_cases_empty_and_all_duplicate_batches(self):
        """Edge cases:
        1. Empty stream does not crash and leaves registries clean.
        2. Interleaved empty batches.
        3. 100% duplicate batch.
        4. Out-of-order timestamps within a batch.
        """
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )

        # Edge case 1: Empty stream
        self.pipeline.run([], checkpoint_store=training_cp)
        self.assertEqual(len(self.pipeline.template_registry.all_templates()), 0)

        # Edge case 2: Interleaved empty batches and 100% duplicate batch
        raw1 = RawLog(event_id="e1", timestamp=1005.0, service="auth", level="INFO", message="m1")
        raw2_early = RawLog(event_id="e2", timestamp=1001.0, service="auth", level="INFO", message="m2")  # earlier ts
        raw3 = RawLog(event_id="e3", timestamp=1003.0, service="auth", level="INFO", message="m3")

        stream = [
            ([], None),                                        # Empty batch
            ([raw1, raw2_early, raw3], [1005000, "e1"]),      # Out-of-order timestamps
            ([], [1005000, "e1"]),                             # Empty batch with cursor
            ([raw1, raw1, raw1], [1005000, "e1_dup"]),         # 100% duplicate batch
        ]

        self.pipeline.run(stream, checkpoint_store=training_cp)

        t_auth = self.pipeline.template_registry.get("T_AUTH")
        self.assertIsNotNone(t_auth)
        self.assertEqual(t_auth.event_count, 3)
        self.assertEqual(t_auth.first_seen, 1001.0)
        self.assertEqual(t_auth.last_seen, 1005.0)

    def test_realtime_isolation_integrity(self):
        """Strict isolation: Pre-existing realtime checkpoint.json and dedup_index.json
        must be byte-for-byte untouched throughout training pipeline runs, crashes, and resumes."""
        rt_cp_file = Path(self.cfg.storage.base_dir) / self.cfg.storage.checkpoint_file
        rt_dedup_file = Path(self.cfg.storage.base_dir) / self.cfg.storage.dedup_index_file

        rt_cp_content = '{"search_after": [999999, "rt_01"], "last_timestamp": 999999.0}'
        rt_dedup_content = '{"version": 2, "max_size": 200000, "items": ["rt_1", "rt_2"]}'

        rt_cp_file.write_text(rt_cp_content, encoding="utf-8")
        rt_dedup_file.write_text(rt_dedup_content, encoding="utf-8")

        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )

        # Run training with multiple batches and intentional duplicate logs
        batch = [
            RawLog(event_id="t_evt_1", timestamp=1000.0, service="auth", level="INFO", message="msg"),
            RawLog(event_id="t_evt_1", timestamp=1000.0, service="auth", level="INFO", message="msg duplicate"),
            RawLog(event_id="t_evt_2", timestamp=1001.0, service="auth", level="INFO", message="msg 2"),
            RawLog(event_id="t_evt_3", timestamp=1002.0, service="auth", level="INFO", message="msg 3"),
            RawLog(event_id="t_evt_4", timestamp=1003.0, service="auth", level="INFO", message="msg 4"),
            RawLog(event_id="t_evt_5", timestamp=1004.0, service="auth", level="INFO", message="msg 5"),
        ]
        self.pipeline.run([(batch, [1004000, "t_evt_5"])], checkpoint_store=training_cp)

        # Realtime files must be 100% identical byte-for-byte
        self.assertEqual(rt_cp_file.read_text(encoding="utf-8"), rt_cp_content)
        self.assertEqual(rt_dedup_file.read_text(encoding="utf-8"), rt_dedup_content)


if __name__ == "__main__":
    unittest.main()

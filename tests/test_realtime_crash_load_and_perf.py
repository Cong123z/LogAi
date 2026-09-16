"""Comprehensive test suite for Realtime Pipeline:
1. Crashes at different pipeline stages (Assign group, Feature Engine, Anomaly Model, DLQ, Registry Flush, Dedup GC)
   verifying checkpoint is never prematurely committed.
2. Mid-batch crash, restart recovery, and DedupIndex idempotency (zero data loss, zero double-counting).
3. High load capacity stress test (10,000 logs, batch streaming, throughput & latency measurement).
4. Fast replay throughput of duplicate logs via DedupIndex.
5. Realtime Anomaly detection accuracy & Alert State Machine under burst load.
"""
from __future__ import annotations

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

    def decision_function(self, X):
        # Negative score means anomaly (e.g. if short_growth_rate > 2.0 or z_10s > 2.0 or spike_ratio > 2.0)
        scores = []
        for row in X:
            z_10s = row[0]
            short_growth = row[2]
            spike_ratio = row[7]
            if z_10s > 2.0 or short_growth > 2.0 or spike_ratio > 2.0:
                scores.append(-0.35)  # anomaly -> anomaly_score = 0.5 - (-0.35) = 0.85
            else:
                scores.append(0.25)   # normal -> anomaly_score = 0.5 - 0.25 = 0.25
        return scores

    def predict(self, X):
        return [-1 if s < 0 else 1 for s in self.decision_function(X)]

sys.modules["sklearn.ensemble"].IsolationForest = MockIsolationForest

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
from logai.storage.checkpoint import CheckpointStore


class _StopLoop(BaseException):
    """Sentinel raised from a mocked poll_batch to break run_forever's
    infinite poll loop after the test's batches have been consumed.

    Subclasses BaseException (not Exception) so run_forever's poll-error
    handler — which only catches Exception — lets it propagate out.
    """


class TestRealtimeCrashLoadAndPerformance(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/missing.yaml"
        self.cfg.training.batch_size = 500
        self.cfg.reliability.dedup_max_size = 50_000
        # Scoring is deferred to the flush boundary (TODO #7). Flushing once per
        # poll (after the whole batch is buffered) reproduces the old per-batch
        # commit/durability semantics these crash & load tests assert on.
        self.cfg.anomaly.predict_batch_size = 1

        self.pipeline = RealtimePipeline(self.cfg)
        self.pipeline.start_metrics_server = MagicMock()
        self.pipeline.anomaly_model._model = MockIsolationForest()

        # Seed pre-existing group
        self.group_auth = GroupState(group_id="G_AUTH", service="auth", first_seen=1000.0, last_seen=1000.0)
        self.pipeline.group_registry.upsert(self.group_auth)
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id="T_AUTH",
                template_text="User <*> logged in",
                service="auth",
                group_id="G_AUTH",
                first_seen=1000.0,
                last_seen=1000.0,
            )
        )

        def mock_parse(raw: RawLog) -> ParsedEvent:
            return ParsedEvent(
                raw=raw,
                template_id="T_AUTH",
                template="User <*> logged in",
                parameters=["123"],
                is_new_template=False,
            )

        self.pipeline.parser.parse = MagicMock(side_effect=mock_parse)
        self.pipeline.embedder.embed_one = MagicMock(return_value=[0.1] * 8)
        self.pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=("G_AUTH", 0.95))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _generate_logs(self, count: int, start_idx: int = 0, base_ts: float = 1000.0) -> list[RawLog]:
        return [
            RawLog(
                event_id=f"evt_{start_idx + i}",
                timestamp=base_ts + (i * 0.05),
                service="auth",
                level="INFO",
                message=f"User {start_idx + i} logged in",
            )
            for i in range(count)
        ]

    def test_crashes_at_different_stages_prevent_checkpoint_commit(self):
        """Verify that an unhandled crash at ANY processing stage
        (DLQ write failure, incomplete terminal state, SIGINT, Template Flush, Group Flush, Dedup GC)
        strictly prevents checkpoint commit."""
        checkpoint = self.pipeline.checkpoint
        self.assertIsNone(checkpoint.get_search_after())

        batch = self._generate_logs(10)
        cursor = [1000.5, "evt_9"]

        # -------------------------------------------------------------
        # Stage 1: Crash during DLQ push (e.g. DLQ disk full when log fails)
        # -------------------------------------------------------------
        with patch.object(self.pipeline.parser, "parse", side_effect=ValueError("Corrupt record")):
            with patch.object(self.pipeline.dlq, "push", side_effect=IOError("DLQ disk full")):
                self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor)])
                with self.assertRaises(IOError):
                    self.pipeline.run_forever()
                self.assertIsNone(checkpoint.get_search_after())

        # -------------------------------------------------------------
        # Stage 2: Fatal error in _process_one returning False (non-terminal outcome)
        # -------------------------------------------------------------
        with patch.object(self.pipeline, "_process_one", return_value=False):
            self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor)])
            with self.assertRaises(RuntimeError) as ctx:
                self.pipeline.run_forever()
            self.assertIn("did not reach a terminal state", str(ctx.exception))
            self.assertIsNone(checkpoint.get_search_after())

        # -------------------------------------------------------------
        # Stage 3: Fatal interruption (KeyboardInterrupt/SIGINT) during feature update
        # -------------------------------------------------------------
        with patch.object(self.pipeline.feature_engine, "update", side_effect=KeyboardInterrupt("SIGINT")):
            self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor)])
            with self.assertRaises(KeyboardInterrupt):
                self.pipeline.run_forever()
            self.assertIsNone(checkpoint.get_search_after())

        # -------------------------------------------------------------
        # Stage 4: Fatal error during TemplateRegistry.flush()
        # Scoring is now deferred to the flush boundary, and flush only fires
        # when the buffer is non-empty. Each flush-stage therefore needs a
        # batch of NEW event_ids: reusing the same ids would let a prior stage's
        # dedup marks skip every event, leaving an empty buffer that never
        # flushes (so the mocked-to-raise flush would never fire).
        # -------------------------------------------------------------
        batch_4 = self._generate_logs(10, start_idx=100)
        cursor_4 = [batch_4[-1].timestamp, batch_4[-1].event_id]
        with patch.object(self.pipeline.template_registry, "flush", side_effect=IOError("Disk write failed on flush")):
            self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch_4, cursor_4)])
            with self.assertRaises(IOError):
                self.pipeline.run_forever()
            self.assertIsNone(checkpoint.get_search_after())

        # -------------------------------------------------------------
        # Stage 5: Fatal error during GroupRegistry.flush()
        # -------------------------------------------------------------
        batch_5 = self._generate_logs(10, start_idx=200)
        cursor_5 = [batch_5[-1].timestamp, batch_5[-1].event_id]
        with patch.object(self.pipeline.group_registry, "flush", side_effect=IOError("Group registry flush error")):
            self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch_5, cursor_5)])
            with self.assertRaises(IOError):
                self.pipeline.run_forever()
            self.assertIsNone(checkpoint.get_search_after())

        # -------------------------------------------------------------
        # Stage 6: Fatal error during DedupIndex.gc()
        # -------------------------------------------------------------
        batch_6 = self._generate_logs(10, start_idx=300)
        cursor_6 = [batch_6[-1].timestamp, batch_6[-1].event_id]
        with patch.object(self.pipeline.dedup, "gc", side_effect=IOError("Dedup GC error")):
            self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch_6, cursor_6)])
            with self.assertRaises(IOError):
                self.pipeline.run_forever()
            self.assertIsNone(checkpoint.get_search_after())

    def test_mid_batch_crash_resumption_and_dedup_idempotency(self):
        """Realistic mid-batch failure scenario:
        1. Batch 1 has 100 events.
        2. At event 60, a sudden crash happens. Checkpoint is NOT committed.
        3. Restart pipeline: ES re-emits Batch 1 from checkpoint (0).
        4. Events 0..59 are detected by DedupIndex (idempotent skip).
        5. Events 60..99 are processed successfully.
        6. Checkpoint commits after full batch completion.
        7. Batch 2 arrives and processes seamlessly."""
        checkpoint = self.pipeline.checkpoint
        self.assertIsNone(checkpoint.get_search_after())

        batch_1 = self._generate_logs(100, start_idx=0, base_ts=1000.0)
        cursor_1 = [1005.0, "evt_99"]

        processed_ids = []
        original_process = self.pipeline._process_one

        def flaky_process(raw: RawLog):
            if raw.event_id == "evt_60":
                raise SystemError("Simulated SIGTERM / Server power crash mid-batch")
            processed_ids.append(raw.event_id)
            return original_process(raw)

        # Run 1: Crash at event 60
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch_1, cursor_1)])
        with patch.object(self.pipeline, "_process_one", side_effect=flaky_process):
            with self.assertRaises(SystemError):
                self.pipeline.run_forever()

        # Checkpoint MUST NOT have committed!
        self.assertIsNone(checkpoint.get_search_after())
        # Dedup index has marked events 0..59
        self.assertEqual(len(processed_ids), 60)
        for i in range(60):
            self.assertTrue(self.pipeline.dedup.seen(f"evt_{i}"))
        self.assertFalse(self.pipeline.dedup.seen("evt_60"))

        # Run 2 (Restart): ES re-emits Batch 1, followed by Batch 2
        batch_2 = self._generate_logs(50, start_idx=100, base_ts=1010.0)
        cursor_2 = [1012.5, "evt_149"]

        second_run_processed = []
        def normal_process(raw: RawLog):
            if not self.pipeline.dedup.seen(raw.event_id):
                second_run_processed.append(raw.event_id)
            return original_process(raw)

        self.pipeline.collector.poll_batch = MagicMock(
            side_effect=[(batch_1, cursor_1), (batch_2, cursor_2), _StopLoop]
        )
        with patch.object(self.pipeline, "_process_one", side_effect=normal_process):
            with self.assertRaises(_StopLoop):
                self.pipeline.run_forever()

        # In second run: events 0..59 were skipped by dedup! Events 60..99 + 100..149 processed.
        self.assertEqual(len(second_run_processed), 40 + 50)
        self.assertIn("evt_60", second_run_processed)
        self.assertIn("evt_99", second_run_processed)
        self.assertIn("evt_149", second_run_processed)

        # Checkpoint is now successfully committed to cursor_2
        self.assertEqual(checkpoint.get_search_after(), cursor_2)
        self.assertEqual(checkpoint.get_last_timestamp(), batch_2[-1].timestamp)

    def test_high_load_throughput_and_stress(self):
        """Feed 10,000 logs across 5 large batches (2,000 logs/batch).
        Measure throughput, verify queue depth metric, and confirm checkpoint progression."""
        checkpoint = self.pipeline.checkpoint
        total_logs = 10_000
        batch_size = 2_000
        num_batches = total_logs // batch_size

        stream = []
        for b in range(num_batches):
            b_logs = self._generate_logs(batch_size, start_idx=b * batch_size, base_ts=1000.0 + b * 100)
            cursor = [b_logs[-1].timestamp, b_logs[-1].event_id]
            stream.append((b_logs, cursor))

        self.pipeline.collector.poll_batch = MagicMock(side_effect=list(stream) + [_StopLoop])

        t0 = time.perf_counter()
        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()
        elapsed = time.perf_counter() - t0

        throughput = total_logs / elapsed
        latency_us = (elapsed / total_logs) * 1e6

        # Assertions
        self.assertEqual(checkpoint.get_search_after(), stream[-1][1])
        self.assertEqual(checkpoint.get_last_timestamp(), stream[-1][0][-1].timestamp)

        # Confirm queue depth returned to 0
        self.pipeline.metrics.logai_queue_depth.set.assert_called_with(0)

        # Confirm high throughput: should comfortably exceed 1,000 logs/sec in single-thread test
        self.assertGreater(throughput, 1_000)
        print(f"\n[REALTIME PERFORMANCE] 10,000 logs processed in {elapsed:.3f}s | Throughput: {throughput:,.0f} logs/sec | Latency: {latency_us:.2f} µs/log")

    def test_duplicate_replay_throughput_after_crash(self):
        """Verify that when ES re-emits thousands of logs after a crash,
        DedupIndex skips them with minimal latency (>10,000 logs/sec)."""
        batch = self._generate_logs(5_000, start_idx=0, base_ts=3000.0)
        cursor = [3000.0 + 5000 * 0.05, "evt_4999"]

        # First run: process and populate dedup
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor), _StopLoop])
        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        # Second run: replay exact same 5,000 logs
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, cursor), _StopLoop])
        t0 = time.perf_counter()
        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()
        elapsed = time.perf_counter() - t0
        replay_throughput = len(batch) / elapsed
        self.assertGreater(replay_throughput, 10_000)
        print(f"\n[DEDUP REPLAY THROUGHPUT] 5,000 duplicate logs skipped in {elapsed:.4f}s | Throughput: {replay_throughput:,.0f} logs/sec")

    def test_anomaly_detection_under_load_with_alert_state_machine(self):
        """Simulate continuous traffic with a sudden anomaly storm under realistic load.
        Verify that AlertStateMachine transitions accurately without lagging."""
        # 1. Normal traffic (60 events at 1 event/sec: base_ts + i * 1.0)
        normal_logs = [
            RawLog(
                event_id=f"norm_{i}",
                timestamp=2000.0 + i * 1.0,
                service="auth",
                level="INFO",
                message=f"User {i} logged in",
            )
            for i in range(60)
        ]
        self.pipeline.collector.poll_batch = MagicMock(
            side_effect=[(normal_logs, [2059.0, "norm_59"]), _StopLoop]
        )
        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        # Window/alert identity is (service, group_id); these events are service
        # "auth" grouped into G_AUTH, so the cell is ("auth", "G_AUTH").
        state = self.pipeline.alert_sm._load(("auth", "G_AUTH"))
        # Steady state should be NORMAL
        self.assertEqual(state.alert_state, AlertStateEnum.NORMAL.value)

        # 2. Sudden burst: 20 events in 0.2 seconds (100 events/sec)
        burst_base = 2060.0
        burst_logs = [
            RawLog(
                event_id=f"burst_{i}",
                timestamp=burst_base + (i * 0.01),
                service="auth",
                level="ERROR",
                message="Critical connection failure to auth DB",
            )
            for i in range(20)
        ]
        self.pipeline.collector.poll_batch = MagicMock(
            side_effect=[(burst_logs, [burst_base + 0.2, "burst_19"]), _StopLoop]
        )
        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        # State machine should have transitioned to ALERTING
        state = self.pipeline.alert_sm._load(("auth", "G_AUTH"))
        self.assertEqual(state.alert_state, AlertStateEnum.ALERTING.value)
        self.assertTrue(state.anomaly)


if __name__ == "__main__":
    unittest.main()

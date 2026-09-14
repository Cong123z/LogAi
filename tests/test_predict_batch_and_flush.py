"""TODO #7 - batch inference at the predict phase.

Covers the plan's test-plan items:
  1. Equivalence  - predict_batch([fv_i]) matches predict(fv_i) element-wise,
     including the "model not trained -> all None" case.
  2. Flush by count - a buffer that reaches predict_batch_size scores in exactly
     one predict_batch call and empties.
  3. Flush by time  - a buffer that stays below predict_batch_size still flushes
     (and commits) once predict_max_wait_seconds elapses, driven off a fake
     monotonic clock advanced by the loop's own sleep.

Follows the suite convention of stubbing heavy optional drivers and swapping a
numpy-free MockNumpy / MockIsolationForest so the real GlobalAnomalyModel and
RealtimePipeline code paths run without the native libraries installed.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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
    zeros = staticmethod(
        lambda shape: MockArray(
            [[0.0] * shape[1] for _ in range(shape[0])] if len(shape) == 2 else [0.0] * shape[0]
        )
    )
    array = staticmethod(lambda data, dtype=None: MockArray(data))


sys.modules["numpy"] = MockNumpy()


class MockIsolationForest:
    """decision_function keyed on z_score_10s (row[0]): a higher burst signal
    yields a more negative (more anomalous) raw score."""

    def __init__(self, *args, **kwargs):
        self.n_features_in_ = 8

    def fit(self, X):
        self.n_features_in_ = len(X[0]) if len(X) > 0 else 8
        return self

    def decision_function(self, X):
        return [(-0.35 if row[0] > 2.0 else 0.25) for row in X]

    def predict(self, X):
        return [-1 if s < 0 else 1 for s in self.decision_function(X)]


sys.modules["sklearn.ensemble"].IsolationForest = MockIsolationForest

from logai.config import AppConfig
from logai.models import (
    AnomalyResult,
    FeatureVector,
    GroupState,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.realtime import realtime_pipeline
from logai.realtime.realtime_pipeline import RealtimePipeline


class TestPredictBatchEquivalence(unittest.TestCase):
    """Item 1: batching must be result-equivalent to per-event scoring."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/missing.yaml"
        self.pipeline = RealtimePipeline(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _vectors(self, n):
        # A spread of z_score_10s straddling the mock's 2.0 anomaly boundary.
        return [
            FeatureVector(
                group_id=f"G{i % 4}",
                timestamp=1000.0 + i,
                z_score_10s=(i % 5) * 1.0,  # 0..4 -> some normal, some anomalous
                short_growth_rate=1.0 + (i % 3) * 0.5,
            )
            for i in range(n)
        ]

    def test_untrained_model_returns_all_none(self):
        vectors = self._vectors(50)
        results = self.pipeline.anomaly_model.predict_batch(vectors)
        self.assertEqual(results, [None] * 50)
        # Empty input is well-defined too.
        self.assertEqual(self.pipeline.anomaly_model.predict_batch([]), [])

    def test_batch_matches_per_event(self):
        self.pipeline.anomaly_model._model = MockIsolationForest()
        vectors = self._vectors(60)

        batched = self.pipeline.anomaly_model.predict_batch(vectors)
        one_by_one = [self.pipeline.anomaly_model.predict(fv) for fv in vectors]

        self.assertEqual(len(batched), len(vectors))
        for b, s, fv in zip(batched, one_by_one, vectors):
            self.assertIsNotNone(b)
            self.assertEqual(b.group_id, fv.group_id)
            self.assertEqual(b.timestamp, fv.timestamp)
            self.assertEqual(b.anomaly_score, s.anomaly_score)
            self.assertEqual(b.anomaly, s.anomaly)
            # decision_function < 0 is exactly IsolationForest.predict()==-1.
            self.assertEqual(b.anomaly, fv.z_score_10s > 2.0)

    def test_bad_dimension_vectors_slot_back_none(self):
        self.pipeline.anomaly_model._model = MockIsolationForest()

        class ShortVec(FeatureVector):
            def as_vector(self):  # 3 dims instead of 8
                return [0.0, 0.0, 0.0]

        good = FeatureVector(group_id="G_ok", timestamp=1.0, z_score_10s=5.0)
        bad = ShortVec(group_id="G_bad", timestamp=2.0)
        results = self.pipeline.anomaly_model.predict_batch([good, bad, good])
        self.assertIsNotNone(results[0])
        self.assertIsNone(results[1])  # wrong dimension -> None, in place
        self.assertIsNotNone(results[2])

    def test_state_persist_failure_keeps_buffer_and_checkpoint(self):
        vectors = self._vectors(3)
        self.pipeline._pending_predictions = [
            (fv.group_id, fv) for fv in vectors
        ]
        self.pipeline._pending_cursor = [1003.0, "e3"]
        self.pipeline._pending_last_ts = 1003.0
        self.pipeline.anomaly_model.predict_batch = MagicMock(return_value=[
            AnomalyResult(
                group_id=fv.group_id,
                timestamp=fv.timestamp,
                anomaly_score=0.8,
                anomaly=True,
                count_1m=10,
            )
            for fv in vectors
        ])
        self.pipeline.alert_sm.transition_batch = MagicMock(
            side_effect=OSError("state write failed")
        )

        with self.assertRaisesRegex(OSError, "state write failed"):
            self.pipeline._flush_batch()

        self.assertEqual(len(self.pipeline._pending_predictions), 3)
        self.assertEqual(self.pipeline._pending_cursor, [1003.0, "e3"])
        self.assertIsNone(self.pipeline.checkpoint.get_search_after())


class _StopLoop(BaseException):
    """Break run_forever's poll loop once the test's polls are consumed."""


class TestFlushCriteria(unittest.TestCase):
    """Items 2 & 3: the buffer flushes on count OR time, whichever comes first."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/missing.yaml"
        self.cfg.elasticsearch.poll_interval_seconds = 1.0

        self.pipeline = RealtimePipeline(self.cfg)
        self.pipeline.start_metrics_server = MagicMock()
        self.pipeline.anomaly_model._model = MockIsolationForest()

        # Known template -> fixed group, so _process_one buffers one vector each.
        self.pipeline.group_registry.upsert(
            GroupState(group_id="G_AUTH", service="auth", first_seen=1000.0, last_seen=1000.0)
        )
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id="T_AUTH", template_text="t", service="auth",
                group_id="G_AUTH", first_seen=1000.0, last_seen=1000.0,
            )
        )
        self.pipeline.parser.parse = MagicMock(side_effect=lambda raw: ParsedEvent(
            raw=raw, template_id="T_AUTH", template="t", parameters=[], is_new_template=False,
        ))
        self.pipeline.embedder.embed_one = MagicMock(return_value=[0.1] * 8)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _batch(self, size, base_ts=1000.0):
        return [
            RawLog(event_id=f"e{i}", timestamp=base_ts + i * 0.001,
                   service="auth", level="INFO", message="m")
            for i in range(size)
        ]

    def test_flush_by_count(self):
        """A single poll delivering exactly predict_batch_size events scores in
        one predict_batch call and leaves the buffer empty."""
        self.cfg.anomaly.predict_batch_size = 5
        self.cfg.anomaly.predict_max_wait_seconds = 3600.0  # time path disabled

        batch = self._batch(5)
        self.pipeline.collector.poll_batch = MagicMock(side_effect=[(batch, [1000.5, "e4"]), _StopLoop])
        spy = MagicMock(wraps=self.pipeline.anomaly_model.predict_batch)
        self.pipeline.anomaly_model.predict_batch = spy

        with self.assertRaises(_StopLoop):
            self.pipeline.run_forever()

        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(spy.call_args.args[0]), 5)
        self.assertEqual(self.pipeline._pending_predictions, [])
        self.pipeline.checkpoint  # committed via flush
        self.assertEqual(self.pipeline.checkpoint.get_search_after(), [1000.5, "e4"])

    def test_flush_by_time_below_count_threshold(self):
        """A buffer that never reaches predict_batch_size still flushes once
        predict_max_wait_seconds elapses. A fake monotonic clock advanced by the
        loop's own sleep drives the timer without real waiting."""
        self.cfg.anomaly.predict_batch_size = 1000  # count path effectively off
        self.cfg.anomaly.predict_max_wait_seconds = 1.0

        clock = {"t": 500.0}
        sleeps = {"n": 0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(duration):
            clock["t"] += duration          # advancing the timer past max_wait
            sleeps["n"] += 1
            if sleeps["n"] >= 4:
                raise _StopLoop

        batch = self._batch(3)
        # One real batch, then empty polls that let the 1s timer expire.
        self.pipeline.collector.poll_batch = MagicMock(
            side_effect=[(batch, [1000.5, "e2"])] + [([], None)] * 10
        )
        spy = MagicMock(wraps=self.pipeline.anomaly_model.predict_batch)
        self.pipeline.anomaly_model.predict_batch = spy

        with patch.object(realtime_pipeline.time, "monotonic", side_effect=fake_monotonic):
            with patch.object(realtime_pipeline.time, "sleep", side_effect=fake_sleep):
                with self.assertRaises(_StopLoop):
                    self.pipeline.run_forever()

        # Flushed despite only 3 < 1000 buffered: the time criterion fired.
        self.assertGreaterEqual(spy.call_count, 1)
        self.assertEqual(len(spy.call_args_list[0].args[0]), 3)
        # The real batch's cursor was committed at the time-triggered flush.
        self.assertEqual(self.pipeline.checkpoint.get_search_after(), [1000.5, "e2"])


if __name__ == "__main__":
    unittest.main()

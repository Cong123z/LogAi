"""Unit and integration tests for Training Pipeline streaming refactor:
1. Stream batch parsing into Drain3.
2. LocalTrainingDedup in RAM dropping duplicates without touching realtime dedup.
3. Isolated CheckpointStore (training_checkpoint.json) without touching realtime checkpoint.json.
4. Resume training capability via initial_search_after cursor and durable event index.
5. Backward compatibility with in-memory List[RawLog].
6. Full 3-phase execution and artifact generation.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
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

mock_np = MockNumpy()
sys.modules["numpy"] = mock_np

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
from logai.storage.dedup import LocalTrainingDedup
from logai.training.train_pipeline import TrainingPipeline, run_training_from_elasticsearch


class TestTrainingPipelineStreaming(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/non_existent.yaml"
        self.cfg.anomaly.min_training_samples = 5
        self.cfg.training.batch_size = 5

        self.pipeline = TrainingPipeline(self.cfg)

        # Mock embedder and clusterer for deterministic fast testing
        self.pipeline.embedder.embed = MagicMock(return_value=[[0.1] * 8])
        self.pipeline.clusterer.cluster = MagicMock(return_value={"T_AUTH": 0, "T_PAYMENT": 0})
        self.pipeline.clusterer.compute_centroid = MagicMock(return_value=[0.1] * 8)
        self.pipeline.doc_matcher.match_all = MagicMock(return_value={})

        # Mock parser to return predictable ParsedEvent
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

    def test_streaming_batches_into_drain3(self):
        """Verify that streaming batches yields events into Drain3, updates TemplateRegistry,
        updates training_checkpoint.json per batch, and completes all 3 phases."""
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )

        batch_1 = [
            RawLog(event_id="e1", timestamp=1000.0, message="m1", service="auth", level="INFO"),
            RawLog(event_id="e2", timestamp=1001.0, message="m2", service="auth", level="INFO"),
            RawLog(event_id="e3", timestamp=1002.0, message="m3", service="auth", level="INFO"),
        ]
        cursor_1 = [1002000, "e3"]

        batch_2 = [
            RawLog(event_id="e4", timestamp=1003.0, message="m4", service="payment", level="INFO"),
            RawLog(event_id="e5", timestamp=1004.0, message="m5", service="payment", level="INFO"),
            RawLog(event_id="e6", timestamp=1005.0, message="m6", service="payment", level="INFO"),
        ]
        cursor_2 = [1005000, "e6"]

        batch_stream = [(batch_1, cursor_1), (batch_2, cursor_2)]

        self.pipeline.run(batch_stream, checkpoint_store=training_cp)

        # 1. Verify Template Registry was built
        templates = self.pipeline.template_registry.all_templates()
        self.assertEqual(len(templates), 2)
        template_map = {t.template_id: t for t in templates}
        self.assertIn("T_AUTH", template_map)
        self.assertIn("T_PAYMENT", template_map)
        self.assertEqual(template_map["T_AUTH"].event_count, 3)
        self.assertEqual(template_map["T_PAYMENT"].event_count, 3)

        # 2. Verify Group Registry was built
        groups = self.pipeline.group_registry.all_groups()
        self.assertTrue(len(groups) >= 1)

        # 3. Verify Global model was saved
        self.assertTrue(self.pipeline.model_store.exists(GLOBAL_MODEL_KEY))

        # 4. Verify training checkpoint is cleared upon successful completion
        self.assertFalse(training_cp.path.exists())

    def test_local_dedup_drops_network_retries_without_touching_realtime(self):
        """Verify that duplicate event_ids in stream are dropped in RAM,
        and realtime dedup_index.json is NOT touched or created."""
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )

        batch_1 = [
            RawLog(event_id="dup_1", timestamp=1000.0, message="m1", service="auth", level="INFO"),
            RawLog(event_id="dup_1", timestamp=1000.0, message="m1 retry", service="auth", level="INFO"),  # retry
            RawLog(event_id="unique_2", timestamp=1001.0, message="m2", service="auth", level="INFO"),
        ]
        batch_2 = [
            RawLog(event_id="unique_2", timestamp=1001.0, message="m2 overlap", service="auth", level="INFO"),  # overlap
            RawLog(event_id="unique_3", timestamp=1002.0, message="m3", service="auth", level="INFO"),
            RawLog(event_id="unique_4", timestamp=1003.0, message="m4", service="auth", level="INFO"),
            RawLog(event_id="unique_5", timestamp=1004.0, message="m5", service="auth", level="INFO"),
        ]
        batch_stream = [(batch_1, [1001000, "unique_2"]), (batch_2, [1004000, "unique_5"])]

        self.pipeline.run(batch_stream, checkpoint_store=training_cp)

        # 5 unique events: dup_1, unique_2, unique_3, unique_4, unique_5
        t_auth = self.pipeline.template_registry.get("T_AUTH")
        self.assertIsNotNone(t_auth)
        self.assertEqual(t_auth.event_count, 5)

        # Verify realtime dedup_index.json was NEVER created
        realtime_dedup_file = Path(self.cfg.storage.base_dir) / self.cfg.storage.dedup_index_file
        self.assertFalse(realtime_dedup_file.exists())

        # Verify realtime checkpoint.json was NEVER created
        realtime_checkpoint_file = Path(self.cfg.storage.base_dir) / self.cfg.storage.checkpoint_file
        self.assertFalse(realtime_checkpoint_file.exists())

    def test_checkpoint_isolation_and_resume_capability(self):
        """Resume must train from both pre- and post-interruption batches."""
        training_cp = CheckpointStore(
            self.cfg.storage, checkpoint_file=self.cfg.storage.training_checkpoint_file
        )

        batch_1 = [
            RawLog(event_id="e1", timestamp=1000.0, message="m1", service="auth", level="INFO"),
            RawLog(event_id="e2", timestamp=1001.0, message="m2", service="auth", level="INFO"),
        ]
        cursor_1 = [1001000, "e2"]

        # Simulate exception during batch 2
        def failing_stream():
            yield (batch_1, cursor_1)
            raise ConnectionError("Simulated Elasticsearch connection drop")

        with self.assertRaises(ConnectionError):
            self.pipeline.run(failing_stream(), checkpoint_store=training_cp)

        # Checkpoint file must still exist with cursor_1
        self.assertTrue(training_cp.path.exists())
        self.assertEqual(training_cp.get_search_after(), cursor_1)
        event_index = Path(self.cfg.storage.base_dir) / self.cfg.storage.training_event_index_file
        self.assertTrue(event_index.exists())

        # Resume verification with run_training_from_elasticsearch mock
        mock_collector = MagicMock()
        mock_collector.stream_historical_batches.return_value = [
            (
                [
                    RawLog(event_id="e3", timestamp=1002.0, message="m3", service="auth", level="INFO"),
                    RawLog(event_id="e4", timestamp=1003.0, message="m4", service="auth", level="INFO"),
                    RawLog(event_id="e5", timestamp=1004.0, message="m5", service="auth", level="INFO"),
                ],
                [1004000, "e5"],
            )
        ]

        with patch("logai.training.train_pipeline.ElasticsearchCollector", return_value=mock_collector):
            with patch("logai.training.train_pipeline.TrainingPipeline", return_value=self.pipeline):
                with patch.object(self.pipeline.anomaly_model, "train", return_value=True) as train:
                    run_training_from_elasticsearch(self.cfg, lookback_seconds=3600)

                # Verify collector received the saved initial_search_after
                call_kwargs = mock_collector.stream_historical_batches.call_args[1]
                self.assertEqual(call_kwargs["initial_search_after"], cursor_1)
                # e1/e2 from durable storage plus e3/e4/e5 from resume.
                self.assertEqual(len(train.call_args.args[0]), 5)

        self.assertFalse(event_index.exists())

    def test_backward_compatibility_with_in_memory_list(self):
        """Verify that passing a regular List[RawLog] to pipeline.run works seamlessly."""
        logs = [
            RawLog(event_id="e3", timestamp=1002.0, message="m3", service="auth", level="INFO"),
            RawLog(event_id="e1", timestamp=1000.0, message="m1", service="auth", level="INFO"),
            RawLog(event_id="e2", timestamp=1001.0, message="m2", service="auth", level="INFO"),
            RawLog(event_id="e4", timestamp=1003.0, message="m4", service="auth", level="INFO"),
            RawLog(event_id="e5", timestamp=1004.0, message="m5", service="auth", level="INFO"),
        ]

        self.pipeline.run(logs)

        t_auth = self.pipeline.template_registry.get("T_AUTH")
        self.assertIsNotNone(t_auth)
        self.assertEqual(t_auth.event_count, 5)
        self.assertEqual(t_auth.first_seen, 1000.0)
        self.assertEqual(t_auth.last_seen, 1004.0)
        self.assertTrue(self.pipeline.model_store.exists(GLOBAL_MODEL_KEY))


if __name__ == "__main__":
    unittest.main()

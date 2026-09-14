"""Unit tests verifying the fast-path for Pending/Unassigned templates in _assign_group.

Prevents the critical CPU-starvation regression where unassigned templates
re-triggered SentenceTransformer embed_one() and synchronous disk I/O
on every single occurrence.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# Lightweight mocks for external libraries if running outside full environment
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

import numpy as np

from logai.config import AppConfig
from logai.models import GroupState, GroupedEvent, ParsedEvent, RawLog, TemplateState
from logai.realtime.realtime_pipeline import PENDING_GROUP_ID, RealtimePipeline


class TestPendingTemplateFastPath(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = AppConfig()
        self.config.storage.base_dir = self.temp_dir
        self.config.storage.model_dir = f"{self.temp_dir}/models"
        self.config.drain3.persistence_path = f"{self.temp_dir}/drain3.bin"

        self.pipeline = RealtimePipeline(self.config)

        # Mock embedder and clusterer to track invocations
        self.pipeline.embedder.embed_one = MagicMock(return_value=np.zeros(384))
        self.pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=(None, 0.55))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_event(self, template_id: str, template: str, timestamp: float) -> ParsedEvent:
        return ParsedEvent(
            raw=RawLog(
                event_id=f"evt_{template_id}_{timestamp}",
                timestamp=timestamp,
                service="hdfs",
                level="INFO",
                message=f"message for {template}",
            ),
            template_id=template_id,
            template=template,
            parameters=[],
            is_new_template=False,
        )

    def test_unassigned_template_calls_embed_one_only_once(self):
        """100 events of an unassigned template must call embed_one() EXACTLY once."""
        template_id = "T00099"
        template_text = "<*>:Transmitted block <*> to <*>"

        results = []
        for i in range(100):
            event = self._make_event(template_id, template_text, timestamp=1000.0 + i)
            res = self.pipeline._assign_group(event)
            results.append(res)

        # All results should be None (unassigned / pending)
        self.assertTrue(all(r is None for r in results))

        # embed_one must be called ONLY ONCE, not 100 times!
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 1)
        self.assertEqual(self.pipeline.clusterer.assign_to_nearest_group.call_count, 1)

        # Template registry state must reflect all 100 events
        state = self.pipeline.template_registry.get(template_id)
        self.assertIsNotNone(state)
        self.assertEqual(state.event_count, 100)
        self.assertEqual(state.group_id, PENDING_GROUP_ID)
        self.assertEqual(state.first_seen, 1000.0)
        self.assertEqual(state.last_seen, 1099.0)

    def test_known_grouped_template_bypasses_embed_one(self):
        """Pre-existing grouped templates must never call embed_one()."""
        group_id = "G0001"
        self.pipeline.group_registry.upsert(
            GroupState(group_id=group_id, first_seen=100.0, last_seen=100.0)
        )
        template_id = "T00001"
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id=template_id,
                template_text="User <*> logged in",
                service="hdfs",
                first_seen=100.0,
                last_seen=100.0,
                event_count=5,
                group_id=group_id,
            )
        )

        event = self._make_event(template_id, "User alice logged in", timestamp=200.0)
        res = self.pipeline._assign_group(event)

        self.assertIsInstance(res, GroupedEvent)
        self.assertEqual(res.group_id, group_id)
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 0)

        state = self.pipeline.template_registry.get(template_id)
        self.assertEqual(state.event_count, 6)
        self.assertEqual(state.last_seen, 200.0)

    def test_legacy_none_group_id_heals_to_pending_without_reembedding(self):
        """Templates stored with group_id=None (legacy on disk) must heal to PENDING_GROUP_ID without re-embedding."""
        template_id = "T_LEGACY"
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id=template_id,
                template_text="Legacy unassigned template",
                service="hdfs",
                first_seen=50.0,
                last_seen=50.0,
                event_count=1,
                group_id=None,
            )
        )

        event = self._make_event(template_id, "Legacy unassigned template", timestamp=60.0)
        res = self.pipeline._assign_group(event)

        self.assertIsNone(res)
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 0)

        state = self.pipeline.template_registry.get(template_id)
        self.assertEqual(state.group_id, PENDING_GROUP_ID)
        self.assertEqual(state.event_count, 2)
        self.assertEqual(state.last_seen, 60.0)


if __name__ == "__main__":
    unittest.main()

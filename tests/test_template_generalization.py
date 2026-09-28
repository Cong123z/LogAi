"""Fix #4: TemplateRegistry must persist Drain3's generalised (`<*>`) template,
not the raw first-event log line.

Root cause guarded here: the first event of a Drain3 cluster is emitted before
the miner has a second sample to generalise against, so its per-event
`template_text` is the raw log (concrete IPs/IDs). `_rebuild_template_registry`
used to keep that first raw string. It must instead read the miner's converged
`cluster.get_template()`.

The fix is tested deterministically by injecting a fake Drain3 miner, so it does
not depend on the real drain3 surviving the suite's global sys.modules stubbing.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

for mod in ["prometheus_client", "drain3",
            "drain3.template_miner", "drain3.file_persistence",
            "drain3.template_miner_config"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from logai.config import AppConfig
from logai.models import ParsedEvent, RawLog
from logai.training.train_pipeline import TrainingPipeline


class _FakeCluster:
    def __init__(self, template: str):
        self._template = template

    def get_template(self) -> str:
        return self._template


class TestTemplateGeneralization(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/none.yaml"

        self.pipeline = TrainingPipeline(self.cfg)
        # Inject a converged Drain3 cluster map: T00001 -> generalised template.
        self.pipeline.parser.miner.drain.id_to_cluster = {
            1: _FakeCluster("User <*> logged in from <*>"),
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_events(self):
        # Four events of cluster T00001. The first record carries the RAW text
        # (as Drain3 emits it on cluster_created); later ones the generalised
        # form. The registry must ignore both and query the live miner.
        raws = [
            ("e0", 1000.0, "User alice logged in from 10.0.0.1"),
            ("e1", 1001.0, "User <*> logged in from <*>"),
            ("e2", 1002.0, "User <*> logged in from <*>"),
            ("e3", 1003.0, "User <*> logged in from <*>"),
        ]
        events = [
            ParsedEvent(
                raw=RawLog(event_id=eid, timestamp=ts, message=msg,
                           service="auth", level="INFO"),
                template_id="T00001",
                template=msg,
                parameters=[],
                is_new_template=(eid == "e0"),
            )
            for eid, ts, msg in raws
        ]
        self.pipeline.event_index.append_batch(events)

    def test_registry_stores_generalized_template(self):
        self._write_events()
        self.pipeline._rebuild_template_registry()

        templates = self.pipeline.template_registry.all_templates()
        self.assertEqual(len(templates), 1)
        text = templates[0].template_text

        self.assertEqual(text, "User <*> logged in from <*>")
        self.assertNotIn("alice", text)
        self.assertNotIn("10.0.0.1", text)
        self.assertEqual(templates[0].event_count, 4)

    def test_generalized_helper_returns_live_template(self):
        self.assertEqual(
            self.pipeline._generalized_template_text("T00001"),
            "User <*> logged in from <*>",
        )

    def test_generalized_helper_falls_back_on_non_numeric_id(self):
        # Mocked pipelines use ids like "T_AUTH"; must not raise, returns None.
        self.assertIsNone(self.pipeline._generalized_template_text("T_AUTH"))

    def test_generalized_helper_none_for_missing_cluster(self):
        self.assertIsNone(self.pipeline._generalized_template_text("T99999"))

    def test_rebuild_falls_back_to_recorded_text_when_no_cluster(self):
        # A template id with no live cluster keeps the recorded template_text.
        event = ParsedEvent(
            raw=RawLog(event_id="x0", timestamp=5.0, message="orphan raw text",
                       service="svc", level="INFO"),
            template_id="T42424",
            template="orphan raw text",
            parameters=[],
            is_new_template=True,
        )
        self.pipeline.event_index.append_batch([event])
        self.pipeline._rebuild_template_registry()

        state = self.pipeline.template_registry.get("T42424")
        self.assertIsNotNone(state)
        self.assertEqual(state.template_text, "orphan raw text")


if __name__ == "__main__":
    unittest.main()

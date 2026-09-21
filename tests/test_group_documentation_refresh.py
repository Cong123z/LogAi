from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from logai.config import AppConfig
from logai.docmatch.refresh_worker import DocumentationRefreshWorker
from logai.docmatch.doc_matcher import MatchResult
from logai.models import GroupState, TemplateState
from logai.storage.documentation import DocumentationCorpusStore, group_fingerprint
from logai.storage.registries import GroupRegistry, TemplateRegistry


class FakeMatcher:
    def __init__(self):
        self.ready = True
        self.last_error = None
        self.entries = [
            SimpleNamespace(doc_id="DOC-DB"),
            SimpleNamespace(doc_id="DOC-AUTH"),
        ]

    def reload(self):
        return True

    def match(self, group_id, _centroid):
        return MatchResult(group_id, "DOC-DB", 0.99, True, "ERR_DB")

    def match_document(self, group_id, documentation_id, _centroid):
        return MatchResult(group_id, documentation_id, 0.1, True, "ERR_AUTH")


class TestGroupDocumentationRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = str(base)
        self.cfg.doc_matcher.corpus_path = str(base / "documentation_corpus.json")
        self.cfg.doc_matcher.overrides_path = str(base / "documentation_overrides.json")
        self.cfg.doc_matcher.status_path = str(base / "documentation_status.json")
        self.cfg.doc_matcher.seed_corpus_path = str(base / "seed.yaml")
        Path(self.cfg.doc_matcher.seed_corpus_path).write_text(
            "- id: DOC-DB\n  title: DB\n  text: database timeout\n  error_code: ERR_DB\n"
            "- id: DOC-AUTH\n  title: Auth\n  text: authentication failed\n  error_code: ERR_AUTH\n",
            encoding="utf-8",
        )
        self.store = DocumentationCorpusStore.from_config(self.cfg)
        self.templates = TemplateRegistry(self.cfg.storage)
        self.groups = GroupRegistry(self.cfg.storage)
        self.templates.upsert(TemplateState("T1", "database <*> timeout", "api", group_id="G1"))
        self.groups.upsert(GroupState("G1", template_ids=["T1"], representative_template="database <*> timeout"))
        self.groups.set_centroid("G1", [1.0, 0.0])
        self.matcher = FakeMatcher()
        self.worker = DocumentationRefreshWorker(self.store, self.matcher, self.groups, self.templates, 5)

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_then_manual_override_and_clear(self):
        self.assertTrue(self.worker.refresh_once())
        automatic = self.groups.get("G1")
        self.assertEqual(automatic.documentation_id, "DOC-DB")
        self.assertEqual(automatic.documentation_source, "automatic")

        snapshot = self.store.load_overrides()
        fingerprint = group_fingerprint(["T1"], ["database <*> timeout"])
        self.store.set_override("G1", "DOC-AUTH", fingerprint, snapshot["revision"])
        self.assertTrue(self.worker.refresh_once())
        manual = self.groups.get("G1")
        self.assertEqual(manual.documentation_id, "DOC-AUTH")
        self.assertEqual(manual.documentation_source, "manual")

        current = self.store.load_overrides()
        self.store.clear_override("G1", current["revision"])
        self.assertTrue(self.worker.refresh_once())
        self.assertEqual(self.groups.get("G1").documentation_id, "DOC-DB")

    def test_changed_group_fingerprint_suspends_override(self):
        snapshot = self.store.load_overrides()
        self.store.set_override("G1", "DOC-AUTH", "old-fingerprint", snapshot["revision"])
        self.worker.refresh_once()
        state = self.groups.get("G1")
        self.assertEqual(state.documentation_source, "stale_override")
        self.assertEqual(state.documentation_id, "DOC-DB")

if __name__ == "__main__":
    unittest.main()

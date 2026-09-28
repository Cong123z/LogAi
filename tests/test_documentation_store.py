from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from logai.storage.documentation import (
    DocumentInUse,
    DocumentationCorpusStore,
    RevisionConflict,
    _revision,
)


class TestDocumentationCorpusStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.seed = base / "seed.yaml"
        self.seed.write_text(
            "- id: DOC-1\n  title: Timeout\n  text: Database timeout\n  error_code: ERR_DB\n",
            encoding="utf-8",
        )
        self.store = DocumentationCorpusStore(
            base / "documentation_corpus.json",
            base / "documentation_overrides.json",
            base / "documentation_status.json",
            self.seed,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_imports_yaml_once_and_persists_json(self):
        corpus = self.store.load_corpus()
        self.assertEqual(corpus["entries"][0]["id"], "DOC-1")
        self.seed.write_text("- id: DOC-2\n  text: replacement\n", encoding="utf-8")
        reloaded = DocumentationCorpusStore(
            self.store.corpus_path,
            self.store.overrides_path,
            self.store.status_path,
            self.seed,
        )
        self.assertEqual(reloaded.load_corpus()["entries"][0]["id"], "DOC-1")

    def test_crud_and_stale_revision(self):
        original = self.store.load_corpus()
        entry, changed = self.store.create_document(
            {"title": "Auth", "text": "Authentication failed", "error_code": "ERR_AUTH"},
            original["revision"],
        )
        self.assertEqual(entry["id"], "DOC-001")
        with self.assertRaises(RevisionConflict):
            self.store.create_document({"text": "stale"}, original["revision"])
        updated, latest = self.store.update_document(
            entry["id"], {"title": "Auth v2", "text": entry["text"], "error_code": ""},
            changed["revision"],
        )
        self.assertEqual(updated["title"], "Auth v2")
        final = self.store.delete_document(entry["id"], latest["revision"])
        self.assertEqual(len(final["entries"]), 1)

    def test_ordered_ids_are_monotonic_across_deletion_and_restart(self):
        original = self.store.load_corpus()
        first, after_first = self.store.create_document(
            {"text": "First generated document"}, original["revision"]
        )
        second, after_second = self.store.create_document(
            {"text": "Second generated document"}, after_first["revision"]
        )
        after_delete = self.store.delete_document(
            first["id"], after_second["revision"]
        )
        reloaded = DocumentationCorpusStore(
            self.store.corpus_path,
            self.store.overrides_path,
            self.store.status_path,
            self.seed,
        )
        third, _ = reloaded.create_document(
            {"text": "Third generated document"}, after_delete["revision"]
        )

        self.assertEqual(first["id"], "DOC-001")
        self.assertEqual(second["id"], "DOC-002")
        self.assertEqual(third["id"], "DOC-003")

    def test_legacy_corpus_derives_counter_and_preserves_named_ids(self):
        original = self.store.load_corpus()
        entries = [
            {"id": "DOC-007", "title": "", "text": "Ordered", "error_code": ""},
            {"id": "DOC-DB-001", "title": "", "text": "Named", "error_code": ""},
        ]
        legacy = {
            "schema_version": 1,
            "revision": original["revision"],
            "updated_at": original["updated_at"],
            "entries": original["entries"],
        }
        self.store.corpus_path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded_legacy = self.store.load_corpus()
        self.assertEqual(loaded_legacy["next_document_number"], 1)

        legacy["entries"] = entries
        legacy["revision"] = _revision(entries)
        self.store.corpus_path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.store.load_corpus()
        created, _ = self.store.create_document(
            {"text": "Next document"}, loaded["revision"]
        )

        self.assertEqual(
            [entry["id"] for entry in loaded["entries"]],
            ["DOC-007", "DOC-DB-001"],
        )
        self.assertEqual(created["id"], "DOC-008")

    def test_assigned_document_cannot_be_deleted(self):
        corpus = self.store.load_corpus()
        overrides = self.store.load_overrides()
        self.store.set_override("G0001", "DOC-1", "fingerprint", overrides["revision"])
        with self.assertRaises(DocumentInUse) as caught:
            self.store.delete_document("DOC-1", corpus["revision"])
        self.assertEqual(caught.exception.group_ids, ["G0001"])

    def test_forced_delete_removes_manual_assignment(self):
        corpus = self.store.load_corpus()
        overrides = self.store.load_overrides()
        self.store.set_override("G0001", "DOC-1", "fingerprint", overrides["revision"])

        deleted = self.store.delete_document(
            "DOC-1", corpus["revision"], force=True
        )

        self.assertEqual(deleted["entries"], [])
        self.assertEqual(self.store.load_overrides()["overrides"], {})

    def test_clear_persists_suppression_and_assignment_removes_it(self):
        current = self.store.load_overrides()
        cleared = self.store.clear_override("G0001", current["revision"])
        self.assertIn("G0001", cleared["cleared_groups"])

        assigned = self.store.set_override(
            "G0001", "DOC-1", "fingerprint", cleared["revision"]
        )
        self.assertNotIn("G0001", assigned["cleared_groups"])

    def test_empty_corpus_is_valid(self):
        corpus = self.store.load_corpus()
        emptied = self.store.delete_document("DOC-1", corpus["revision"])
        self.assertEqual(emptied["entries"], [])
        on_disk = json.loads(self.store.corpus_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["entries"], [])


if __name__ == "__main__":
    unittest.main()

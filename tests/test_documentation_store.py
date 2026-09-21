from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from logai.storage.documentation import (
    DocumentInUse,
    DocumentationCorpusStore,
    RevisionConflict,
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
        self.assertTrue(entry["id"].startswith("DOC-"))
        with self.assertRaises(RevisionConflict):
            self.store.create_document({"text": "stale"}, original["revision"])
        updated, latest = self.store.update_document(
            entry["id"], {"title": "Auth v2", "text": entry["text"], "error_code": ""},
            changed["revision"],
        )
        self.assertEqual(updated["title"], "Auth v2")
        final = self.store.delete_document(entry["id"], latest["revision"])
        self.assertEqual(len(final["entries"]), 1)

    def test_assigned_document_cannot_be_deleted(self):
        corpus = self.store.load_corpus()
        overrides = self.store.load_overrides()
        self.store.set_override("G0001", "DOC-1", "fingerprint", overrides["revision"])
        with self.assertRaises(DocumentInUse) as caught:
            self.store.delete_document("DOC-1", corpus["revision"])
        self.assertEqual(caught.exception.group_ids, ["G0001"])

    def test_empty_corpus_is_valid(self):
        corpus = self.store.load_corpus()
        emptied = self.store.delete_document("DOC-1", corpus["revision"])
        self.assertEqual(emptied["entries"], [])
        on_disk = json.loads(self.store.corpus_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["entries"], [])


if __name__ == "__main__":
    unittest.main()

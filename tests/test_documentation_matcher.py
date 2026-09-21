"""Focused resilience tests for the optional documentation enrichment stage."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

# Importing the matcher should not load a real transformer model in unit tests.
if "sentence_transformers" not in sys.modules:
    sys.modules["sentence_transformers"] = MagicMock()

from logai.config import DocMatcherConfig
from logai.docmatch.doc_matcher import DocumentationMatcher


class FakeEmbedder:
    def __init__(self, vectors):
        self.vectors = np.asarray(vectors, dtype=np.float32)
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return self.vectors[: len(texts)]


class TestDocumentationMatcherResilience(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.corpus_path = Path(self.temp_dir.name) / "corpus.yaml"
        self.cache_path = Path(self.temp_dir.name) / "cache.pkl"
        self.config = DocMatcherConfig(
            corpus_path=str(self.corpus_path), similarity_threshold=0.75
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_valid_corpus(self):
        self.corpus_path.write_text(
            "- id: DOC-1\n"
            "  title: Database timeout\n"
            "  text: Database connection timeout\n"
            "  error_code: ERR_DB_TIMEOUT\n"
            "- id: DOC-2\n"
            "  title: Authentication failure\n"
            "  text: User authentication failed\n",
            encoding="utf-8",
        )

    def test_valid_corpus_loads_and_matches(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )

        result = matcher.match("G1", np.array([1.0, 0.0]))

        self.assertTrue(matcher.ready)
        self.assertTrue(result.documented)
        self.assertEqual(result.documentation_id, "DOC-1")
        self.assertEqual(result.error_code, "ERR_DB_TIMEOUT")

    def test_failed_reload_preserves_last_known_good_snapshot(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )
        previous_entries = list(matcher.entries)
        previous_embeddings = matcher.embeddings.copy()
        self.corpus_path.write_text("not: a-list\n", encoding="utf-8")

        refreshed = matcher.reload()

        self.assertFalse(refreshed)
        self.assertTrue(matcher.ready)
        self.assertEqual(matcher.entries, previous_entries)
        self.assertEqual(matcher.embeddings.tolist(), previous_embeddings.tolist())

    def test_invalid_initial_corpus_disables_matcher_without_raising(self):
        self.corpus_path.write_text(
            "- id: duplicate\n  text: first\n"
            "- id: duplicate\n  text: second\n",
            encoding="utf-8",
        )

        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )

        self.assertFalse(matcher.ready)
        self.assertIn("Duplicate documentation id", matcher.last_error)
        self.assertFalse(matcher.match("G1", np.array([1.0, 0.0])).documented)

    def test_cache_write_failure_does_not_disable_valid_snapshot(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )
        matcher._cache_store.save = MagicMock(side_effect=OSError("disk full"))

        refreshed = matcher.reload()

        self.assertTrue(refreshed)
        self.assertTrue(matcher.ready)
        self.assertIsNone(matcher.last_error)

    def test_incompatible_centroid_is_rejected_explicitly(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )

        with self.assertRaisesRegex(ValueError, "incompatible"):
            matcher.match("G1", np.array([1.0, 0.0, 0.0]))

    def test_unchanged_corpus_does_not_reembed(self):
        self._write_valid_corpus()
        embedder = FakeEmbedder([[1.0, 0.0], [0.0, 1.0]])
        matcher = DocumentationMatcher(self.config, embedder, self.cache_path)

        refreshed = matcher.reload()

        self.assertTrue(refreshed)
        self.assertEqual(embedder.calls, 1)

    def test_metadata_only_change_reuses_document_embeddings(self):
        self._write_valid_corpus()
        embedder = FakeEmbedder([[1.0, 0.0], [0.0, 1.0]])
        matcher = DocumentationMatcher(self.config, embedder, self.cache_path)
        self.corpus_path.write_text(
            "- id: DOC-1\n  title: Renamed timeout\n"
            "  text: Database connection timeout\n  error_code: NEW_CODE\n"
            "- id: DOC-2\n  title: Renamed auth\n"
            "  text: User authentication failed\n",
            encoding="utf-8",
        )

        self.assertTrue(matcher.reload())
        self.assertEqual(embedder.calls, 1)

    def test_repeated_group_centroid_uses_cached_match(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )
        centroid = np.array([1.0, 0.0])

        first = matcher.match("G1", centroid)
        second = matcher.match("G1", centroid.copy())

        self.assertIs(first, second)
        self.assertEqual(len(matcher._match_cache), 1)

    def test_changed_corpus_invalidates_match_cache(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )
        matcher.match("G1", np.array([1.0, 0.0]))
        self.corpus_path.write_text(
            "- id: DOC-NEW\n  text: New documentation\n", encoding="utf-8"
        )
        matcher.embedder = FakeEmbedder([[0.0, 1.0]])

        refreshed = matcher.reload()

        self.assertTrue(refreshed)
        self.assertEqual(matcher._match_cache, {})
        self.assertEqual(
            matcher.match("G1", np.array([0.0, 1.0])).documentation_id,
            "DOC-NEW",
        )

    def test_match_all_isolates_an_invalid_group_centroid(self):
        self._write_valid_corpus()
        matcher = DocumentationMatcher(
            self.config,
            FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]),
            self.cache_path,
        )

        results = matcher.match_all(
            {
                "G_VALID": np.array([1.0, 0.0]),
                "G_INVALID": np.array([1.0, 0.0, 0.0]),
            }
        )

        self.assertIn("G_VALID", results)
        self.assertNotIn("G_INVALID", results)


if __name__ == "__main__":
    unittest.main()

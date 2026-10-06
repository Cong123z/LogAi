"""LLM incident classification: candidates, classifier and pipeline wiring."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from logai.alert.alert_state_machine import AlertStateMachine
from logai.config import AlertConfig, DocMatcherConfig
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.storage.base import JSONStore


class FakeEmbedder:
    """Returns a fixed vector per text so candidate order is deterministic."""

    def __init__(self, vectors_by_text):
        self.vectors_by_text = vectors_by_text
        self.config = MagicMock(model_name="test-model", dimension=2)

    def embed(self, texts):
        return np.asarray([self.vectors_by_text[t] for t in texts], dtype=np.float32)


def _matcher(tmp_path: Path, docs) -> DocumentationMatcher:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"entries": [
        {"id": doc_id, "title": f"title {doc_id}", "text": text, "error_code": f"E_{doc_id}"}
        for doc_id, text, _ in docs
    ]}), encoding="utf-8")
    embedder = FakeEmbedder({text: vector for _, text, vector in docs})
    return DocumentationMatcher(
        DocMatcherConfig(corpus_path=str(corpus)), embedder, tmp_path / "cache.pkl"
    )


DOCS = [
    ("D1", "one", [1.0, 0.0]),
    ("D2", "two", [0.8, 0.6]),
    ("D3", "three", [0.0, 1.0]),
]


def test_top_k_orders_and_limits(tmp_path):
    matcher = _matcher(tmp_path, DOCS)
    hits = matcher.top_k(np.array([1.0, 0.0]), 2)
    assert [entry.doc_id for entry, _ in hits] == ["D1", "D2"]
    assert hits[0][1] >= hits[1][1]


def test_top_k_empty_corpus(tmp_path):
    empty = _matcher(tmp_path, [])
    assert empty.top_k(np.array([1.0, 0.0]), 5) == []
    matcher = _matcher(tmp_path, DOCS)
    assert matcher.top_k(None, 5) == []
    assert matcher.top_k(np.array([1.0, 0.0, 0.0]), 5) == []


def test_keys_in_states(tmp_path):
    store = JSONStore(tmp_path / "anomaly_state.json")
    store.bulk_set({
        json.dumps(["api", "GA"]): {"group_id": ["api", "GA"], "timestamp": 1.0, "alert_state": "ALERTING"},
        json.dumps(["api", "GB"]): {"group_id": ["api", "GB"], "timestamp": 1.0, "alert_state": "NORMAL"},
        "GC": {"group_id": "GC", "timestamp": 1.0, "alert_state": "ALERTING"},
    })
    sm = AlertStateMachine(AlertConfig(), store)
    assert sm.keys_in_states({"ALERTING", "COOLING"}) == {("api", "GA")}

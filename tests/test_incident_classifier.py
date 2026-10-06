"""LLM incident classification: candidates, classifier and pipeline wiring."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from logai.alert.alert_state_machine import AlertStateMachine
from logai.config import AlertConfig, DocMatcherConfig
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.storage.base import JSONStore


@pytest.fixture(autouse=True)
def _use_real_numpy(real_numpy):
    global np
    np = real_numpy


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


# --- Task 2: LLMConfig + IncidentClassifier ---------------------------------

import os
from unittest.mock import patch

import requests

from logai.config import LLMConfig, StorageConfig, load_config
from logai.incident.classifier import IncidentClassifier, summarize_parameters
from logai.models import GroupState, TemplateState
from logai.storage.registries import GroupRegistry, TemplateRegistry

KEY = ("api", "GA")
STORE_KEY = json.dumps(["api", "GA"])
ALERT = {"state": "ALERTING", "score": 0.8, "count_1m": 400}
CLASSIFIER_DOCS = [
    ("DOC-001", "database timeout", [1.0, 0.0]),
    ("DOC-002", "auth failure", [0.0, 1.0]),
]


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _reply(content):
    if not isinstance(content, str):
        content = json.dumps(content)
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


SUGGESTION = {"title": "Restart gateway", "text": "Restart the MOMO gateway pod.", "error_code": "E_GW"}


def _classifier(tmp_path, responses, **config):
    storage = StorageConfig(base_dir=str(tmp_path))
    templates = TemplateRegistry(storage)
    groups = GroupRegistry(storage)
    templates.upsert(TemplateState(template_id="T1", template_text="db timeout <*>", service="api",
                                   level="ERROR", event_count=9, group_id="GA"))
    groups.upsert(GroupState(group_id="GA", service="api", template_ids=["T1"],
                             representative_template="db timeout <*>", event_count=9))
    groups.set_centroid("GA", np.array([1.0, 0.0]))
    groups.upsert(GroupState(group_id="GB", service="api", template_ids=["T1"]))
    session = FakeSession(responses)
    store = JSONStore(tmp_path / "incident_analysis.json")
    results = []
    clf = IncidentClassifier(
        LLMConfig(endpoint="http://llm/v1/chat/completions", model="m",
                  retry_backoff_seconds=0, **config),
        _matcher(tmp_path, CLASSIFIER_DOCS), groups, templates, store,
        session=session, on_result=results.append,
    )
    return clf, session, store, results


def _evidence(session):
    return json.loads(session.calls[0]["json"]["messages"][1]["content"])


def test_valid_candidate_matched(tmp_path):
    clf, _, store, results = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "confidence": 0.9, "reasoning": "db", "suggestion": None})])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "done" and rec["documentation_id"] == "DOC-001"
    assert rec["document_title"] == "title DOC-001"
    assert store.get(STORE_KEY)["documentation_id"] == "DOC-001"
    assert results == ["matched"]


def test_null_with_suggestion(tmp_path):
    clf, _, _, results = _classifier(tmp_path, [_reply(
        {"documentation_id": None, "confidence": 0.3, "reasoning": "new", "suggestion": SUGGESTION})])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "done" and rec["documentation_id"] is None
    assert rec["suggestion"]["title"] == "Restart gateway"
    assert results == ["suggested"]


def test_hallucinated_id_downgraded(tmp_path):
    clf, _, _, _ = _classifier(tmp_path, [
        _reply({"documentation_id": "DOC-999", "reasoning": "x", "suggestion": SUGGESTION}),
        _reply({"documentation_id": "DOC-999", "reasoning": "x", "suggestion": None}),
    ])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "done" and rec["documentation_id"] is None and rec["suggestion"]
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "failed"


def test_malformed_json_failed(tmp_path):
    clf, _, _, results = _classifier(tmp_path, [_reply("not json at all")])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "failed" and rec["error"]
    assert results == ["failed"]


def test_http_500_retried_then_failed(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [FakeResponse(500, {})] * 3)
    rec = clf.process(KEY, ALERT, [])
    assert len(session.calls) == 3
    assert rec["status"] == "failed" and "500" in rec["error"]


def test_fenced_reply_parsed(tmp_path):
    body = json.dumps({"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None})
    clf, _, _, _ = _classifier(tmp_path, [_reply(f"Here:\n```json\n{body}\n```")])
    assert clf.process(KEY, ALERT, [])["status"] == "done"


def test_no_centroid_still_suggests(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "reasoning": "x", "suggestion": SUGGESTION})])
    rec = clf.process(("api", "GB"), ALERT, [])
    assert _evidence(session)["candidates"] == []
    assert len(session.calls) == 1
    assert rec["documentation_id"] is None and rec["suggestion"]["title"] == "Restart gateway"


def test_suggestion_trimmed_and_typed(tmp_path):
    clf, _, _, _ = _classifier(tmp_path, [
        _reply({"documentation_id": None, "reasoning": "x",
                "suggestion": {"title": "x" * 500, "text": "fix", "error_code": "E"}}),
        _reply({"documentation_id": None, "reasoning": "x",
                "suggestion": {"title": 123, "text": "fix"}}),
    ])
    assert len(clf.process(KEY, ALERT, [])["suggestion"]["title"]) == 200
    assert clf.process(KEY, ALERT, [])["status"] == "failed"


def test_drop_group_removes_entries(tmp_path):
    clf, _, store, _ = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None})])
    clf.process(KEY, ALERT, [])
    assert clf.drop_group("GA") == 1
    assert store.get(STORE_KEY) is None


def test_summarize_parameters():
    assert summarize_parameters([
        ("T1", ["<*>", "MOMO"]), ("T1", ["<*>", "MOMO"]), ("T1", ["<*>", "VTP"]),
    ]) == [{"template_id": "T1", "slot": 1, "top_values": [["MOMO", 2], ["VTP", 1]]}]


def test_submit_writes_pending_and_dedupes(tmp_path):
    clf, _, store, _ = _classifier(tmp_path, [])
    assert clf.submit(KEY, ALERT, []) is True
    assert store.get(STORE_KEY)["status"] == "pending"
    assert clf.submit(KEY, ALERT, []) is False


def test_request_body(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None})])
    clf.process(KEY, ALERT, [{"template_id": "T1", "slot": 1, "top_values": [["MOMO", 2]]}])
    body = session.calls[0]["json"]
    assert body["model"] == "m" and body["temperature"] == 0 and body["max_tokens"] == 800
    evidence = _evidence(session)
    assert set(evidence) == {"service", "group_id", "alert", "group", "templates", "parameters", "candidates"}
    assert evidence["candidates"][0]["id"] == "DOC-001"
    assert evidence["templates"] == [{"id": "T1", "text": "db timeout <*>", "level": "ERROR", "count": 9}]
    assert "Authorization" not in session.calls[0]["headers"]

    clf, session, _, _ = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None})], api_key="k")
    clf.process(KEY, ALERT, [])
    assert session.calls[0]["headers"]["Authorization"] == "Bearer k"


def test_llm_env_overrides():
    with patch.dict(os.environ, {"LOGAI_LLM_ENDPOINT": "http://x", "LOGAI_LLM_MODEL": "qwen",
                                 "LOGAI_LLM_API_KEY": "s"}):
        llm = load_config().llm
    assert (llm.endpoint, llm.model, llm.api_key) == ("http://x", "qwen", "s")
    assert llm.max_candidates == 5 and llm.max_tokens == 800

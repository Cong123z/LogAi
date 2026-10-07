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
    clf, session, _, results = _classifier(tmp_path, [_reply("not json at all"), _reply("still not")])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "failed" and rec["error"]
    assert len(session.calls) == 2  # asked again once with a JSON reminder
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
    assert set(evidence) == {"service", "group_id", "alert", "group", "templates",
                             "unknown_templates", "past_incidents", "parameters", "candidates"}
    assert evidence["candidates"][0]["id"] == "DOC-001"
    # all-time counts are never sent; recent counts are 0 without activity
    assert evidence["templates"] == [{"id": "T1", "text": "db timeout <*>", "level": "ERROR",
                                      "count_15m": 0, "count_30m": 0, "rate_per_min": 0.0,
                                      "baseline_per_min": None, "ratio": None,
                                      "age_at_alert_minutes": 0.0}]
    assert evidence["group"] == {"representative_template": "db timeout <*>",
                                 "count_15m": 0, "count_30m": 0}
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


# --- Task 3: realtime pipeline wiring ---------------------------------------

from collections import deque

from logai.config import AppConfig
from logai.models import AnomalyResult, AnomalyState, GroupedEvent, RawLog

WK = ("auth", "G_AUTH")


def _pipeline(tmp_path, endpoint="http://llm"):
    from logai.realtime.realtime_pipeline import RealtimePipeline

    cfg = AppConfig()
    cfg.storage.base_dir = str(tmp_path)
    cfg.storage.model_dir = str(tmp_path / "models")
    cfg.drain3.persistence_path = str(tmp_path / "drain3_state.bin")
    cfg.doc_matcher.corpus_path = str(tmp_path / "missing.yaml")
    cfg.llm.endpoint = endpoint
    # Prometheus collectors are process-global; a mock keeps pipelines independent.
    with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
        return RealtimePipeline(cfg)


def _result(key):
    return AnomalyResult(group_id=key, timestamp=1.0, anomaly_score=0.9, anomaly=True, count_1m=12)


def _state(key, alert_state):
    return AnomalyState(group_id=key, timestamp=1.0, anomaly_score=0.9, alert_state=alert_state)






def test_params_buffered_without_placeholders(tmp_path):
    p = _pipeline(tmp_path)
    p._assign_group = lambda parsed: GroupedEvent(parsed=parsed, group_id="G_AUTH")
    for i, (reason, gateway) in enumerate([("Timeout", "VTP"), ("Unauthorized", "MOMO")]):
        p._process_one(RawLog(
            timestamp=1000.0 + i, service="auth", level="ERROR", event_id=f"e{i}",
            message=f"Recharge failed for msisdn=8491234567{i} code=E50{i} reason={reason} gateway={gateway}",
        ))
    buffered = p._recent_params[WK]
    assert isinstance(buffered, deque) and buffered.maxlen == 200
    assert [params[-2:] for _, params in buffered] == [["Unauthorized", "MOMO"]]


def test_classifier_results_counted(tmp_path):
    p = _pipeline(tmp_path)
    p.incident_classifier._on_result("matched")
    p.metrics.logai_llm_requests_total.labels.assert_called_with(result="matched")


def test_disabled_when_no_endpoint(tmp_path):
    p = _pipeline(tmp_path, endpoint="")
    # The classifier always exists (a web profile can enable it at runtime)
    # but stays disabled while no endpoint is configured.
    assert p.incident_classifier is not None and not p.incident_classifier.enabled


# --- Final review fixes -------------------------------------------------------

def test_restart_turns_stale_pending_into_failed(tmp_path):
    store = JSONStore(tmp_path / "incident_analysis.json")
    store.set(STORE_KEY, {"status": "pending", "service": "api", "group_id": "GA", "queued_at": 1.0})
    _classifier(tmp_path, [])
    rec = JSONStore(tmp_path / "incident_analysis.json").get(STORE_KEY)
    assert rec["status"] == "failed" and "restart" in rec["error"]


def test_null_error_code_suggestion_accepted(tmp_path):
    clf, _, _, _ = _classifier(tmp_path, [_reply({"documentation_id": None, "reasoning": "x",
        "suggestion": {"title": "Restart gateway", "text": "fix", "error_code": None}})])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "done" and rec["suggestion"]["error_code"] == ""


def test_prose_with_braces_before_json(tmp_path):
    body = json.dumps({"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None})
    clf, _, _, _ = _classifier(tmp_path, [_reply(f"Use {{placeholder}} values.\n```json\n{body}\n```")])
    assert clf.process(KEY, ALERT, [])["status"] == "done"


def test_submit_store_failure_does_not_raise_or_block(tmp_path):
    clf, _, store, _ = _classifier(tmp_path, [])
    original = store.set
    store.set = MagicMock(side_effect=OSError("disk full"))
    assert clf.submit(KEY, ALERT, []) is False
    store.set = original
    assert clf.submit(KEY, ALERT, []) is True


def test_parameter_evidence_bounded(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None})])
    clf.process(KEY, ALERT, summarize_parameters([("T1", ["x" * 5000]), ("T_OTHER", ["y"])]))
    params = _evidence(session)["parameters"]
    assert [p["template_id"] for p in params] == ["T1"]
    assert len(params[0]["top_values"][0][0]) == 200


# --- Richer evidence: rate vs baseline, template age, unknown templates

def test_template_age_and_unknown_templates_in_evidence(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [_reply(
        {"documentation_id": "DOC-001", "reasoning": "db"})])
    at = 100_000.0
    for state in [
        # group member first seen 5 min before the alert: listed before T1 (old, busier)
        TemplateState(template_id="T_NEW", template_text="pool exhausted", service="api",
                      level="ERROR", first_seen=at - 300, last_seen=at, event_count=2, group_id="GA"),
        TemplateState(template_id="T1", template_text="db timeout <*>", service="api",
                      level="ERROR", first_seen=at - 86_400, last_seen=at, event_count=9, group_id="GA"),
        # pending, recent, same service: kept, ERROR before the busier INFO
        TemplateState(template_id="U_INFO", template_text="retry <*>", service="api",
                      level="INFO", first_seen=at - 60, last_seen=at - 10, event_count=50,
                      group_id="UNASSIGNED_PENDING"),
        TemplateState(template_id="U_ERR", template_text="socket closed", service="api",
                      level="ERROR", first_seen=at - 120, last_seen=at, event_count=3,
                      group_id="UNASSIGNED_PENDING"),
        # dropped: no recent events, other service
        TemplateState(template_id="U_OLD", template_text="old", service="api",
                      first_seen=at - 7200, last_seen=at - 3600, group_id="UNASSIGNED_PENDING"),
        TemplateState(template_id="U_SVC", template_text="other", service="web",
                      first_seen=at, last_seen=at, group_id="UNASSIGNED_PENDING"),
    ]:
        clf.templates.upsert(state)
    clf.groups.upsert(GroupState(group_id="GA", service="api", template_ids=["T1", "T_NEW"]))
    activity = {"T_NEW": {"count_15m": 2, "count_30m": 2}, "T1": {"count_15m": 4, "count_30m": 7},
                "U_INFO": {"count_15m": 50, "count_30m": 50}, "U_ERR": {"count_15m": 3, "count_30m": 3}}
    clf.process(KEY, {**ALERT, "at": at}, [], context={"activity": activity})
    evidence = _evidence(session)
    assert [(t["id"], t["age_at_alert_minutes"], t["count_30m"]) for t in evidence["templates"]] == [
        ("T_NEW", 5.0, 2), ("T1", 1440.0, 7)]
    assert "count" not in evidence["templates"][0]
    assert evidence["group"]["count_15m"] == 6 and evidence["group"]["count_30m"] == 9
    assert [t["id"] for t in evidence["unknown_templates"]] == ["U_ERR", "U_INFO"]
    assert evidence["unknown_templates"][0]["age_at_alert_minutes"] == 2.0


def test_feature_engine_describe_rate_vs_baseline():
    from logai.config import FeatureConfig
    from logai.features.feature_engine import FeatureEngine

    engine = FeatureEngine(FeatureConfig(), origin=0.0)
    key = ("api", "GA")
    assert engine.describe(key, 10.0) is None and key not in engine.live_window_keys()
    for minute in range(10):  # 6/min baseline for 10 minutes
        for i in range(6):
            engine.update(key, minute * 60.0 + i * 10.0)
    for i in range(60):  # then 60 events in the last minute
        engine.update(key, 600.0 + i)
    rate = engine.describe(key, 660.0)
    assert rate["rate_1m_per_min"] == 60.0 and rate["count_1m"] == 60
    assert rate["baseline_1m_median_per_min"] == 6.0 and rate["baseline_minutes"] == 10
    assert rate["ratio_to_baseline"] == 10.0 and "10.0x" in rate["summary"]
    assert engine.describe(key, 900.0)["count_1m"] == 0  # read-only: nothing pruned/advanced
    assert engine.describe(key, 660.0)["count_1m"] == 60


def test_window_request_carries_rate_and_alert_time(tmp_path):
    p = _pipeline(tmp_path)
    p.incident_classifier.submit = MagicMock(return_value=True)
    p.group_registry.upsert(GroupState(group_id="G_AUTH", service="auth"))
    for i in range(5):
        p.feature_engine.update(WK, 1000.0 + i)
    p._event_clock, p._event_clock_wall = 1005.0, __import__("time").monotonic()
    p._submit_request("window", json.dumps(list(WK)), 10.0)
    _, alert, _ = p.incident_classifier.submit.call_args.args
    assert alert["count_1m"] == 5 and alert["rate"]["count_1m"] == 5
    assert alert["at"] >= 1005.0  # no scored state yet -> request time on the event clock


# --- Incident history + output language ---------------------------------------

from logai.incident.cases import add_case


def _with_case(tmp_path, clf, texts=("db timeout <*>",)):
    clf.cases_path = str(tmp_path / "incident_cases.json")
    return add_case(clf.cases_path, {"service": "api", "group_ids": ["GA"], "template_texts": list(texts),
                                     "title": "DB down", "root_cause": "primary failover"})


def test_past_incident_matched_on_service_signature(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [
        _reply({"documentation_id": "DOC-001", "reasoning": "same as before",
                "similar_case_id": "CASE-0001"}),
        _reply({"documentation_id": "DOC-001", "reasoning": "x", "similar_case_id": "CASE-0999"}),
    ])
    _with_case(tmp_path, clf, texts=("db timeout <*>", "pool exhausted"))
    context = {"signature": ["pool exhausted", "db timeout <*>", "socket closed"]}
    rec = clf.process(KEY, ALERT, [], context=context)
    past = _evidence(session)["past_incidents"]
    assert [(c["id"], c["overlap"]) for c in past] == [("CASE-0001", 1.0)]
    assert (rec["similar_case_id"], rec["similar_case_title"]) == ("CASE-0001", "DB down")
    assert "signature" not in rec  # incidents are saved from service analyses only
    assert [(r["id"], r["overlap"]) for r in rec["recalled"]] == [("CASE-0001", 1.0)]
    assert "root_cause" not in rec["recalled"][0]  # the page reads the current text
    rec = clf.process(KEY, ALERT, [], context=context)  # an id never offered is dropped
    assert rec["status"] == "done" and rec["similar_case_id"] is None


def test_unrelated_case_not_offered(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [_reply({"documentation_id": "DOC-001", "reasoning": "x"})])
    _with_case(tmp_path, clf, texts=("something else", "and more"))
    clf.process(KEY, ALERT, [])
    assert _evidence(session)["past_incidents"] == []


def test_language_instruction_and_record(tmp_path):
    from logai.incident.classifier import SYSTEM_PROMPT

    clf, session, store, _ = _classifier(tmp_path, [
        _reply({"documentation_id": "DOC-001", "reasoning": "x"}),
        _reply({"documentation_id": "DOC-001", "reasoning": "x"}),
    ])
    assert clf.process(KEY, ALERT, [], language="vi")["language"] == "vi"
    assert "in Vietnamese" in session.calls[0]["json"]["messages"][0]["content"]
    clf.process(KEY, ALERT, [])
    assert session.calls[1]["json"]["messages"][0]["content"] == SYSTEM_PROMPT
    assert store.get(STORE_KEY)["language"] == "en"


def test_window_request_language_reaches_submit(tmp_path):
    from logai.incident.requests import add_request

    p = _pipeline(tmp_path)
    p.incident_classifier.submit = MagicMock(return_value=True)
    p.group_registry.upsert(GroupState(group_id="G_AUTH", service="auth"))
    add_request(tmp_path / "analysis_requests.json", "window", json.dumps(list(WK)), 10.0,
                now=10.0, language="vi")
    p._process_analysis_requests()
    assert p.incident_classifier.submit.call_args.kwargs["language"] == "vi"


def test_pipeline_counts_activity_and_sends_window_context(tmp_path):
    p = _pipeline(tmp_path)
    p.incident_classifier.submit = MagicMock(return_value=True)
    p._assign_group = lambda parsed: GroupedEvent(parsed=parsed, group_id="G_AUTH")
    p.group_registry.upsert(GroupState(group_id="G_AUTH", service="auth"))
    for i in range(3):
        p._process_one(RawLog(timestamp=1000.0 + i, service="auth", level="ERROR",
                              event_id=f"e{i}", message="login failed for user"))
    template_id = next(iter(p.template_activity.counts(1003.0, "auth")))
    p.template_registry.upsert(TemplateState(template_id=template_id, template_text="login failed",
                                             service="auth", level="ERROR", group_id="G_AUTH"))
    p._event_clock, p._event_clock_wall = 1003.0, __import__("time").monotonic()
    p._submit_request("window", json.dumps(list(WK)), 10.0)
    context = p.incident_classifier.submit.call_args.kwargs["context"]
    assert context["activity"][template_id]["count_15m"] == 3
    # new template, no 24 h baseline yet: ERROR level stands in for "elevated"
    assert [(e["text"], e["count_15m"], e["reasons"]) for e in context["signature"]] == [
        ("login failed", 3, ["elevated", "new"])]



# --- LLM reliability: truncation/JSON retry, budget, deadline, priority, cost --

from logai.incident.classifier import JSON_REMINDER

VALID = {"documentation_id": "DOC-001", "reasoning": "db", "suggestion": None}


def _raw(content, finish_reason="stop", usage=None):
    payload = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    if usage:
        payload["usage"] = usage
    return FakeResponse(200, payload)


def test_cut_off_reply_is_asked_again_with_twice_the_budget(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [
        _raw('{"documentation_id": "DOC', "length", {"prompt_tokens": 1000, "completion_tokens": 800}),
        _raw(json.dumps(VALID), usage={"prompt_tokens": 1000, "completion_tokens": 120}),
    ])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "done" and rec["attempts"] == 2
    assert [c["json"]["max_tokens"] for c in session.calls] == [800, 1600]
    assert rec["usage"] == {"prompt_tokens": 2000, "completion_tokens": 920}
    assert isinstance(rec["duration_s"], float)


def test_cut_off_twice_fails_with_a_clear_error(tmp_path):
    clf, _, _, _ = _classifier(tmp_path, [_raw("{", "length"), _raw("{", "length")])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "failed" and "cut off at 1600 tokens" in rec["error"]


def test_invalid_json_is_asked_again_with_a_reminder(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [_reply("Sure! Here is my analysis."), _reply(VALID)])
    rec = clf.process(KEY, ALERT, [])
    assert rec["status"] == "done" and rec["attempts"] == 2
    prompts = [c["json"]["messages"][0]["content"] for c in session.calls]
    assert not prompts[0].endswith(JSON_REMINDER) and prompts[1].endswith(JSON_REMINDER)
    assert [c["json"]["max_tokens"] for c in session.calls] == [800, 800]


def test_vietnamese_gets_a_bigger_token_budget(tmp_path):
    from test_service_analysis import SVC_EVIDENCE, SVC_REPLY

    clf, session, _, _ = _classifier(tmp_path, [_reply(VALID), _reply(VALID), _reply(SVC_REPLY)])
    clf.service_store = JSONStore(tmp_path / "service_analysis.json")
    clf.process(KEY, ALERT, [], language="vi")
    clf.process(KEY, ALERT, [], language="en")
    clf.process_service("api", 1.0, SVC_EVIDENCE, {"DOC-001": "t"}, {"G1"}, "vi")
    assert [c["json"]["max_tokens"] for c in session.calls] == [1200, 800, 3000]


def test_urgent_jobs_run_before_template_triage(tmp_path):
    clf, _, _, _ = _classifier(tmp_path, [])
    clf.service_store = JSONStore(tmp_path / "service_analysis.json")
    clf.template_store = JSONStore(tmp_path / "template_triage.json")
    assert clf.submit_template("T7", 1.0, {}, {})
    assert clf.submit(KEY, ALERT, [])
    assert clf.submit_template("T8", 2.0, {}, {})
    assert clf.submit_service("api", 3.0, {}, {}, set())
    order = [clf._queue.get_nowait()[2] for _ in range(4)]  # worker not started
    assert order == [KEY, ("service", "api"), ("template", "T7"), ("template", "T8")]


class TimeoutSession:
    def __init__(self):
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        raise requests.Timeout("read timed out")


def test_read_timeout_is_retried_only_once(tmp_path):
    clf, _, _, _ = _classifier(tmp_path, [], max_retries=5)
    clf._session = session = TimeoutSession()
    rec = clf.process(KEY, ALERT, [])
    assert session.calls == 2 and rec["status"] == "failed" and "timed out" in rec["error"]


def test_job_deadline_stops_retries(tmp_path):
    clf, session, _, _ = _classifier(tmp_path, [FakeResponse(503, {})] * 3,
                                     timeout_seconds=60, job_deadline_seconds=1)
    rec = clf.process(KEY, ALERT, [])
    assert len(session.calls) == 1  # a retry could not finish before the deadline
    assert rec["status"] == "failed" and "did not answer within 1 s" in rec["error"]


def test_call_cost_reported_for_done_and_failed_jobs(tmp_path):
    reported = []
    clf, _, store, _ = _classifier(tmp_path, [
        _raw(json.dumps(VALID), usage={"prompt_tokens": 900, "completion_tokens": 100}),
        FakeResponse(400, {}),
    ])
    clf._on_call = lambda kind, meta: reported.append((kind, dict(meta)))
    clf.process(KEY, ALERT, [])
    failed = clf.process(KEY, ALERT, [])
    assert failed["status"] == "failed" and failed["attempts"] == 1 and failed["usage"] is None
    assert [kind for kind, _ in reported] == ["window", "window"]
    assert reported[0][1]["usage"] == {"prompt_tokens": 900, "completion_tokens": 100}
    assert "duration_s" in store.get(STORE_KEY)


def test_metrics_record_llm_call():
    from logai.metrics.prometheus_exporter import MetricsExporter

    fake = MagicMock()
    MetricsExporter.record_llm_call(fake, "service", {
        "duration_s": 12.5, "usage": {"prompt_tokens": 4000, "completion_tokens": 900},
        "retries": ["length"]})
    fake.logai_llm_request_duration_seconds.labels.assert_called_with(kind="service")
    fake.logai_llm_request_duration_seconds.labels().observe.assert_called_with(12.5)
    fake.logai_llm_tokens_total.labels.assert_any_call(kind="service", type="prompt")
    fake.logai_llm_tokens_total.labels.assert_any_call(kind="service", type="completion")
    fake.logai_llm_retries_total.labels.assert_called_with(kind="service", reason="length")

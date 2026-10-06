"""On-demand LLM analysis of a whole service."""
from __future__ import annotations

from logai.config import StorageConfig
from logai.incident.requests import add_request, load_requests


def test_add_and_load_requests(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "recharge", 1000.0, now=1000.0)
    assert load_requests(p) == {"recharge": 1000.0}


def test_add_request_prunes_old(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "old", 1.0, now=1.0)
    add_request(p, "new", 90_000.0, now=90_000.0)
    assert load_requests(p) == {"new": 90_000.0}


def test_load_requests_corrupt(tmp_path):
    p = tmp_path / "analysis_requests.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_requests(p) == {}
    p.write_text('{"requests": {"a": "x", "b": 2}}', encoding="utf-8")
    assert load_requests(p) == {"b": 2.0}
    p.write_text('["not", "an", "object"]', encoding="utf-8")
    assert load_requests(p) == {}
    assert load_requests(tmp_path / "missing.json") == {}


def test_storage_defaults():
    assert StorageConfig().service_analysis_file == "service_analysis.json"
    assert StorageConfig().analysis_requests_file == "analysis_requests.json"


# --- Task 2: evidence + validation --------------------------------------------

import json
import math

import numpy as np
import pytest

import test_incident_classifier as tic
from logai.alert.alert_state_machine import AlertStateMachine
from logai.config import AlertConfig
from logai.incident.service_analysis import (
    SERVICE_SYSTEM_PROMPT,
    build_service_evidence,
    validate_service_reply,
)
from logai.models import GroupState, TemplateState
from logai.storage.base import JSONStore
from logai.storage.registries import GroupRegistry, TemplateRegistry


@pytest.fixture(autouse=True)
def _use_real_numpy(real_numpy):
    global np
    np = real_numpy
    tic.np = real_numpy  # helpers imported from that module use its global


def _registries(tmp_path):
    storage = StorageConfig(base_dir=str(tmp_path))
    return GroupRegistry(storage), TemplateRegistry(storage)


def _template(templates, tid, service, level, count, gid):
    templates.upsert(TemplateState(template_id=tid, template_text=f"text {tid}", service=service,
                                   level=level, event_count=count, group_id=gid))


def _group(groups, gid, tids, centroid=None):
    groups.upsert(GroupState(group_id=gid, template_ids=tids, representative_template=f"rep {gid}"))
    if centroid is not None:
        groups.set_centroid(gid, np.array(centroid))


def _fixture(tmp_path):
    groups, templates = _registries(tmp_path)
    _template(templates, "T1", "recharge", "ERROR", 50, "G1")
    _template(templates, "T1b", "api", "ERROR", 7, "G1")
    _template(templates, "T2", "recharge", "INFO", 900, "G2")
    _template(templates, "T3", "api", "INFO", 5, "G3")
    _group(groups, "G1", ["T1", "T1b"], [1.0, 0.0])
    _group(groups, "G2", ["T2"], [0.0, 1.0])
    _group(groups, "G3", ["T3"])
    return groups, templates, tic._matcher(tmp_path, tic.CLASSIFIER_DOCS)


def test_evidence_groups_and_ranking(tmp_path):
    groups, templates, matcher = _fixture(tmp_path)
    evidence, candidates, sent = build_service_evidence(
        "recharge", groups, templates, {"G3": ("ALERTING", 0.9)}, {}, matcher)
    assert evidence["service"] == "recharge"
    assert [g["group_id"] for g in evidence["groups"]] == ["G3", "G1", "G2"]
    assert evidence["groups"][0]["alert"] == {"state": "ALERTING", "score": 0.9}
    assert evidence["groups"][1]["alert"] == {"state": "NORMAL", "score": 0.0}
    assert sent == {"G1", "G2", "G3"}
    assert set(candidates) == {"DOC-001", "DOC-002"}


def test_evidence_per_service_counts(tmp_path):
    groups, templates, matcher = _fixture(tmp_path)
    evidence, _, _ = build_service_evidence("recharge", groups, templates, {}, {}, matcher)
    g1 = next(g for g in evidence["groups"] if g["group_id"] == "G1")
    assert g1["event_count"] == 50 and g1["level"] == "ERROR"
    assert g1["templates"] == [{"id": "T1", "text": "text T1", "level": "ERROR", "count": 50}]
    assert g1["representative_template"] == "rep G1"
    assert "G3" not in {g["group_id"] for g in evidence["groups"]}


def test_evidence_caps(tmp_path):
    groups, templates = _registries(tmp_path)
    docs = [(f"DOC-{i:03d}", f"doc {i}", [math.cos(i / 10), math.sin(i / 10)]) for i in range(20)]
    for i in range(25):
        _template(templates, f"T{i}", "recharge", "ERROR", i + 1, f"G{i}")
        angle = i / 12
        _group(groups, f"G{i}", [f"T{i}"], [math.cos(angle), math.sin(angle)])
    evidence, candidates, sent = build_service_evidence(
        "recharge", groups, templates, {}, {}, tic._matcher(tmp_path, docs))
    assert len(evidence["groups"]) == 20 and len(sent) == 20
    ids = [c["id"] for c in evidence["candidates"]]
    assert len(ids) == 15 and len(set(ids)) == 15 and set(ids) == set(candidates)
    sims = [c["similarity"] for c in evidence["candidates"]]
    assert sims == sorted(sims, reverse=True)
    assert all(len(c["text"]) <= 1000 for c in evidence["candidates"])


def test_evidence_without_centroids(tmp_path):
    groups, templates = _registries(tmp_path)
    _template(templates, "T1", "recharge", "ERROR", 5, "G1")
    _group(groups, "G1", ["T1"])
    evidence, candidates, sent = build_service_evidence(
        "recharge", groups, templates, {}, {}, tic._matcher(tmp_path, tic.CLASSIFIER_DOCS))
    assert evidence["candidates"] == [] and candidates == {}
    assert [g["group_id"] for g in evidence["groups"]] == ["G1"] and sent == {"G1"}


def test_evidence_parameters_limited(tmp_path):
    groups, templates = _registries(tmp_path)
    for tid, count in [("T1", 40), ("T2", 30), ("T3", 20), ("T4", 10)]:
        _template(templates, tid, "recharge", "ERROR", count, "G1")
    _group(groups, "G1", ["T1", "T2", "T3", "T4"], [1.0, 0.0])
    evidence, _, _ = build_service_evidence(
        "recharge", groups, templates, {},
        {"G1": [("T4", ["x"]), ("T1", ["<*>", "y" * 300])]},
        tic._matcher(tmp_path, tic.CLASSIFIER_DOCS))
    params = evidence["groups"][0]["parameters"]
    assert [p["template_id"] for p in params] == ["T1"]
    assert params[0]["slot"] == 1 and len(params[0]["top_values"][0][0]) == 200


def test_states_for_service(tmp_path):
    store = JSONStore(tmp_path / "anomaly_state.json")
    store.bulk_set({
        json.dumps(["recharge", "G3"]): {"group_id": ["recharge", "G3"], "timestamp": 1.0,
                                          "alert_state": "ALERTING", "anomaly_score": 0.9},
        json.dumps(["api", "G1"]): {"group_id": ["api", "G1"], "timestamp": 1.0,
                                     "alert_state": "NORMAL", "anomaly_score": 0.1},
        "LEGACY": {"group_id": "LEGACY", "timestamp": 1.0, "alert_state": "ALERTING"},
    })
    sm = AlertStateMachine(AlertConfig(), store)
    assert sm.states_for_service("recharge") == {"G3": ("ALERTING", 0.9)}


CANDS = {"DOC-001": "title DOC-001"}
SUG = {"title": "Restart", "text": "do it", "error_code": None}


def test_validate_health_and_summary():
    out = validate_service_reply({"health": "meh", "summary": "ok", "issues": []}, CANDS, {"G1"})
    assert out["health"] == "unknown" and out["summary"] == "ok" and out["issues"] == []
    with pytest.raises(ValueError):
        validate_service_reply({"health": "healthy", "summary": ""}, CANDS, {"G1"})
    long = validate_service_reply({"health": "critical", "summary": "s" * 5000}, CANDS, set())
    assert long["health"] == "critical" and len(long["summary"]) == 4000


def test_validate_issue_rules():
    issues = [
        {"title": "db", "group_ids": ["G1", "GX"], "documentation_id": "DOC-001", "reasoning": "r"},
        {"title": "gw", "group_ids": ["G1"], "documentation_id": "DOC-999", "reasoning": "r", "suggestion": SUG},
        {"title": "none", "group_ids": ["G1"], "documentation_id": "DOC-999", "reasoning": "r"},
    ] + [{"title": f"i{i}", "group_ids": [], "documentation_id": "DOC-001"} for i in range(12)]
    out = validate_service_reply({"health": "degraded", "summary": "s", "issues": issues}, CANDS, {"G1"})
    assert len(out["issues"]) == 10
    first, second = out["issues"][0], out["issues"][1]
    assert first["group_ids"] == ["G1"] and first["document_title"] == "title DOC-001"
    assert first["suggestion"] is None
    assert second["documentation_id"] is None and second["suggestion"]["error_code"] == ""
    assert "none" not in [i["title"] for i in out["issues"]]


def test_validate_malformed_issues():
    assert validate_service_reply({"health": "healthy", "summary": "s", "issues": "x"}, CANDS, set())["issues"] == []
    out = validate_service_reply({"health": "healthy", "summary": "s", "issues": [
        1, None, {"title": "ok", "documentation_id": "DOC-001", "group_ids": "G1"}]}, CANDS, {"G1"})
    assert [i["title"] for i in out["issues"]] == ["ok"] and out["issues"][0]["group_ids"] == []


def test_service_prompt_mentions_shape():
    assert '"health"' in SERVICE_SYSTEM_PROMPT and '"issues"' in SERVICE_SYSTEM_PROMPT

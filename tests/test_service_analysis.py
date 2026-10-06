"""On-demand LLM analysis of a whole service."""
from __future__ import annotations

from logai.config import StorageConfig
from logai.incident.requests import add_request


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


# --- Task 3: classifier service job type ---------------------------------------

import time

from logai.config import LLMConfig
from logai.incident.classifier import IncidentClassifier

SVC_EVIDENCE = {"service": "recharge", "groups": [], "candidates": []}
SVC_REPLY = {"health": "degraded", "summary": "s", "issues": [
    {"title": "t", "group_ids": ["G1"], "documentation_id": "DOC-001", "reasoning": "r"}]}


def _svc_classifier(tmp_path, responses, preseed=None):
    groups, templates = _registries(tmp_path)
    service_store = JSONStore(tmp_path / "service_analysis.json")
    if preseed:
        service_store.bulk_set(preseed)
        service_store = JSONStore(tmp_path / "service_analysis.json")
    session = tic.FakeSession(responses)
    results = []
    clf = IncidentClassifier(
        LLMConfig(endpoint="http://llm", model="m", retry_backoff_seconds=0),
        tic._matcher(tmp_path, tic.CLASSIFIER_DOCS), groups, templates,
        JSONStore(tmp_path / "incident_analysis.json"),
        session=session, on_result=results.append, service_store=service_store,
    )
    return clf, session, service_store, results


def test_process_service_done(tmp_path):
    clf, session, store, results = _svc_classifier(tmp_path, [tic._reply(SVC_REPLY)])
    rec = clf.process_service("recharge", 5.0, SVC_EVIDENCE, {"DOC-001": "title DOC-001"}, {"G1"})
    assert rec["status"] == "done" and rec["health"] == "degraded" and rec["requested_at"] == 5.0
    assert rec["issues"][0]["document_title"] == "title DOC-001" and rec["model"] == "m"
    assert store.get("recharge")["status"] == "done"
    body = session.calls[0]["json"]
    assert body["max_tokens"] == 2000
    assert body["messages"][0]["content"] == SERVICE_SYSTEM_PROMPT
    assert json.loads(body["messages"][1]["content"]) == SVC_EVIDENCE
    assert results == ["service_done"]


def test_process_service_failed_keeps_requested_at(tmp_path):
    clf, _, store, results = _svc_classifier(tmp_path, [tic._reply("garbage")])
    rec = clf.process_service("recharge", 5.0, SVC_EVIDENCE, {}, set())
    assert rec["status"] == "failed" and rec["requested_at"] == 5.0 and rec["error"]
    assert store.get("recharge")["status"] == "failed"
    assert results == ["service_failed"]


def test_submit_service_pending_and_dedupe(tmp_path):
    clf, _, store, _ = _svc_classifier(tmp_path, [])
    assert clf.submit_service("recharge", 5.0, SVC_EVIDENCE, {}, set()) is True
    record = store.get("recharge")
    assert record["status"] == "pending" and record["requested_at"] == 5.0
    assert clf.submit_service("recharge", 6.0, SVC_EVIDENCE, {}, set()) is False
    clf.service_store = None
    assert clf.submit_service("other", 6.0, SVC_EVIDENCE, {}, set()) is False


def test_service_restart_pending_to_failed(tmp_path):
    _, _, store, _ = _svc_classifier(tmp_path, [], preseed={
        "recharge": {"status": "pending", "service": "recharge", "requested_at": 7.0, "queued_at": 7.0}})
    record = JSONStore(tmp_path / "service_analysis.json").get("recharge")
    assert record["status"] == "failed" and record["requested_at"] == 7.0
    assert "restart" in record["error"]


def test_worker_runs_both_job_types(tmp_path):
    window_reply = {"documentation_id": None, "reasoning": "x", "suggestion": tic.SUGGESTION}
    clf, _, store, _ = _svc_classifier(tmp_path, [tic._reply(window_reply), tic._reply(SVC_REPLY)])
    clf.start()
    try:
        assert clf.submit(("recharge", "G1"), tic.ALERT, [])
        assert clf.submit_service("recharge", 5.0, SVC_EVIDENCE, {"DOC-001": "t"}, {"G1"})
        incident = JSONStore(tmp_path / "incident_analysis.json")
        deadline = time.time() + 3
        while time.time() < deadline:
            done = (JSONStore(tmp_path / "incident_analysis.json").get(json.dumps(["recharge", "G1"])) or {}).get("status")
            svc = (JSONStore(tmp_path / "service_analysis.json").get("recharge") or {}).get("status")
            if done not in (None, "pending") and svc not in (None, "pending"):
                break
            time.sleep(0.05)
        assert done == "done" and svc == "done"
    finally:
        clf.stop()


# --- Task 4: engine pickup + heartbeat ------------------------------------------

from unittest.mock import MagicMock, patch


def _engine(tmp_path, record=None, endpoint="http://llm"):
    if record is not None:
        JSONStore(tmp_path / "service_analysis.json").set("recharge", record)
    p = tic._pipeline(tmp_path, endpoint=endpoint)
    p.template_registry.upsert(TemplateState(
        template_id="T1", template_text="x", service="recharge", event_count=1, group_id="G1"))
    if p.incident_classifier is not None:
        p.incident_classifier.submit_service = MagicMock(return_value=True)
    return p


def _request(tmp_path, service, requested_at):
    add_request(tmp_path / "analysis_requests.json", "service", service, requested_at, now=requested_at)


def test_request_submitted_once(tmp_path):
    p = _engine(tmp_path)
    _request(tmp_path, "recharge", 10.0)
    p._process_analysis_requests()
    p._process_analysis_requests()
    submit = p.incident_classifier.submit_service
    assert submit.call_count == 1
    service, requested_at, evidence, candidates, sent = submit.call_args.args
    assert (service, requested_at, evidence["service"]) == ("recharge", 10.0, "recharge")


def test_handled_across_restart(tmp_path):
    p = _engine(tmp_path, record={"status": "done", "service": "recharge", "requested_at": 10.0})
    _request(tmp_path, "recharge", 10.0)
    p._process_analysis_requests()
    p.incident_classifier.submit_service.assert_not_called()


def test_new_request_after_done_runs_again(tmp_path):
    p = _engine(tmp_path, record={"status": "done", "service": "recharge", "requested_at": 10.0})
    _request(tmp_path, "recharge", 20.0)
    p._process_analysis_requests()
    p._process_analysis_requests()
    assert p.incident_classifier.submit_service.call_count == 1


def test_unknown_service_marked_handled(tmp_path):
    p = _engine(tmp_path)
    _request(tmp_path, "ghost", 10.0)
    p._process_analysis_requests()
    p._process_analysis_requests()
    p.incident_classifier.submit_service.assert_not_called()
    assert p._handled_requests["service:ghost"] == 10.0


def test_active_service_retried(tmp_path):
    p = _engine(tmp_path)
    p.incident_classifier.submit_service = MagicMock(side_effect=[False, True])
    _request(tmp_path, "recharge", 10.0)
    for _ in range(3):
        p._process_analysis_requests()
    assert p.incident_classifier.submit_service.call_count == 2


def test_heartbeat_has_llm_enabled(tmp_path):
    p = _engine(tmp_path)
    p._last_grouping_heartbeat = 0.0
    p._heartbeat_grouping()
    assert p.grouping_store.load_status()["runtime"]["llm_enabled"] is True
    off = _engine(tmp_path / "off", endpoint="")
    off._last_grouping_heartbeat = 0.0
    off._heartbeat_grouping()
    assert off.grouping_store.load_status()["runtime"]["llm_enabled"] is False
    off._process_analysis_requests()  # disabled: no classifier, no error


def test_pickup_never_raises(tmp_path):
    p = _engine(tmp_path)
    _request(tmp_path, "recharge", 10.0)
    with patch("logai.realtime.realtime_pipeline.build_service_evidence", side_effect=RuntimeError("boom")):
        assert p._process_analysis_requests() is None
    p.incident_classifier.submit_service.assert_not_called()


# --- Final review fixes ---------------------------------------------------------

import os as _os


def test_unknown_service_writes_failed_record(tmp_path):
    p = _engine(tmp_path)
    _request(tmp_path, "ghost", 10.0)
    p._process_analysis_requests()
    record = p.incident_classifier.service_store.get("ghost")
    assert record["status"] == "failed" and record["requested_at"] == 10.0


def test_one_bad_service_does_not_block_others(tmp_path):
    p = _engine(tmp_path)
    p.template_registry.upsert(TemplateState(
        template_id="T2", template_text="y", service="billing", event_count=1, group_id="G2"))
    add_request(tmp_path / "analysis_requests.json", "service", "recharge", 10.0, now=10.0)
    add_request(tmp_path / "analysis_requests.json", "service", "billing", 11.0, now=11.0)
    real = build_service_evidence

    def flaky(service, *args):
        if service == "recharge":
            raise RuntimeError("boom")
        return real(service, *args)

    with patch("logai.realtime.realtime_pipeline.build_service_evidence", side_effect=flaky):
        p._process_analysis_requests()
    assert p.incident_classifier.submit_service.call_args.args[0] == "billing"
    record = p.incident_classifier.service_store.get("recharge")
    assert record["status"] == "failed" and record["requested_at"] == 10.0 and "boom" in record["error"]


def test_request_written_within_same_mtime_is_seen(tmp_path):
    p = _engine(tmp_path)
    path = tmp_path / "analysis_requests.json"
    add_request(path, "service", "ghost", 10.0, now=10.0)
    p._process_analysis_requests()
    stat = _os.stat(path)
    add_request(path, "service", "recharge", 11.0, now=11.0)
    _os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # coarse-timestamp filesystem
    p._process_analysis_requests()
    assert p.incident_classifier.submit_service.call_args.args[0] == "recharge"

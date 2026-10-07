"""AI Insights: on-demand analyses (window, service, template) and deletes."""
from __future__ import annotations

import json

from logai.incident.requests import add_request, load_requests, parse_key, request_key


def test_request_key_round_trip():
    key = request_key("window", json.dumps(["pay \"vn\"", "G1"]))
    assert parse_key(key) == ("window", json.dumps(["pay \"vn\"", "G1"]))
    assert parse_key("service:a:b") == ("service", "a:b")
    assert parse_key("bogus") is None


def test_add_and_load_v2(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "template", "T00042", 100.0, now=100.0)
    add_request(p, "service", "recharge", 101.0, action="delete", now=101.0)
    assert load_requests(p) == {
        "template:T00042": ("analyze", 100.0, "en"),
        "service:recharge": ("delete", 101.0, "en"),
    }
    raw = json.loads(p.read_text())
    assert raw["schema_version"] == 2


def test_latest_action_wins_and_prune(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "service", "old", 1.0, now=1.0)
    add_request(p, "service", "x", 90_000.0, now=90_000.0)
    add_request(p, "service", "x", 90_001.0, action="delete", now=90_001.0)
    assert load_requests(p) == {"service:x": ("delete", 90_001.0, "en")}


def test_v1_file_reads_as_service_analyze(tmp_path):
    p = tmp_path / "analysis_requests.json"
    p.write_text(json.dumps({"schema_version": 1, "requests": {"recharge": 5.0}}))
    assert load_requests(p) == {"service:recharge": ("analyze", 5.0, "en")}


def test_corrupt_or_bad_entries(tmp_path):
    p = tmp_path / "analysis_requests.json"
    p.write_text("{nope")
    assert load_requests(p) == {}
    p.write_text(json.dumps({"schema_version": 2, "requests": {
        "service:a": {"action": "explode", "at": 1.0},
        "service:b": {"action": "analyze", "at": "x"},
        "nokind": {"action": "analyze", "at": 1.0},
        "template:T1": {"action": "analyze", "at": 2.0},
    }}))
    assert load_requests(p) == {"template:T1": ("analyze", 2.0, "en")}


# --- template triage evidence + validation ------------------------------------

import numpy as np
import pytest

from logai.config import StorageConfig
from logai.incident.template_triage import (
    TEMPLATE_SYSTEM_PROMPT,
    build_template_evidence,
    validate_template_reply,
)
from logai.models import GroupState, TemplateState
from logai.storage.registries import GroupRegistry, TemplateRegistry


@pytest.fixture(autouse=True)
def _use_real_numpy(real_numpy):
    global np
    np = real_numpy


def _regs(tmp_path):
    s = StorageConfig(base_dir=str(tmp_path))
    return GroupRegistry(s), TemplateRegistry(s)


def _seed(tmp_path, n_groups=7):
    groups, templates = _regs(tmp_path)
    templates.upsert(TemplateState(template_id="T9", template_text="payment gateway <*> refused",
                                   service="recharge", level="ERROR", event_count=4,
                                   group_id="UNASSIGNED_PENDING"))
    templates.set_embedding("T9", np.array([1.0, 0.0]))
    for i in range(n_groups):
        angle = i * 0.2
        groups.upsert(GroupState(group_id=f"G{i}", service="recharge", template_ids=[],
                                 representative_template=f"rep {i}", event_count=10 * i))
        groups.set_centroid(f"G{i}", np.array([np.cos(angle), np.sin(angle)]))
    return groups, templates


def test_template_evidence_top5_groups(tmp_path):
    groups, templates = _seed(tmp_path)
    evidence, candidates = build_template_evidence("T9", templates, groups)
    assert evidence["template"]["text"] == "payment gateway <*> refused"
    assert evidence["template"]["level"] == "ERROR"
    ids = [g["group_id"] for g in evidence["candidate_groups"]]
    assert ids == ["G0", "G1", "G2", "G3", "G4"] and set(candidates) == set(ids)
    sims = [g["similarity"] for g in evidence["candidate_groups"]]
    assert sims == sorted(sims, reverse=True)


def test_template_evidence_without_embedding(tmp_path):
    groups, templates = _seed(tmp_path)
    templates.upsert(TemplateState(template_id="T10", template_text="x", service="s",
                                   group_id="UNASSIGNED_PENDING"))
    evidence, candidates = build_template_evidence("T10", templates, groups)
    assert evidence["candidate_groups"] == [] and candidates == {}
    with pytest.raises(LookupError):
        build_template_evidence("T404", templates, groups)


def test_validate_template_reply():
    cands = {"G1": "rep 1"}
    out = validate_template_reply({"verdict": "suspicious", "reasoning": "refused payments",
                                   "suggested_group_id": "G1", "confidence": 1.5}, cands)
    assert out == {"verdict": "suspicious", "reasoning": "refused payments", "confidence": 1.0,
                   "suggested_group_id": "G1", "suggested_group_rep": "rep 1"}
    out = validate_template_reply({"verdict": "odd", "reasoning": "r", "suggested_group_id": "G999"}, cands)
    assert out["verdict"] == "unsure" and out["suggested_group_id"] is None
    assert validate_template_reply({"verdict": "benign", "reasoning": "r", "suggested_group_id": "new"},
                                   cands)["suggested_group_id"] == "new"
    with pytest.raises(ValueError):
        validate_template_reply({"verdict": "benign", "reasoning": ""}, cands)
    assert '"verdict"' in TEMPLATE_SYSTEM_PROMPT


# --- classifier: template jobs, requested_at on windows, delete ------------------

import time

import test_incident_classifier as tic
from logai.config import LLMConfig
from logai.incident.classifier import IncidentClassifier
from logai.storage.base import JSONStore

TRIAGE_REPLY = {"verdict": "suspicious", "confidence": 0.8, "reasoning": "refused payments",
                "suggested_group_id": "G0"}


def _clf(tmp_path, responses, preseed_templates=None):
    groups, templates = _seed(tmp_path)
    tic.np = np
    template_store = JSONStore(tmp_path / "template_triage.json")
    if preseed_templates:
        template_store.bulk_set(preseed_templates)
        template_store = JSONStore(tmp_path / "template_triage.json")
    session = tic.FakeSession(responses)
    results = []
    clf = IncidentClassifier(
        LLMConfig(endpoint="http://llm", model="m", retry_backoff_seconds=0),
        tic._matcher(tmp_path, tic.CLASSIFIER_DOCS), groups, templates,
        JSONStore(tmp_path / "incident_analysis.json"), session=session,
        on_result=results.append, service_store=JSONStore(tmp_path / "service_analysis.json"),
        template_store=template_store,
    )
    return clf, session, results


def test_process_template_done(tmp_path):
    clf, session, results = _clf(tmp_path, [tic._reply(TRIAGE_REPLY)])
    evidence, candidates = build_template_evidence("T9", clf.templates, clf.groups)
    rec = clf.process_template("T9", 5.0, evidence, candidates)
    assert rec["status"] == "done" and rec["verdict"] == "suspicious"
    assert rec["suggested_group_id"] == "G0" and rec["requested_at"] == 5.0 and rec["model"] == "m"
    assert clf.template_store.get("T9")["verdict"] == "suspicious"
    assert session.calls[0]["json"]["messages"][0]["content"] == TEMPLATE_SYSTEM_PROMPT
    assert results == ["template_done"]


def test_process_template_failed(tmp_path):
    clf, _, results = _clf(tmp_path, [tic._reply("nope")])
    rec = clf.process_template("T9", 5.0, {}, {})
    assert rec["status"] == "failed" and rec["requested_at"] == 5.0 and results == ["template_failed"]


def test_submit_template_pending_dedupe_and_restart(tmp_path):
    clf, _, _ = _clf(tmp_path, [])
    assert clf.submit_template("T9", 5.0, {}, {}) is True
    assert clf.template_store.get("T9")["status"] == "pending"
    assert clf.submit_template("T9", 6.0, {}, {}) is False
    clf2, _, _ = _clf(tmp_path / "b", [], preseed_templates={
        "T9": {"status": "pending", "template_id": "T9", "requested_at": 7.0}})
    rec = clf2.template_store.get("T9")
    assert rec["status"] == "failed" and rec["requested_at"] == 7.0


def test_window_record_keeps_requested_at(tmp_path):
    clf, _, _ = _clf(tmp_path, [tic._reply(
        {"documentation_id": "DOC-001", "reasoning": "r", "suggestion": None})])
    assert clf.submit(("recharge", "G0"), tic.ALERT, [], requested_at=9.0)
    assert clf.store.get(json.dumps(["recharge", "G0"]))["requested_at"] == 9.0
    rec = clf.process(("recharge", "G0"), tic.ALERT, [], requested_at=9.0)
    assert rec["requested_at"] == 9.0


def test_delete_record_all_kinds(tmp_path):
    clf, _, _ = _clf(tmp_path, [])
    clf.store.set(json.dumps(["recharge", "G0"]), {"status": "done"})
    clf.service_store.set("recharge", {"status": "done"})
    clf.template_store.set("T9", {"status": "done"})
    assert clf.delete_record("window", json.dumps(["recharge", "G0"])) is True
    assert clf.delete_record("service", "recharge") is True
    assert clf.delete_record("template", "T9") is True
    assert clf.delete_record("template", "T9") is False
    assert clf.store.get(json.dumps(["recharge", "G0"])) is None


def test_delete_while_in_flight_is_not_resurrected(tmp_path):
    clf, _, _ = _clf(tmp_path, [tic._reply(TRIAGE_REPLY)])
    assert clf.submit_template("T9", 5.0, {}, {"G0": "rep 0"})
    clf.delete_record("template", "T9")
    clf.process_template("T9", 5.0, {}, {"G0": "rep 0"})
    assert clf.template_store.get("T9") is None


def test_worker_runs_template_jobs(tmp_path):
    clf, _, _ = _clf(tmp_path, [tic._reply(TRIAGE_REPLY)])
    clf.start()
    try:
        assert clf.submit_template("T9", 5.0, {}, {"G0": "rep 0"})
        deadline = time.time() + 3
        while time.time() < deadline and (clf.template_store.get("T9") or {}).get("status") == "pending":
            time.sleep(0.05)
        assert clf.template_store.get("T9")["status"] == "done"
        time.sleep(0.1)
        assert ("template", "T9") not in clf._active
    finally:
        clf.stop()


# --- pipeline: no auto analysis; requests for every kind ---------------------------

from unittest.mock import MagicMock, patch

from logai.models import AnomalyResult


def _engine(tmp_path, endpoint="http://llm"):
    p = tic._pipeline(tmp_path, endpoint=endpoint)
    tic.np = np
    for name in ("submit", "submit_service", "submit_template"):
        setattr(p.incident_classifier, name, MagicMock(return_value=True))
    return p


def _req(tmp_path, kind, target, at, action="analyze"):
    add_request(tmp_path / "analysis_requests.json", kind, target, at, action=action, now=at)


def test_alerts_never_trigger_llm_automatically(tmp_path):
    p = _engine(tmp_path)
    results = [AnomalyResult(group_id=tic.WK, timestamp=1000.0 + i, anomaly_score=0.99,
                             anomaly=True, count_1m=500) for i in range(10)]
    p.anomaly_model.predict_batch = MagicMock(return_value=results)
    p._pending_predictions = [(tic.WK, MagicMock()) for _ in results]
    p._flush_predictions()
    assert p.alert_sm._load(tic.WK).alert_state == "ALERTING"
    p.incident_classifier.submit.assert_not_called()


def test_window_analyze_request(tmp_path):
    p = _engine(tmp_path)
    p.group_registry.upsert(GroupState(group_id="G_AUTH", service="auth"))
    _req(tmp_path, "window", json.dumps(list(tic.WK)), 10.0)
    p._control_tick()
    p._control_tick()
    submit = p.incident_classifier.submit
    assert submit.call_count == 1
    key, alert, params = submit.call_args.args
    assert key == tic.WK and alert["state"] == "NORMAL" and submit.call_args.kwargs["requested_at"] == 10.0


def test_template_analyze_request_and_unknown_template(tmp_path):
    p = _engine(tmp_path)
    p.template_registry.upsert(TemplateState(template_id="T9", template_text="x", service="auth",
                                             group_id="UNASSIGNED_PENDING"))
    _req(tmp_path, "template", "T9", 10.0)
    _req(tmp_path, "template", "T404", 11.0)
    p._control_tick()
    assert p.incident_classifier.submit_template.call_args.args[:2] == ("T9", 10.0)
    failed = p.incident_classifier.template_store.get("T404")
    assert failed["status"] == "failed" and failed["requested_at"] == 11.0


def test_delete_request_removes_record_once(tmp_path):
    p = _engine(tmp_path)
    p.incident_classifier.template_store.set("T9", {"status": "done", "requested_at": 5.0})
    p.incident_classifier.delete_record = MagicMock(wraps=p.incident_classifier.delete_record)
    _req(tmp_path, "template", "T9", 20.0, action="delete")
    p._control_tick()
    p._control_tick()
    assert p.incident_classifier.template_store.get("T9") is None
    assert p.incident_classifier.delete_record.call_count == 1


def test_requests_ignored_while_llm_disabled_but_deletes_apply(tmp_path):
    p = _engine(tmp_path, endpoint="")
    p.incident_classifier.service_store.set("auth", {"status": "done", "requested_at": 1.0})
    _req(tmp_path, "service", "auth", 10.0, action="delete")
    _req(tmp_path, "template", "T9", 10.0)
    p._control_tick()
    assert p.incident_classifier.service_store.get("auth") is None
    p.incident_classifier.submit_template.assert_not_called()

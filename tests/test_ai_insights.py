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
from logai.incident.classifier import BRIEF_PROMPT, IncidentClassifier
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
    assert session.calls[0]["json"]["messages"][0]["content"] == TEMPLATE_SYSTEM_PROMPT + BRIEF_PROMPT
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


def test_history_keeps_every_finished_analysis(tmp_path):
    from logai.web.app import create_app

    clf, _, _ = _clf(tmp_path, [
        tic._reply(TRIAGE_REPLY),
        tic._reply({**TRIAGE_REPLY, "verdict": "benign"}),
        tic._reply("nope"),
    ])
    clf.history_path = str(tmp_path / "analysis_history.jsonl")
    for at in (1.0, 2.0, 3.0):  # done, done, failed
        clf.process_template("T9", at, {}, {"G0": "rep"})
    assert clf.template_store.get("T9")["status"] == "failed"
    with open(clf.history_path, "a", encoding="utf-8") as stream:
        stream.write('{"torn line\n')  # a crash mid-append must not break reads

    client = create_app(data_dir=str(tmp_path)).test_client()
    body = client.get("/api/insights/history").get_json()
    # Failed analyses are kept too; newest first; one row per analysis.
    assert [row["headline"].split(" ")[0] for row in body["items"]] == ["Failed", "Benign", "Suspicious"]
    assert body["total"] == body["all_total"] == 3
    assert {row["kind"] for row in body["items"]} == {"template"}
    newest = body["items"][0]
    detail = client.get(f"/api/insights/history/{newest['n']}").get_json()
    assert detail["record"]["status"] == "failed" and detail["n"] == newest["n"]
    assert client.get("/api/insights/history/999").status_code == 404
    assert client.get("/api/insights/history?kind=template&id=T8").get_json()["items"] == []
    assert client.get("/api/insights/history?kind=window").get_json()["items"] == []
    assert client.get("/api/insights/history?kind=bogus").status_code == 400


def test_history_entries_can_be_deleted(tmp_path):
    from logai.incident.history import append_history, backfill_history
    from logai.web.app import create_app

    path = tmp_path / "analysis_history.jsonl"
    rows = [  # line: (kind, id, status)
        ("service", "api", "failed"), ("service", "api", "done"),
        ("service", "web", "failed"), ("template", "T1", "failed"), ("service", "api", "done"),
    ]
    for at, (kind, target_id, status) in enumerate(rows):
        append_history(path, kind, target_id, {"status": status, "analyzed_at": float(at)}, at=float(at))
    client = create_app(data_dir=str(tmp_path)).test_client()
    history = lambda query="": client.get(f"/api/insights/history{query}").get_json()
    delete = lambda body: client.post("/api/insights/history/delete", json=body)

    assert history()["failed_total"] == 3
    assert delete({"n": 4}).get_json() == {"deleted": 1}
    assert delete({"n": 4}).status_code == 404  # already deleted
    assert delete({"n": 99}).status_code == 404
    assert client.get("/api/insights/history/4").status_code == 404
    # Other entries keep their numbers.
    assert client.get("/api/insights/history/3").get_json()["id"] == "T1"
    assert sorted(row["n"] for row in history()["items"]) == [0, 1, 2, 3]

    # Bulk: only failed rows matching the filters go.
    assert delete({"failed": True, "kind": "service", "service": "api"}).get_json() == {"deleted": 1}
    body = history()
    assert sorted(row["n"] for row in body["items"]) == [1, 2, 3] and body["all_total"] == 3
    assert history("?kind=service&service=api")["failed_total"] == 0
    assert delete({"failed": True}).get_json() == {"deleted": 2}
    assert [row["n"] for row in history()["items"]] == [1]

    for bad in ({}, {"n": "1"}, {"n": True}, {"failed": "yes"}):
        assert delete(bad).status_code == 400
    assert delete({"failed": True, "kind": "bogus"}).status_code == 400

    # The engine's backfill on start sees the kept lines, so nothing comes back.
    latest = {"api": {"status": "done", "analyzed_at": 4.0}, "web": {"status": "failed", "analyzed_at": 2.0}}
    assert backfill_history(path, {"service": latest}) == 0
    assert history()["all_total"] == 1


def test_error_lines_and_correlated_alerts_reach_window_and_service_evidence(tmp_path):
    from logai.alert.alert_state_machine import group_id_key
    from logai.models import GroupedEvent, RawLog

    p = _engine(tmp_path)
    p._assign_group = lambda parsed: GroupedEvent(parsed=parsed, group_id="G_AUTH")
    for i, level in enumerate(("ERROR", "INFO")):
        p._process_one(RawLog(
            timestamp=1000.0 + i, service="auth", level=level, event_id=f"e{i}",
            message=f"Login failed java.net.ConnectException: refused password=s3cret try={i}",
            metadata={"host_name": "auth-01"},
        ))
    template_id = next(iter(p.error_samples._recent))[1]
    p.template_registry.upsert(TemplateState(template_id=template_id, template_text="login failed",
                                             service="auth", group_id="G_AUTH", first_seen=1000.0))
    p.group_registry.upsert(GroupState(group_id="G_AUTH", service="auth", template_ids=[template_id]))
    p.group_registry.upsert(GroupState(group_id="G_API", service="api", representative_template="api down"))
    p.alert_sm._store.set(group_id_key(("api", "G_API")), {
        "group_id": ["api", "G_API"], "timestamp": 1000.0, "anomaly_score": 0.9,
        "alert_state": "ALERTING"})
    p.alert_sm._non_normal.add(("api", "G_API"))
    p._event_clock, p._event_clock_wall = 1010.0, time.monotonic()

    _req(tmp_path, "window", json.dumps(list(tic.WK)), 10.0)
    _req(tmp_path, "service", "auth", 11.0)
    p._control_tick()

    context = p.incident_classifier.submit.call_args.kwargs["context"]
    errors = context["errors"]
    assert errors["summary"]["error_templates"] == 1
    signature = errors["signatures"][0]
    assert signature["exception"] == "java.net.ConnectException"
    assert signature["samples"] == [signature["samples"][0]] and "s3cret" not in signature["samples"][0]
    assert errors["top_values"]["host"] == [["auth-01", 1]]
    assert sum(errors["timeline_per_min"]) == 2  # the template's events, all levels
    assert context["correlated_alerts"][0]["group_id"] == "G_API"

    evidence = p.incident_classifier.submit_service.call_args.args[2]
    assert list(evidence)[0] == "errors"
    assert evidence["correlated_alerts"][0]["service"] == "api"


def test_results_older_than_the_history_file_are_backfilled_once(tmp_path):
    """Analyses made before analysis_history.jsonl existed must not vanish
    from the history when they are re-analyzed and overwritten."""
    from logai.incident.history import read_history

    window_key = json.dumps(["recharge", "G0000"])
    JSONStore(tmp_path / "incident_analysis.json").bulk_set({
        window_key: {"status": "done", "analyzed_at": 5.0, "documentation_id": "DOC-1"},
        json.dumps(["recharge", "G0001"]): {"status": "failed", "analyzed_at": 6.0},
        json.dumps(["recharge", "G0002"]): {"status": "pending", "requested_at": 6.5},
    })
    JSONStore(tmp_path / "service_analysis.json").bulk_set(
        {"recharge": {"status": "done", "analyzed_at": 7.0, "summary": "ok"}})
    history = tmp_path / "analysis_history.jsonl"

    def build():
        clf, _, _ = _clf(tmp_path, [], preseed_templates={
            "T9": {"status": "done", "analyzed_at": 8.0, "verdict": "benign"}})
        return clf

    for _ in range(2):  # a second engine start must not duplicate them
        clf = build()
        clf.history_path = str(history)
        clf.backfill_history()
    logged = [(e["kind"], e["id"], e["record"]["status"], e["at"]) for _, e in read_history(history)]
    assert len(logged) == 5  # nothing duplicated by the second start
    assert {(kind, at) for kind, _, _, at in logged if at < 100} == {
        ("window", 5.0), ("window", 6.0), ("service", 7.0), ("template", 8.0)}
    # The pending one was marked failed at start-up (interrupted), so it is kept too.
    assert ("window", json.dumps(["recharge", "G0002"]), "failed") in {e[:3] for e in logged}

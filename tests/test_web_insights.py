"""Web API for AI Insights: list, request, delete; documents assigned on save."""
from __future__ import annotations

import json
import time

from test_web_grouping_api import _client

from logai.incident.requests import add_request, load_requests
from logai.storage.grouping import GroupingOverrideStore

GA = json.dumps(["api", "GA"])


def _beat(base, age=0.0, **runtime):
    runtime.setdefault("llm_enabled", True)
    runtime.setdefault("llm_status", "ok")
    GroupingOverrideStore(base / "grouping_overrides.json", base / "grouping_status.json") \
        .update_heartbeat(time.time() - age, runtime=runtime)


def _write(base, name, data):
    (base / name).write_text(json.dumps(data), encoding="utf-8")


def test_insights_lists_windows_services_and_unknown_templates():
    temporary, base, client = _client()
    try:
        _beat(base)
        _write(base, "anomaly_state.json", {GA: {"group_id": ["api", "GA"], "alert_state": "ALERTING",
                                                 "anomaly_score": 0.8}})
        _write(base, "incident_analysis.json", {GA: {"status": "done", "documentation_id": "DOC-001",
                                                     "requested_at": 5.0}})
        _write(base, "template_triage.json", {"T3": {"status": "done", "verdict": "benign",
                                                    "requested_at": 5.0}})
        body = client.get("/api/insights").get_json()
        window = next(w for w in body["windows"] if w["key"] == GA)
        assert window["alert_state"] == "ALERTING" and window["analysis"]["documentation_id"] == "DOC-001"
        assert [s["service"] for s in body["services"]] == ["api"]
        assert [t["template_id"] for t in body["templates"]] == ["T3"]
        assert body["templates"][0]["analysis"]["verdict"] == "benign"
        assert body["llm"]["status"] == "ok" and body["grouping_revision"]
    finally:
        temporary.cleanup()


def test_analyze_each_kind_and_pending():
    temporary, base, client = _client()
    try:
        _beat(base)
        for kind, target in [("window", GA), ("service", "api"), ("template", "T3")]:
            response = client.post("/api/insights/analyze", json={"kind": kind, "id": target})
            assert response.status_code == 202, (kind, response.get_json())
        requests = load_requests(base / "analysis_requests.json")
        assert set(requests) == {f"window:{GA}", "service:api", "template:T3"}
        again = client.post("/api/insights/analyze", json={"kind": "service", "id": "api"})
        assert again.status_code == 409 and again.get_json()["error"] == "analysis_pending"
        listed = client.get("/api/insights").get_json()
        assert next(s for s in listed["services"] if s["service"] == "api")["analysis"]["status"] == "requested"
        for kind, target in [("window", json.dumps(["api", "NOPE"])), ("service", "ghost"),
                             ("template", "T404"), ("bogus", "x")]:
            assert client.post("/api/insights/analyze", json={"kind": kind, "id": target}).status_code in (400, 404)
    finally:
        temporary.cleanup()


def test_triage_all_unknown_templates():
    temporary, base, client = _client()
    try:
        _beat(base)
        response = client.post("/api/insights/analyze", json={"kind": "templates_all"})
        assert response.status_code == 202 and response.get_json()["queued"] == 1
        assert "template:T3" in load_requests(base / "analysis_requests.json")
        again = client.post("/api/insights/analyze", json={"kind": "templates_all"})
        assert again.get_json()["queued"] == 0
    finally:
        temporary.cleanup()


def test_analyze_blocked_with_reason():
    temporary, base, client = _client()
    try:
        _beat(base, age=3600)
        assert client.post("/api/insights/analyze", json={"kind": "service", "id": "api"}).status_code == 503
        _beat(base, llm_enabled=False, llm_status="disabled", llm_reason="No active LLM profile")
        response = client.post("/api/insights/analyze", json={"kind": "service", "id": "api"})
        assert response.status_code == 409 and response.get_json()["message"] == "No active LLM profile"
    finally:
        temporary.cleanup()


def test_delete_hides_record_and_404_without_one():
    temporary, base, client = _client()
    try:
        _beat(base)
        _write(base, "service_analysis.json", {"api": {"status": "done", "requested_at": time.time() - 5}})
        response = client.post("/api/insights/delete", json={"kind": "service", "id": "api"})
        assert response.status_code == 202
        assert load_requests(base / "analysis_requests.json")["service:api"][0] == "delete"
        listed = client.get("/api/insights").get_json()
        assert next(s for s in listed["services"] if s["service"] == "api")["analysis"] is None
        assert client.post("/api/insights/delete", json={"kind": "template", "id": "T3"}).status_code == 404
    finally:
        temporary.cleanup()


def test_stale_request_shows_failed_and_does_not_block():
    temporary, base, client = _client()
    try:
        _beat(base)
        old = time.time() - 3600
        add_request(base / "analysis_requests.json", "service", "api", old, now=old)
        record = next(s for s in client.get("/api/insights").get_json()["services"])["analysis"]
        assert record["status"] == "failed" and "did not pick up" in record["error"]
        assert client.post("/api/insights/analyze", json={"kind": "service", "id": "api"}).status_code == 202
    finally:
        temporary.cleanup()


def _revision(client):
    return client.get("/api/documentation").get_json()["revision"]


def test_save_document_assigns_it_to_groups():
    temporary, base, client = _client()
    try:
        response = client.post("/api/documentation", json={
            "revision": _revision(client), "title": "Gateway refused", "text": "Restart it.",
            "error_code": "E1", "assign_group_ids": ["GA", "GX"]})
        assert response.status_code == 201
        body = response.get_json()
        assert body["assigned_group_ids"] == ["GA"]
        assert body["assignment_skipped"] == [{"group_id": "GX", "reason": "not_found"}]
        overrides = json.loads((base / "documentation_overrides.json").read_text())["overrides"]
        assert overrides["GA"]["documentation_id"] == body["item"]["id"]
    finally:
        temporary.cleanup()


def test_save_document_skips_assignment_while_grouping_pending():
    temporary, base, client = _client()
    try:
        store = GroupingOverrideStore(base / "grouping_overrides.json", base / "grouping_status.json")
        store.replace_assignment("T1", "anchor", "T2", store.load_overrides()["revision"])
        response = client.post("/api/documentation", json={
            "revision": _revision(client), "title": "t", "text": "x", "assign_group_ids": ["GA"]})
        assert response.status_code == 201
        body = response.get_json()
        assert body["assigned_group_ids"] == []
        assert body["assignment_skipped"][0]["reason"] == "grouping_pending"
    finally:
        temporary.cleanup()

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


# --- Incident history + language ----------------------------------------------

def test_create_service_incident_then_delete():
    temporary, base, client = _client()
    try:
        _write(base, "service_analysis.json", {
            "api": {"status": "done", "service": "api", "language": "vi", "analyzed_at": 8.0,
                    "issues": [{"title": "t", "group_ids": ["G1", "G2"]}, {"title": "u", "group_ids": ["G2"]}],
                    "signature": {"template_texts": ["db timeout <*>", "socket closed"], "occurred_at": 42.0}},
            "quiet": {"status": "done", "service": "quiet", "issues": [], "signature": {"template_texts": []}},
        })
        fields = {"title": "DB down", "root_cause": "failover", "resolution": "restart"}
        response = client.post("/api/incident-cases", json={
            "service": "api", "template_texts": ["forged"], **fields})
        assert response.status_code == 201, response.get_json()
        case = response.get_json()
        assert case["template_texts"] == ["db timeout <*>", "socket closed"]
        assert case["group_ids"] == ["G1", "G2"] and case["occurred_at"] == 42.0
        assert case["language"] == "vi" and case["service"] == "api"
        assert client.post("/api/incident-cases", json={"service": "ghost", **fields}).status_code == 404
        assert client.post("/api/incident-cases", json={"service": "quiet", **fields}).status_code == 400
        assert client.post("/api/incident-cases", json={"service": "api", "title": ""}).status_code == 400
        listed = client.get("/api/incident-cases").get_json()["cases"]
        assert [c["id"] for c in listed] == ["CASE-0001"]
        assert client.delete("/api/incident-cases/CASE-0001").status_code == 200
        assert client.delete("/api/incident-cases/CASE-0001").status_code == 404
    finally:
        temporary.cleanup()


def test_analyze_language_stored_and_validated():
    temporary, base, client = _client()
    try:
        _beat(base)
        bad = client.post("/api/insights/analyze", json={"kind": "service", "id": "api", "language": "fr"})
        assert bad.status_code == 400
        ok = client.post("/api/insights/analyze", json={"kind": "service", "id": "api", "language": "vi"})
        assert ok.status_code == 202
        assert load_requests(base / "analysis_requests.json")["service:api"][2] == "vi"
    finally:
        temporary.cleanup()


def test_incident_keeps_pattern_numbers_from_the_analysis():
    temporary, base, client = _client()
    try:
        _write(base, "service_analysis.json", {"api": {
            "status": "done", "service": "api", "issues": [],
            "signature": {
                "templates": [{"text": "db timeout <*>", "level": "ERROR", "rate_per_min": 300.0,
                               "baseline_per_min": 7.5, "ratio": 40.0, "reasons": ["elevated"]}],
                "totals": {"events_15m": 31000, "error_events_15m": 4800}, "occurred_at": 42.0}}})
        response = client.post("/api/incident-cases", json={
            "service": "api", "title": "DB down", "root_cause": "failover",
            "pattern": [{"text": "forged", "ratio": 1.0}]})
        assert response.status_code == 201, response.get_json()
        case = response.get_json()
        assert case["template_texts"] == ["db timeout <*>"]
        assert case["pattern"][0]["ratio"] == 40.0 and case["pattern"][0]["reasons"] == ["elevated"]
        assert case["totals"] == {"events_15m": 31000, "error_events_15m": 4800}
    finally:
        temporary.cleanup()


def _registry(base):
    _write(base, "template_registry.json", {
        "T1": {"template_id": "T1", "template_text": "db timeout <*>", "service": "api", "level": "ERROR",
               "group_id": "G1"},
        "T2": {"template_id": "T2", "template_text": "receive message", "service": "api", "level": "INFO",
               "group_id": "G2"},
        "T3": {"template_id": "T3", "template_text": "pool exhausted", "service": "api", "level": "ERROR",
               "group_id": None},
        "W1": {"template_id": "W1", "template_text": "web thing", "service": "web", "level": "ERROR"},
    })


def test_save_from_analysis_can_drop_noise_and_add_templates():
    temporary, base, client = _client()
    try:
        _registry(base)
        _write(base, "service_analysis.json", {"api": {"status": "done", "service": "api", "issues": [],
            "signature": {"templates": [{"text": "db timeout <*>", "ratio": 40.0},
                                        {"text": "receive message", "ratio": 5.0}], "occurred_at": 9.0}}})
        response = client.post("/api/incident-cases", json={
            "service": "api", "title": "DB", "root_cause": "pool", "keep_texts": ["db timeout <*>"],
            "add_template_ids": ["T3"]})
        assert response.status_code == 201, response.get_json()
        case = response.get_json()
        assert case["template_texts"] == ["db timeout <*>", "pool exhausted"]
        assert case["pattern"][0]["ratio"] == 40.0 and case["pattern"][1]["reasons"] == ["manual"]
        empty = client.post("/api/incident-cases", json={
            "service": "api", "title": "DB", "root_cause": "pool", "keep_texts": []})
        assert empty.status_code == 400
    finally:
        temporary.cleanup()


def test_write_incident_by_hand_and_edit_it():
    temporary, base, client = _client()
    try:
        _registry(base)
        response = client.post("/api/incident-cases", json={
            "service": "api", "manual": True, "add_template_ids": ["T1", "T3"],
            "title": "Pool exhausted", "root_cause": "from experience", "resolution": "restart pool"})
        assert response.status_code == 201, response.get_json()
        case = response.get_json()
        assert case["source"] == {"kind": "manual"} and case["occurred_at"] is None
        assert case["template_texts"] == ["db timeout <*>", "pool exhausted"]
        assert client.post("/api/incident-cases", json={
            "service": "api", "manual": True, "add_template_ids": ["W1"], "title": "x",
            "root_cause": "y"}).status_code == 400  # another service's template
        assert client.post("/api/incident-cases", json={
            "service": "api", "manual": True, "title": "x", "root_cause": "y"}).status_code == 400

        edited = client.put(f"/api/incident-cases/{case['id']}", json={
            "title": "Pool exhausted at peak", "root_cause": "pool of 20 too small",
            "resolution": "raise to 80", "documentation_id": "DOC-005",
            "keep_texts": ["pool exhausted"], "add_template_ids": ["T2"]})
        assert edited.status_code == 200, edited.get_json()
        body = edited.get_json()
        assert body["template_texts"] == ["pool exhausted", "receive message"]
        assert body["documentation_id"] == "DOC-005" and body["updated_at"]
        assert client.put("/api/incident-cases/CASE-0404", json={
            "title": "x", "root_cause": "y"}).status_code == 404
        assert client.put(f"/api/incident-cases/{case['id']}", json={
            "title": "", "root_cause": "y"}).status_code == 400
        incidents = client.get("/api/insights").get_json()["incidents"]
        assert incidents[case["id"]]["root_cause"] == "pool of 20 too small"
    finally:
        temporary.cleanup()

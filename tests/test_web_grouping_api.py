from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from logai.storage.grouping import GroupingOverrideStore
from logai.web.app import create_app


def _client():
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    (base / "template_registry.json").write_text(json.dumps({
        "T1": {"template_id": "T1", "template_text": "one", "service": "api", "event_count": 2, "group_id": "GA"},
        "T2": {"template_id": "T2", "template_text": "two", "service": "api", "event_count": 5, "group_id": "GB"},
        "T3": {"template_id": "T3", "template_text": "three", "service": "api", "event_count": 1, "group_id": "UNASSIGNED_PENDING"},
    }), encoding="utf-8")
    (base / "group_registry.json").write_text(json.dumps({
        "GA": {"group_id": "GA", "template_ids": ["T1"], "representative_template": "one"},
        "GB": {"group_id": "GB", "template_ids": ["T2"], "representative_template": "two"},
    }), encoding="utf-8")
    app = create_app(data_dir=str(base))
    app.testing = True
    return temporary, base, app.test_client()


def test_assign_pending_template_to_existing_group_and_conflict():
    temporary, base, client = _client()
    try:
        revision = client.get("/api/groups").get_json()["grouping_revision"]
        accepted = client.put("/api/templates/T3/group", json={
            "target_group_id": "GB", "expected_revision": revision
        })
        assert accepted.status_code == 202
        body = accepted.get_json()
        assert body["requested_target"]["id"] == "T2"
        assert client.get("/api/grouping/status").get_json()["state"] == "engine_unavailable"
        conflict = client.put("/api/templates/T1/group", json={
            "target_group_id": "GB", "expected_revision": revision
        })
        assert conflict.status_code == 409
    finally:
        temporary.cleanup()


def test_group_listing_includes_every_member_template():
    temporary, _base, client = _client()
    try:
        groups = client.get("/api/groups").get_json()["items"]
        by_id = {group["group_id"]: group for group in groups}

        assert by_id["GA"]["templates"] == [{
            "template_id": "T1",
            "template_text": "one",
            "service": "api",
            "level": "INFO",
            "event_count": 2,
            "first_seen": 0,
            "last_seen": 0,
        }]
        assert [template["template_id"] for template in by_id["GB"]["templates"]] == ["T2"]
    finally:
        temporary.cleanup()


def test_create_manual_group_and_expose_applied_result():
    temporary, base, client = _client()
    try:
        revision = client.get("/api/groups").get_json()["grouping_revision"]
        accepted = client.put("/api/templates/T3/group", json={
            "create_new": True, "expected_revision": revision
        }).get_json()
        assert accepted["requested_target"]["id"] == "G_MANUAL_001"

        store = GroupingOverrideStore(
            base / "grouping_overrides.json", base / "grouping_status.json"
        )
        now = time.time()
        store.write_status({
            "applied_revision": accepted["revision"],
            "attempted_revision": accepted["revision"],
            "last_attempt_at": now,
            "last_applied_at": now,
            "last_heartbeat_at": now,
            "state": "applied",
            "error": None,
            "results": {"T3": {"state": "applied", "effective_group_id": accepted["requested_target"]["id"]}},
            "unresolved": {},
        })
        detail = client.get("/api/templates/T3").get_json()
        assert detail["manual_assignment"]["target_kind"] == "manual_group"
        assert detail["grouping_result"]["state"] == "applied"
    finally:
        temporary.cleanup()


def test_alert_api_filters_orphan_group_state():
    temporary, base, client = _client()
    try:
        (base / "anomaly_state.json").write_text(json.dumps({
            json.dumps(["api", "DELETED"]): {
                "group_id": ["api", "DELETED"], "alert_state": "ALERTING"
            }
        }), encoding="utf-8")
        listing = client.get("/api/alerts").get_json()
        assert all(item["group_id"] != "DELETED" for item in listing["items"])
    finally:
        temporary.cleanup()


def test_same_group_noop_still_rejects_stale_revision():
    temporary, base, client = _client()
    try:
        store = GroupingOverrideStore(
            base / "grouping_overrides.json", base / "grouping_status.json"
        )
        original = store.load_overrides()
        changed = store.replace_assignment(
            "T2", "anchor", "T1", original["revision"]
        )
        now = time.time()
        store.write_status({
            "applied_revision": changed["revision"],
            "attempted_revision": changed["revision"],
            "last_attempt_at": now,
            "last_applied_at": now,
            "last_heartbeat_at": now,
            "state": "applied",
            "error": None,
            "results": {"T2": {"state": "applied", "effective_group_id": "GA"}},
            "unresolved": {},
        })

        response = client.put("/api/templates/T1/group", json={
            "target_group_id": "GA",
            "expected_revision": original["revision"],
        })

        assert response.status_code == 409
        assert response.get_json()["current_revision"] == changed["revision"]
    finally:
        temporary.cleanup()


def test_health_reports_engine_progress_from_fresh_heartbeat():
    temporary, base, client = _client()
    try:
        store = GroupingOverrideStore(
            base / "grouping_overrides.json", base / "grouping_status.json"
        )
        revision = store.load_overrides()["revision"]
        now = time.time()
        store.write_status({
            "applied_revision": revision,
            "attempted_revision": revision,
            "last_attempt_at": now,
            "last_applied_at": now,
            "last_heartbeat_at": now,
            "state": "applied",
            "error": None,
            "results": {},
            "unresolved": {},
            "runtime": {
                "last_successful_poll_at": now - 1,
                "last_processed_event_at": 123.0,
                "last_checkpoint_commit_at": now - 2,
            },
        })

        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.get_json()["engine_alive"] is True
        assert response.get_json()["last_processed_event_at"] == 123.0
    finally:
        temporary.cleanup()


def test_alert_api_attaches_incident_analysis():
    temporary, base, client = _client()
    try:
        key = json.dumps(["api", "GA"])
        (base / "anomaly_state.json").write_text(json.dumps({
            key: {"group_id": ["api", "GA"], "alert_state": "ALERTING"}
        }), encoding="utf-8")
        (base / "incident_analysis.json").write_text(json.dumps({
            key: {"status": "done", "service": "api", "group_id": "GA",
                  "documentation_id": "DOC-001", "confidence": 0.82}
        }), encoding="utf-8")
        listing = client.get("/api/alerts").get_json()
        item = next(i for i in listing["items"] if i["group_id"] == "GA")
        assert item["analysis"]["documentation_id"] == "DOC-001"
        assert next(i for i in listing["items"] if i["group_id"] == "GB")["analysis"] is None
    finally:
        temporary.cleanup()

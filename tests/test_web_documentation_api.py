from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from logai.storage.documentation import group_fingerprint
from logai.web.app import create_app


class TestWebDocumentationAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.seed = self.base / "seed.yaml"
        self.seed.write_text("- id: DOC-1\n  title: Timeout\n  text: Database timeout\n", encoding="utf-8")
        (self.base / "template_registry.json").write_text(json.dumps({
            "T1": {"template_id": "T1", "template_text": "Database <*> timeout", "service": "api"}
        }), encoding="utf-8")
        (self.base / "group_registry.json").write_text(json.dumps({
            "G1": {"group_id": "G1", "template_ids": ["T1"], "service": "api", "documented": False}
        }), encoding="utf-8")
        app = create_app(
            str(self.base),
            str(self.base / "documentation_corpus.json"),
            str(self.base / "documentation_overrides.json"),
            str(self.base / "documentation_status.json"),
            str(self.seed),
        )
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_document_crud_and_revision_conflict(self):
        listing = self.client.get("/api/documentation").get_json()
        created = self.client.post("/api/documentation", json={
            "revision": listing["revision"], "title": "Auth", "text": "Auth failed", "error_code": "ERR_AUTH"
        })
        self.assertEqual(created.status_code, 201)
        stale = self.client.post("/api/documentation", json={
            "revision": listing["revision"], "text": "stale"
        })
        self.assertEqual(stale.status_code, 409)

    def test_documentation_reports_active_group_count(self):
        registry_path = self.base / "group_registry.json"
        groups = json.loads(registry_path.read_text(encoding="utf-8"))
        groups["G1"].update({
            "documented": True,
            "documentation_id": "DOC-1",
            "error_code": "ERR_TIMEOUT",
        })
        registry_path.write_text(json.dumps(groups), encoding="utf-8")

        listing = self.client.get("/api/documentation").get_json()
        self.assertEqual(listing["items"][0]["group_count"], 1)
        self.assertEqual(listing["items"][0]["manual_group_count"], 0)

    def test_alerts_merge_persisted_state_with_group_level(self):
        templates_path = self.base / "template_registry.json"
        templates = json.loads(templates_path.read_text(encoding="utf-8"))
        templates["T1"]["level"] = "ERROR"
        templates_path.write_text(json.dumps(templates), encoding="utf-8")
        (self.base / "anomaly_state.json").write_text(json.dumps({
            json.dumps(["api", "G1"]): {
                "group_id": ["api", "G1"],
                "timestamp": 123.0,
                "anomaly_score": 0.91,
                "anomaly": True,
                "consecutive_anomaly_count": 4,
                "alert_state": "ALERTING",
            }
        }), encoding="utf-8")

        listing = self.client.get("/api/alerts").get_json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["counts"]["ALERTING"], 1)
        self.assertEqual(listing["items"][0]["group_id"], "G1")
        self.assertEqual(listing["items"][0]["service"], "api")
        self.assertEqual(listing["items"][0]["level"], "ERROR")

    def test_alerts_include_unevaluated_groups_as_normal(self):
        listing = self.client.get("/api/alerts").get_json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["counts"]["NORMAL"], 1)
        self.assertEqual(listing["items"][0]["alert_state"], "NORMAL")

    def test_assign_clear_and_delete_protection(self):
        docs = self.client.get("/api/documentation").get_json()
        groups = self.client.get("/api/groups").get_json()
        assigned = self.client.put("/api/groups/G1/documentation", json={
            "documentation_id": "DOC-1", "override_revision": groups["override_revision"]
        })
        self.assertEqual(assigned.status_code, 200)
        blocked = self.client.delete("/api/documentation/DOC-1", json={"revision": docs["revision"]})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["group_ids"], ["G1"])
        cleared = self.client.delete("/api/groups/G1/documentation", json={
            "override_revision": assigned.get_json()["override_revision"]
        })
        self.assertEqual(cleared.status_code, 200)

    def test_ui_exposes_separate_sidebar_views_and_reload(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-view="templates"', html)
        self.assertIn('data-view="groups"', html)
        self.assertIn('data-view="alerting"', html)
        self.assertIn('data-view="documentation"', html)
        self.assertIn('id="reload-app"', html)
        self.assertIn("Error code", html)
        self.assertIn('id="group-stat-total"', html)
        self.assertIn('data-group-sort="error_code"', html)
        self.assertIn('data-document-sort="group_count"', html)
        self.assertIn('data-alert-sort="level"', html)
        self.assertIn("state.alertRefreshTimer", html)
        self.assertIn("height: 100vh", html)
        self.assertIn("position: sticky", html)
        self.assertIn("state.reconnectTimer", html)


if __name__ == "__main__":
    unittest.main()

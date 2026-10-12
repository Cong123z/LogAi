"""Elasticsearch endpoint and credentials set on the web: connection store,
engine refresh (with the cluster-change cursor reset), training and web API."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

for mod in [
    "numpy", "sklearn", "sklearn.ensemble", "hdbscan", "elasticsearch",
    "drain3", "drain3.template_miner", "drain3.file_persistence",
    "drain3.template_miner_config", "drain3.masking", "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from test_web_grouping_api import _client

from logai.config import AppConfig, ElasticsearchConfig
from logai.realtime.realtime_pipeline import RealtimePipeline
from logai.storage.es_connection import (
    ConnectionConflict,
    ConnectionError_,
    ESConnectionStore,
    apply,
    display_host,
    hosts_fingerprint,
)
from logai.storage.grouping import GroupingOverrideStore
from logai.storage.index_selection import IndexSelectionStore


def _save(store, revision=None, **fields):
    values = {"hosts": ["https://es-a:9200"], "username": "elastic", "password": "pw",
              "verify_certs": True, **fields}
    return store.save(expected_revision=revision, **values)


# -- store ---------------------------------------------------------------------

def test_store_saves_0600_and_never_shows_the_password(tmp_path):
    store = ESConnectionStore(tmp_path / "conn.json")
    assert store.load() is None and store.public_view() is None
    saved = _save(store, hosts="https://es-a:9200/, https://es-b:9200")
    assert saved["hosts"] == ["https://es-a:9200", "https://es-b:9200"]
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    view = store.public_view()
    assert "password" not in view and view["password_set"] is True
    assert view["revision"] == saved["revision"]


def test_validation(tmp_path):
    store = ESConnectionStore(tmp_path / "conn.json")
    for hosts in ([], [""], ["ftp://es"], ["https://user:pw@es"], ["https://es:99999"], 5):
        with pytest.raises(ConnectionError_):
            _save(store, hosts=hosts)
    with pytest.raises(ConnectionError_):
        _save(store, password="a\nb")


def test_empty_password_keeps_it_unless_the_cluster_changes(tmp_path):
    store = ESConnectionStore(tmp_path / "conn.json")
    first = _save(store)
    second = _save(store, first["revision"], password="", username="reader", verify_certs=False)
    assert store.load()["password"] == "pw" and store.load()["username"] == "reader"
    with pytest.raises(ConnectionError_, match="Re-enter the password"):
        _save(store, second["revision"], hosts=["https://evil:9200"], password="")
    with pytest.raises(ConnectionConflict):
        _save(store, first["revision"])
    _save(store, second["revision"], hosts=["https://es-c:9200"], password="new")
    assert store.load()["password"] == "new"


def test_clear_needs_current_revision(tmp_path):
    store = ESConnectionStore(tmp_path / "conn.json")
    saved = _save(store)
    with pytest.raises(ConnectionConflict):
        store.clear(None)
    store.clear(saved["revision"])
    assert store.load() is None


def test_apply_fingerprint_and_display():
    env = ElasticsearchConfig(hosts=["http://env:9200"], index="logs-*", username="u", password="p")
    assert apply(env, None) is env
    merged = apply(env, {"hosts": ["https://es-a"], "username": "", "password": "", "verify_certs": False})
    assert merged.hosts == ["https://es-a"] and merged.username is None and merged.password is None
    assert merged.verify_certs is False and merged.index == "logs-*"
    assert hosts_fingerprint(["https://B:443/x", "http://a"]) == hosts_fingerprint(["http://a:9200", "https://b"])
    assert display_host("https://user:pw@es:9200/p") == "https://es:9200/p"


# -- realtime engine -----------------------------------------------------------

class TestEngineConnection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = AppConfig()
        cfg.storage.base_dir = self.tmp
        cfg.storage.model_dir = f"{self.tmp}/models"
        cfg.doc_matcher.corpus_path = f"{self.tmp}/missing.yaml"
        cfg.elasticsearch.hosts = ["http://env-es:9200"]
        self.cfg = cfg
        self.pipeline = self._start()
        self.store = ESConnectionStore(Path(self.tmp) / "es_connection.json")
        IndexSelectionStore(Path(self.tmp) / "es_index_selection.json").replace(["logs-*"], None, now=500.0)
        self.pipeline._refresh_index_selection()
        self.pipeline.checkpoint.commit_indices({"logs-1": ([1000, 1], 1000.0)})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self):
        with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
            pipeline = RealtimePipeline(self.cfg)
        pipeline.collector.resolve = MagicMock(side_effect=lambda patterns: {"logs-*": ["logs-1"]})
        return pipeline

    def test_new_credentials_on_the_same_cluster_keep_cursors(self):
        _save(self.store, hosts=["http://ENV-ES:9200/"])
        self.assertTrue(self.pipeline._refresh_es_connection())
        self.assertEqual(self.pipeline.collector.config.username, "elastic")
        self.assertEqual(self.pipeline.collector.config.password, "pw")
        self.pipeline._refresh_index_selection()
        self.assertEqual(self.pipeline.checkpoint.get_index_cursor("logs-1")["search_after"], [1000, 1])
        self.assertFalse(self.pipeline._refresh_es_connection())  # unchanged file

    def test_another_cluster_is_read_from_now(self):
        _save(self.store)
        before = time.time()
        self.assertTrue(self.pipeline._refresh_es_connection())
        self.assertEqual(self.pipeline.collector.config.hosts, ["https://es-a:9200"])
        self.pipeline._refresh_index_selection()
        cursor = self.pipeline.checkpoint.get_index_cursor("logs-1")
        self.assertIsNone(cursor["search_after"])
        self.assertGreaterEqual(cursor["floor_ts"], before)

        # Back to the env config: that cluster is also read from now.
        self.store.clear(self.store.load()["revision"])
        self.assertTrue(self.pipeline._refresh_es_connection())
        self.assertEqual(self.pipeline.collector.config.hosts, ["http://env-es:9200"])

    def test_change_made_while_the_engine_was_down(self):
        _save(self.store)
        restarted = self._start()
        self.assertEqual(restarted.collector.config.hosts, ["https://es-a:9200"])
        self.assertIsNone(restarted.checkpoint.get_index_cursor("logs-1"))

    def test_saving_new_settings_cuts_the_failure_backoff_short(self):
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                _save(self.store)  # the user fixes the settings meanwhile

        with patch("logai.realtime.realtime_pipeline.time.sleep", sleep):
            self.pipeline._backoff_sleep(300.0)
        self.assertEqual(sleeps, [1.0, 1.0])

        # A failing search stops retrying once new settings are saved.
        collector = self.pipeline.collector
        collector.client = MagicMock()
        collector.client.search.side_effect = ConnectionRefusedError("down")
        self.assertTrue(self.pipeline._connection_changed())
        with patch("logai.collector.es_collector.time.sleep") as retry_sleep:
            with self.assertRaises(ConnectionRefusedError):
                collector._search({}, "logs-1")
        retry_sleep.assert_not_called()

    def test_status_reports_connection_without_secrets(self):
        _save(self.store)
        self.pipeline._refresh_es_connection()
        self.pipeline.collector.list_indices = MagicMock(return_value=[{"index": "logs-1"}])
        self.pipeline._refresh_index_selection()
        self.pipeline._publish_index_status()
        status = json.loads((Path(self.tmp) / "es_index_status.json").read_text())
        self.assertEqual(status["connection"], {
            "source": "web", "revision": self.store.load()["revision"],
            "hosts": ["https://es-a:9200"], "username": "elastic",
            "verify_certs": True, "connected": True,
        })
        self.assertNotIn("pw", json.dumps(status))


def test_checkpoint_without_cluster_is_adopted(tmp_path):
    cfg = AppConfig()
    cfg.storage.base_dir = str(tmp_path)
    cfg.storage.model_dir = f"{tmp_path}/models"
    cfg.doc_matcher.corpus_path = f"{tmp_path}/missing.yaml"
    (tmp_path / "checkpoint.json").write_text(json.dumps({"search_after": [5, 1], "last_timestamp": 5.0}))
    with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
        pipeline = RealtimePipeline(cfg)
    assert pipeline.checkpoint.get_search_after() == [5, 1]
    assert pipeline.checkpoint.cluster()["since"] is None


# -- training --------------------------------------------------------------------

def test_training_reads_the_web_connection(tmp_path):
    from logai.training import train_pipeline

    cfg = AppConfig()
    cfg.storage.base_dir = str(tmp_path)
    _save(ESConnectionStore(tmp_path / "es_connection.json"))
    with patch.object(train_pipeline, "ElasticsearchCollector", side_effect=RuntimeError("stop")) as collector:
        with pytest.raises(RuntimeError, match="stop"):
            train_pipeline.run_training_from_elasticsearch(cfg)
    used = collector.call_args.args[0]
    assert used.hosts == ["https://es-a:9200"] and used.password == "pw"


# -- web API ---------------------------------------------------------------------

def test_web_get_put_delete():
    temporary, base, client = _client()
    try:
        GroupingOverrideStore(base / "grouping_overrides.json", base / "grouping_status.json") \
            .update_heartbeat(time.time())
        (base / "es_index_status.json").write_text(json.dumps({"connection": {
            "source": "env", "revision": None, "hosts": ["http://env:9200"],
            "username": "", "verify_certs": True, "connected": True}}))
        view = client.get("/api/es-connection").get_json()
        assert view["state"] == "connected" and view["source"] == "env" and view["saved"] is None
        assert view["engine"]["hosts"] == ["http://env:9200"]

        assert client.put("/api/es-connection", json={"hosts": ["https://u:p@es"]}).status_code == 400
        saved = client.put("/api/es-connection", json={
            "hosts": ["https://es-a:9200"], "username": "elastic", "password": "secret",
            "verify_certs": False, "revision": None})
        assert saved.status_code == 202
        body = saved.get_json()
        assert body["state"] == "pending" and body["saved"]["password_set"] is True
        assert "secret" not in saved.get_data(as_text=True)
        assert "secret" not in client.get("/api/es-connection").get_data(as_text=True)

        stale = client.put("/api/es-connection", json={"hosts": ["https://es-a:9200"], "revision": None})
        assert stale.status_code == 409

        (base / "es_index_status.json").write_text(json.dumps({
            "error": "401 Unauthorized", "connection": {
                "source": "web", "revision": body["saved"]["revision"], "connected": False}}))
        view = client.get("/api/es-connection").get_json()
        assert view["state"] == "error" and view["error"] == "401 Unauthorized"

        assert client.delete("/api/es-connection").status_code == 409
        cleared = client.delete(f"/api/es-connection?revision={body['saved']['revision']}")
        assert cleared.status_code == 202 and cleared.get_json()["source"] == "env"
    finally:
        temporary.cleanup()

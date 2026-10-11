"""Choosing Elasticsearch indices on the web: selection store, per-index
cursors, engine refresh and the web API."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

import pytest

# Same lightweight stubs as the other realtime tests, so collection order
# never mixes a stubbed and a real copy of these drivers.
for mod in [
    "numpy", "sklearn", "sklearn.ensemble", "hdbscan", "elasticsearch",
    "drain3", "drain3.template_miner", "drain3.file_persistence",
    "drain3.template_miner_config", "drain3.masking", "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from test_web_grouping_api import _client

from logai.collector.es_collector import ElasticsearchCollector
from logai.config import AppConfig
from logai.models import FeatureVector, RawLog
from logai.realtime.realtime_pipeline import RealtimePipeline
from logai.storage.checkpoint import CheckpointStore
from logai.storage.grouping import GroupingOverrideStore
from logai.storage.index_selection import (
    IndexSelectionConflict,
    IndexSelectionError,
    IndexSelectionStore,
)


# -- selection store -----------------------------------------------------------

def test_store_validates_dedups_and_keeps_added_at(tmp_path):
    store = IndexSelectionStore(tmp_path / "sel.json")
    assert store.load() is None
    first = store.replace(["app-logs-*", "orders", "orders"], None, now=100.0)
    assert [e["pattern"] for e in first["entries"]] == ["app-logs-*", "orders"]
    second = store.replace(["orders", "pay"], first["revision"], now=200.0)
    assert {e["pattern"]: e["added_at"] for e in second["entries"]} == {"orders": 100.0, "pay": 200.0}
    assert store.load()["revision"] == second["revision"]
    with pytest.raises(IndexSelectionConflict):
        store.replace(["x"], first["revision"])
    for bad in (["Upper"], ["a,b"], ["-x"], ["_x"], [""], "notalist"):
        with pytest.raises(IndexSelectionError):
            store.replace(bad, second["revision"])


def test_first_save_keeps_engine_start_time(tmp_path):
    store = IndexSelectionStore(tmp_path / "sel.json")
    saved = store.replace(["recharge-logs", "new"], None, known_added_at={"recharge-logs": 5.0}, now=9.0)
    assert {e["pattern"]: e["added_at"] for e in saved["entries"]} == {"recharge-logs": 5.0, "new": 9.0}


# -- collector -------------------------------------------------------------------

def _collector(tmp_path, hits):
    cfg = AppConfig()
    cfg.storage.base_dir = str(tmp_path)
    collector = object.__new__(ElasticsearchCollector)
    collector.config = cfg.elasticsearch
    collector.checkpoint = CheckpointStore(cfg.storage)
    collector._malformed_counter = None
    collector._search = MagicMock(return_value={"hits": {"hits": hits}})
    return collector


def _hit(doc_id, index, sort):
    return {"_id": doc_id, "_index": index, "sort": sort, "_source": {
        "@timestamp": "2026-10-07T00:00:00Z", "service": "api", "message": "ok"}}


def test_poll_index_uses_its_floor_then_its_own_cursor(tmp_path):
    collector = _collector(tmp_path, [_hit("a1", "idx-a", [5, 1])])
    collector.checkpoint.set_index_floors({"idx-a": 1_790_000_000.0, "idx-b": 1.0})
    collector.checkpoint.commit_indices({"idx-b": ([9, 9], 9.0)})

    batch, cursor = collector.poll_batch("idx-a")
    body, index = collector._search.call_args.args
    assert index == "idx-a" and "search_after" not in body
    assert body["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"].startswith("2026-09-21")
    assert cursor == [5, 1] and batch[0].es_index == "idx-a"

    collector.poll_batch("idx-b")
    body, index = collector._search.call_args.args
    # From its own cursor, re-reading the cursor's millisecond.
    assert index == "idx-b" and "search_after" not in body
    assert body["query"]["bool"]["filter"] == [
        {"range": {"@timestamp": {"gte": 9, "format": "epoch_millis"}}}]
    # Fetching never commits.
    assert collector.checkpoint.get_index_cursor("idx-a")["search_after"] is None


def test_millisecond_filling_a_page_is_read_whole(tmp_path):
    same_ms = [_hit(f"d{i}", "idx-a", [9, i]) for i in range(5)]
    collector = _collector(tmp_path, [])
    collector._search.side_effect = [{"hits": {"hits": same_ms[:3]}}, {"hits": {"hits": same_ms}}]
    collector.config.batch_size = 3
    collector.checkpoint.commit_indices({"idx-a": ([9, 0], 0.009)})
    batch, _ = collector.poll_batch("idx-a")
    first, second = [call.args[0] for call in collector._search.call_args_list]
    assert first["size"] == 3 and second["size"] == 10_000
    assert "search_after" not in second and len(batch) == 5


def test_millisecond_beyond_the_largest_page_falls_back_to_search_after(tmp_path):
    full = {"hits": {"hits": [_hit(f"d{i}", "idx-a", [9, i]) for i in range(3)]}}
    collector = _collector(tmp_path, [])
    collector._search.side_effect = [full, full, full]
    with patch("logai.collector.es_collector._WHOLE_MILLISECOND", 3):
        collector.config.batch_size = 3
        collector.checkpoint.commit_indices({"idx-a": ([9, 0], 0.009)})
        collector.poll_batch("idx-a")
    bodies = [call.args[0] for call in collector._search.call_args_list]
    assert len(bodies) == 2 and bodies[-1]["search_after"] == [9, 0]


def test_historical_pages_reread_the_cursor_millisecond(tmp_path):
    collector = _collector(tmp_path, [])
    collector._search.side_effect = [
        {"hits": {"hits": [_hit("a", "i", [5, 1]), _hit("b", "i", [7, 2])]}},
        {"hits": {"hits": [_hit("b", "i", [7, 2])]}},
    ]
    pages = list(collector.stream_historical_batches(start_ts=0.0, max_docs=10, batch_size=2))
    assert [cursor for _, cursor in pages] == [[7, 2], [7, 2]]
    second = collector._search.call_args_list[1].args[0]
    assert "search_after" not in second
    assert {"range": {"@timestamp": {"gte": 7, "format": "epoch_millis"}}} in second["query"]["bool"]["filter"]


def test_unknown_index_gets_a_now_floor_once(tmp_path):
    collector = _collector(tmp_path, [])
    before = time.time()
    collector.poll_batch("idx-new")
    floor = collector.checkpoint.get_index_cursor("idx-new")["floor_ts"]
    assert floor >= before
    collector.poll_batch("idx-new")
    assert collector.checkpoint.get_index_cursor("idx-new")["floor_ts"] == floor


def test_resolve_and_list_indices():
    collector = object.__new__(ElasticsearchCollector)
    collector.client = MagicMock()
    collector.client.indices.get.side_effect = lambda index, **_: (
        {"logs-2": {}, "logs-1": {}} if index == "logs-*" else {})
    # Shaped like elasticsearch-py's ListApiResponse: the list is in .body.
    collector.client.cat.indices.return_value = MagicMock(body=[
        {"index": ".kibana", "docs.count": "3"},
        {"index": "logs-1", "docs.count": "10", "health": "green", "status": "open"},
    ])
    assert collector.resolve(["logs-*", "none"]) == {"logs-*": ["logs-1", "logs-2"], "none": []}
    assert collector.list_indices() == [
        {"index": "logs-1", "docs_count": 10, "health": "green", "status": "open"}]


# -- realtime engine ---------------------------------------------------------------

class _Stop(BaseException):
    pass


class TestEngineSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = AppConfig()
        cfg.storage.base_dir = self.tmp
        cfg.storage.model_dir = f"{self.tmp}/models"
        cfg.doc_matcher.corpus_path = f"{self.tmp}/missing.yaml"
        cfg.anomaly.predict_batch_size = 1
        self.cfg = cfg
        with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
            self.pipeline = RealtimePipeline(cfg)
        self.pipeline.start_metrics_server = MagicMock()
        self.pipeline.collector.resolve = MagicMock(side_effect=lambda patterns: {
            p: {"logs-*": ["logs-1", "logs-2"], "orders": ["orders"]}.get(p, []) for p in patterns})
        self.store = IndexSelectionStore(Path(self.tmp) / "es_index_selection.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _process(self, raw):
        self.pipeline._pending_predictions.append(
            (raw.event_id, FeatureVector(group_id=raw.event_id, timestamp=raw.timestamp)))
        return True

    def test_without_saved_selection_keeps_legacy_cursor(self):
        self.assertFalse(self.pipeline._refresh_index_selection())
        self.assertIsNone(self.pipeline._active_indices)

    def test_selection_applies_floors_and_drops_removed(self):
        saved = self.store.replace(["logs-*", "orders"], None, now=500.0)
        self.assertTrue(self.pipeline._refresh_index_selection())
        self.assertEqual(self.pipeline._active_indices, ["logs-1", "logs-2", "orders"])
        self.assertEqual(self.pipeline.checkpoint.get_index_cursor("logs-2")["floor_ts"], 500.0)

        self.store.replace(["orders"], saved["revision"], now=600.0)
        self.assertTrue(self.pipeline._refresh_index_selection())
        self.assertEqual(self.pipeline._active_indices, ["orders"])
        self.assertIsNone(self.pipeline.checkpoint.get_index_cursor("logs-1"))
        # orders keeps its original start.
        self.assertEqual(self.pipeline.checkpoint.get_index_cursor("orders")["floor_ts"], 500.0)

    def test_switch_from_legacy_starts_where_it_stopped(self):
        self.pipeline.checkpoint.commit([1, 2], 400.0)
        self.store.replace(["orders"], None, now=900.0)
        self.pipeline._refresh_index_selection()
        self.assertEqual(self.pipeline.checkpoint.get_index_cursor("orders")["floor_ts"], 400.0)

    def test_new_index_under_old_pattern_starts_at_pattern_time(self):
        self.store.replace(["logs-*"], None, now=500.0)
        self.pipeline._refresh_index_selection()
        self.pipeline.collector.resolve.side_effect = lambda patterns: {"logs-*": ["logs-1", "logs-2", "logs-3"]}
        self.pipeline._last_resolve_at = float("-inf")
        self.pipeline._refresh_index_selection()
        self.assertEqual(self.pipeline.checkpoint.get_index_cursor("logs-3")["floor_ts"], 500.0)

    def test_each_index_is_polled_and_committed_after_flush(self):
        self.store.replace(["logs-*"], None, now=500.0)
        batches = {
            "logs-1": ([RawLog(timestamp=1000.0, service="a", level="INFO", message="x", event_id="e1")], [1000, 1]),
            "logs-2": ([RawLog(timestamp=1001.0, service="a", level="INFO", message="y", event_id="e2")], [1001, 7]),
        }
        calls = []

        def poll(index=None):
            calls.append(index)
            if len(calls) > 2:
                raise _Stop()
            return batches[index]

        self.pipeline.collector.poll_batch = MagicMock(side_effect=poll)
        self.pipeline._process_one = MagicMock(side_effect=self._process)
        with self.assertRaises(_Stop):
            self.pipeline.run_forever()
        self.assertEqual(calls[:2], ["logs-1", "logs-2"])
        cp = self.pipeline.checkpoint
        self.assertEqual(cp.get_index_cursor("logs-1")["search_after"], [1000, 1])
        self.assertEqual(cp.get_index_cursor("logs-2")["last_timestamp"], 1001.0)
        self.assertIsNone(cp.get_search_after())  # legacy cursor untouched

    def test_restart_while_es_is_down_keeps_every_cursor(self):
        self.store.replace(["logs-*"], None, now=500.0)
        self.pipeline._refresh_index_selection()
        self.pipeline.checkpoint.commit_indices({"logs-1": ([1000, 1], 1000.0)})

        with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
            restarted = RealtimePipeline(self.cfg)
        restarted.collector.resolve = MagicMock(side_effect=ConnectionError("es down"))
        restarted._refresh_index_selection()
        cp = restarted.checkpoint
        self.assertEqual(cp.get_index_cursor("logs-1")["search_after"], [1000, 1])
        # The known indices are still polled, so the outage is reported.
        self.assertEqual(restarted._active_indices, ["logs-1", "logs-2"])

        restarted.collector.resolve = self.pipeline.collector.resolve
        restarted._last_resolve_at = float("-inf")
        restarted._refresh_index_selection()
        self.assertEqual(cp.get_index_cursor("logs-1")["search_after"], [1000, 1])

    def test_page_of_only_malformed_hits_is_stepped_over(self):
        self.store.replace(["orders"], None, now=500.0)
        collector = self.pipeline.collector
        collector._search = MagicMock(return_value={"hits": {"hits": [{"_id": "x", "sort": [5, 1]}]}})
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise _Stop()

        with patch("logai.realtime.realtime_pipeline.time.sleep", sleep):
            with self.assertRaises(_Stop):
                self.pipeline._poll_loop()
        self.assertEqual(self.pipeline.checkpoint.get_index_cursor("orders")["search_after"], [5, 1])
        self.assertEqual(collector._search.call_args.args[0]["query"]["bool"]["filter"],
                         [{"range": {"@timestamp": {"gte": 5, "format": "epoch_millis"}}}])

    def test_reread_boundary_doc_counts_as_nothing_new(self):
        self.store.replace(["orders"], None, now=500.0)
        self.pipeline._refresh_index_selection()
        seen = RawLog(timestamp=1000.0, service="a", level="INFO", message="x", event_id="e1")
        self.pipeline.dedup.mark("e1")
        self.pipeline.checkpoint.commit_indices({"orders": ([1000000, 4], 1000.0)})
        # Same doc, other _doc after a merge: still nothing new.
        self.pipeline.collector.poll_batch = MagicMock(return_value=([seen], [1000000, 9]))
        self.pipeline._process_one = MagicMock()
        self.pipeline.checkpoint.commit_indices = MagicMock()
        with patch("logai.realtime.realtime_pipeline.time.sleep", side_effect=_Stop):
            with self.assertRaises(_Stop):  # slept: nothing new
                self.pipeline._poll_loop()
        self.pipeline._process_one.assert_not_called()
        self.pipeline.checkpoint.commit_indices.assert_not_called()

    def test_status_file_for_the_web(self):
        self.store.replace(["orders"], None, now=500.0)
        self.pipeline._refresh_index_selection()
        self.pipeline.collector.list_indices = MagicMock(return_value=[{"index": "orders"}])
        self.pipeline._publish_index_status()
        status = json.loads((Path(self.tmp) / "es_index_status.json").read_text())
        self.assertEqual(status["selection_revision"], self.store.load()["revision"])
        self.assertEqual(status["resolved"], {"orders": ["orders"]})
        self.assertEqual(status["available"], [{"index": "orders"}])


# -- web API -------------------------------------------------------------------------

def test_web_get_put_and_conflict():
    temporary, base, client = _client()
    try:
        GroupingOverrideStore(base / "grouping_overrides.json", base / "grouping_status.json") \
            .update_heartbeat(time.time())
        (base / "es_index_status.json").write_text(json.dumps({
            "mode": "configured", "entries": [{"pattern": "recharge-logs", "added_at": 5.0}],
            "available": [{"index": "recharge-logs"}]}))
        view = client.get("/api/es-indices").get_json()
        assert view["source"] == "configured" and view["revision"] is None
        assert view["state"] == "applied"

        assert client.put("/api/es-indices", json={"patterns": ["Bad,"]}).status_code == 400
        saved = client.put("/api/es-indices", json={"patterns": ["recharge-logs", "app-*"], "revision": None})
        assert saved.status_code == 202
        assert saved.get_json()["entries"][0]["added_at"] == 5.0
        view = client.get("/api/es-indices").get_json()
        assert view["source"] == "selected" and view["state"] == "pending"

        stale = client.put("/api/es-indices", json={"patterns": ["x"], "revision": None})
        assert stale.status_code == 409
        assert stale.get_json()["current_revision"] == view["revision"]
    finally:
        temporary.cleanup()

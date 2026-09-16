"""(service, group_id) window key: independent windows, serialization seam, idle-tick.

Guards the three contracts the refactor introduces:
  1. FeatureEngine keeps a SEPARATE sliding window per (service, group) so a
     service's rate baseline is no longer averaged with co-grouped services.
  2. The tuple identity survives the ONLY flatten boundary - anomaly_state.json
     (JSONStore can't key a dict by a tuple) - and _non_normal reloads as TUPLES
     (regression: a string key would miss the tuple window keys in the idle-tick
     union and strand a pre-restart ALERTING cell).
  3. The idle tick drives off (alert set UNION live windows) with a PER-CELL
     silence guard, so a silent service inside a chatty group still cools down.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

for _mod in [
    "numpy", "sklearn", "sklearn.ensemble", "hdbscan", "elasticsearch",
    "sentence_transformers", "drain3", "drain3.template_miner",
    "drain3.file_persistence", "drain3.template_miner_config", "drain3.masking",
    "prometheus_client",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from logai.alert.alert_state_machine import (
    AlertStateMachine,
    _coerce_group_id,
    _parse_group_id_key,
    group_id_key,
)
from logai.config import AlertConfig, AppConfig, FeatureConfig, MetricsConfig
from logai.features.feature_engine import FeatureEngine
from logai.metrics.prometheus_exporter import MetricsExporter
from logai.models import AlertStateEnum, AnomalyResult, AnomalyState
from logai.realtime.realtime_pipeline import RealtimePipeline
from logai.storage.base import JSONStore
from logai.storage.registries import GroupRegistry, TemplateRegistry


# --------------------------------------------------------------------------- #
# 1. FeatureEngine: one window per (service, group)
# --------------------------------------------------------------------------- #
class TestFeatureEnginePerServiceWindows(unittest.TestCase):
    def setUp(self):
        self.engine = FeatureEngine(FeatureConfig())

    def test_two_services_same_group_have_independent_windows(self):
        """Bursting service A must NOT inflate service B's baseline in the shared
        group G1 - the exact false-negative the refactor removes."""
        base = 1000.0
        for i in range(40):                      # A: burst of 40 within 2s
            self.engine.update(("A", "G1"), base + i * 0.05)
        fv_a = self.engine.update(("A", "G1"), base + 2.0)

        # snapshot() materializes the queried cell (defaultdict), so read the
        # live-key set BEFORE touching B.
        live = set(self.engine.live_window_keys())
        self.assertIn(("A", "G1"), live)
        self.assertNotIn(("B", "G1"), live)      # B never fed -> no window yet

        fv_b = self.engine.snapshot(("B", "G1"), base + 2.0)  # B: no events yet
        self.assertGreater(fv_a.count_1m, 0)
        self.assertEqual(fv_b.count_1m, 0)       # B's window is empty, not polluted
        self.assertEqual(fv_b.z_score_10s, 0.0)  # neutral, not A's spike

    def test_last_event_ts_and_live_window_keys(self):
        self.assertIsNone(self.engine.last_event_ts(("A", "G1")))  # .get must not create
        self.assertNotIn(("A", "G1"), list(self.engine.live_window_keys()))  # no materialize
        self.engine.update(("A", "G1"), 500.0)
        self.engine.update(("A", "G1"), 501.0)
        self.assertEqual(self.engine.last_event_ts(("A", "G1")), 501.0)
        self.assertIn(("A", "G1"), list(self.engine.live_window_keys()))
        self.assertIsNone(self.engine.last_event_ts(("C", "G9")))  # absent -> None


# --------------------------------------------------------------------------- #
# 2. Alert serialization seam (the one flatten boundary)
# --------------------------------------------------------------------------- #
class TestAlertKeySerialization(unittest.TestCase):
    def test_flatten_and_parse_roundtrip(self):
        key = group_id_key(("hdfs", "G0001"))
        self.assertEqual(key, json.dumps(["hdfs", "G0001"]))
        self.assertEqual(_parse_group_id_key(key), ("hdfs", "G0001"))

    def test_legacy_plain_string_key_survives(self):
        """An anomaly_state.json written before this change keys by bare group id.
        It must load without crashing and stay a string (orphan cell)."""
        self.assertEqual(group_id_key("G0001"), "G0001")
        self.assertEqual(_parse_group_id_key("G0001"), "G0001")
        self.assertEqual(_coerce_group_id("G0001"), "G0001")

    def test_coerce_from_list_shape(self):
        self.assertEqual(_coerce_group_id(["a", "G1"]), ("a", "G1"))
        self.assertEqual(_coerce_group_id(("a", "G1")), ("a", "G1"))

    def test_non_normal_is_tuple_after_reload(self):
        """GAP-3 REGRESSION. Persist an ALERTING cell, rebuild the state machine
        from the same JSONStore, and assert groups_not_normal() returns TUPLES that
        would match a FeatureEngine window key. If the reload left a flattened
        string, the idle-tick union would silently miss it and the alert would hang
        forever after restart."""
        tmp = tempfile.mkdtemp()
        try:
            store = JSONStore(f"{tmp}/anomaly_state.json")
            cfg = AlertConfig()
            machine = AlertStateMachine(cfg, store)
            key = ("hdfs", "G0001")
            # drive one cell into ALERTING through the public batch API
            high = AnomalyResult(
                group_id=key, timestamp=1000.0, anomaly_score=0.9, anomaly=True,
                count_1m=cfg.min_events_1m + 10,
            )
            for _ in range(cfg.alert_consecutive + cfg.warm_consecutive):
                machine.transition_batch([high])
            # a fresh machine re-reads the persisted file
            reloaded = AlertStateMachine(cfg, JSONStore(f"{tmp}/anomaly_state.json"))
            cells = reloaded.groups_not_normal()
            self.assertIn(key, cells)
            self.assertTrue(
                all(isinstance(c, tuple) for c in cells),
                f"groups_not_normal must return tuples, got {cells!r}",
            )
            # it is exactly the shape the idle tick unions against window keys
            self.assertEqual(set(cells), {key})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bulk_set_writes_flat_string_not_tuple(self):
        tmp = tempfile.mkdtemp()
        try:
            store = JSONStore(f"{tmp}/s.json")
            machine = AlertStateMachine(AlertConfig(), store)
            machine.transition_batch([AnomalyResult(
                group_id=("svc", "G1"), timestamp=1.0, anomaly_score=0.9,
                anomaly=True, count_1m=99)])
            raw = json.load(open(f"{tmp}/s.json"))
            self.assertEqual(list(raw.keys()), [json.dumps(["svc", "G1"])])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


    def test_alert_state_value_group_id_round_trips_as_tuple(self):
        """The persisted VALUE (not just the key) must recover a tuple group_id.
        json.dump(default=str) would otherwise mangle a tuple into "['a', 'b']"-as-
        string; _state_payload stores it as a real JSON array so _load restores the
        tuple and the state machine's own comparison stays consistent."""
        tmp = tempfile.mkdtemp()
        try:
            store = JSONStore(f"{tmp}/s.json")
            machine = AlertStateMachine(AlertConfig(), store)
            key = ("svc with, comma", "G1")
            machine.transition_batch([AnomalyResult(
                group_id=key, timestamp=1.0, anomaly_score=0.9, anomaly=True,
                count_1m=99)])
            # on disk, group_id is a JSON array, NOT a stringified tuple
            raw = json.load(open(f"{tmp}/s.json"))
            stored = raw[group_id_key(key)]
            self.assertEqual(stored["group_id"], list(key))
            # and reload recovers an exact tuple, comma-safe
            self.assertEqual(machine._load(key).group_id, key)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3. Idle tick: per-cell guard + restart-safe cooling
# --------------------------------------------------------------------------- #
class _StubEmbedder:
    def embed_one(self, _t):
        return [0.0]

    def embed(self, texts):
        return [[0.0] for _ in texts]


def _make_pipeline(tmp):
    cfg = AppConfig()
    cfg.storage.base_dir = tmp
    cfg.storage.model_dir = f"{tmp}/models"
    cfg.doc_matcher.corpus_path = f"{tmp}/missing.yaml"
    pipeline = RealtimePipeline(cfg)
    pipeline.embedder = _StubEmbedder()
    return pipeline, cfg


class TestIdleTickServiceWindows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pipeline, self.cfg = _make_pipeline(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idle_tick_snapshots_silent_service_in_chatty_group(self):
        """A cell that went silent inside a group whose OTHER service is still
        active must still be snapshotted (per-cell guard), or its ALERTING gauge
        hangs. This is the bug the old group-level last_seen would reintroduce."""
        fe = self.pipeline.feature_engine
        gap = self.cfg.alert.idle_eval_seconds
        chatty = ("hdfs", "G0001")
        quiet = ("yarn", "G0001")
        now = 10_000.0
        for _ in range(6):
            fe.update(chatty, now)               # chatty cell: fresh
        fe.update(quiet, now - 2 * gap)          # quiet cell: stale, same group

        self.pipeline.alert_sm.groups_not_normal = MagicMock(return_value=[quiet])
        fe.snapshot = MagicMock(return_value=MagicMock())
        self.pipeline._evaluate_idle_alerting_groups(now)

        snap_keys = [c.args[0] for c in fe.snapshot.call_args_list]
        self.assertIn(quiet, snap_keys)          # quiet cell WAS re-evaluated
        self.assertNotIn(chatty, snap_keys)      # chatty cell owned by per-event path

    def test_idle_tick_cools_alerting_cell_after_restart(self):
        """After a restart an ALERTING cell from the persisted alert set has no
        live window; it must still be snapshotted (empty -> neutral vector) so it
        cools down. last_event_ts is None -> not skipped."""
        fe = self.pipeline.feature_engine
        now = 99_000.0
        stranded = ("hdfs", "G_ALERTING")
        self.assertFalse(fe.last_event_ts(stranded))  # no window yet (post-restart)
        self.pipeline.alert_sm.groups_not_normal = MagicMock(return_value=[stranded])
        fe.snapshot = MagicMock(return_value=MagicMock())
        self.pipeline._evaluate_idle_alerting_groups(now)
        fe.snapshot.assert_any_call(stranded, now)
        self.assertIn(stranded, [k for k, _ in self.pipeline._pending_predictions])


# --------------------------------------------------------------------------- #
# 4. Metrics carry the service label
# --------------------------------------------------------------------------- #
class _RecordingMetric:
    def __init__(self, sink, name):
        self._sink, self._name = sink, name

    def labels(self, **kwargs):
        outer = self

        class _C:
            def set(self, v):
                outer._sink.setdefault(outer._name, []).append((kwargs, v))

            def inc(self, amount=1.0):
                outer._sink.setdefault(outer._name, []).append((kwargs, amount))

        return _C()


class TestMetricsServiceLabel(unittest.TestCase):
    def setUp(self):
        self.exporter = MetricsExporter(MetricsConfig())
        self.sink = {}
        for name in ("log_anomaly_score", "log_alert_state", "log_alerts_total"):
            setattr(self.exporter, name, _RecordingMetric(self.sink, name))

    def test_anomaly_score_label_has_service(self):
        grp = MagicMock(group_id="G0001", documented=True)
        self.exporter.set_anomaly_score(grp, 0.7, service="hdfs")
        (labels, val), = self.sink["log_anomaly_score"]
        self.assertEqual(labels, {"service": "hdfs", "group_id": "G0001", "documented": "true"})
        self.assertEqual(val, 0.7)

    def test_alert_state_unpacks_tuple_label(self):
        st = AnomalyState(
            group_id=("hdfs", "G0001"), timestamp=0.0,
            alert_state=AlertStateEnum.ALERTING.value,
        )
        self.exporter.set_alert_state(st)
        for labels, _ in self.sink["log_alert_state"]:
            self.assertEqual(labels["service"], "hdfs")
            self.assertEqual(labels["group_id"], "G0001")
        escalations = self.sink.get("log_alerts_total", [])
        self.assertTrue(any(l["service"] == "hdfs" for l, _ in escalations))


if __name__ == "__main__":
    unittest.main()

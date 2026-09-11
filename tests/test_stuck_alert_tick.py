"""Fix #5: a stuck ALERTING alert must cool down to NORMAL on its own once a
group stops emitting logs (zero-event), driven by a periodic idle tick that
re-evaluates non-NORMAL groups via FeatureEngine.snapshot().

Root cause guarded here: anomaly/alert used to be evaluated only on the
event-path (`_process_one` -> `_run_anomaly_and_alert`). When an incident ends
and a group goes silent, no event ever re-enters the pipeline for it, so the
sliding window never decays to rate 0 and the `log_alert_state{state=ALERTING}`
gauge stays pinned at 1.0 forever.

Follows the suite convention of stubbing heavy optional drivers. A LOCAL
MockIsolationForest keyed on z_score_10s / short_growth_rate (NOT spike_ratio)
scores idle cold-start snapshots as normal, so cooldown converges in a few ticks.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

for mod in [
    "numpy",
    "sklearn",
    "sklearn.ensemble",
    "hdbscan",
    "elasticsearch",
    "sentence_transformers",
    "drain3",
    "drain3.template_miner",
    "drain3.file_persistence",
    "drain3.template_miner_config",
    "drain3.masking",
    "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


class MockArray:
    def __init__(self, data):
        self._data = data
        if len(data) > 0 and isinstance(data[0], (list, tuple)):
            self.ndim = 2
            self.shape = (len(data), len(data[0]))
        else:
            self.ndim = 1
            self.shape = (len(data),)

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def __getitem__(self, idx):
        return self._data[idx]


class MockNumpy:
    float64 = float
    zeros = staticmethod(
        lambda shape: MockArray(
            [[0.0] * shape[1] for _ in range(shape[0])] if len(shape) == 2 else [0.0] * shape[0]
        )
    )
    array = staticmethod(lambda data, dtype=None: MockArray(data))


sys.modules["numpy"] = MockNumpy()


class MockIsolationForest:
    """Scores a vector as an anomaly when the short-term burst signals fire.

    Keyed on z_score_10s (row[0]) and short_growth_rate (row[2]) ONLY - never
    spike_ratio - so an idle cold-start snapshot (z=0, short_growth=1.0) reads
    as normal and lets a stuck alert cool down.
    """

    def __init__(self, *args, **kwargs):
        self.n_features_in_ = 8

    def fit(self, X):
        self.n_features_in_ = len(X[0]) if len(X) > 0 else 8
        return self

    def decision_function(self, X):
        scores = []
        for row in X:
            z_10s = row[0]
            short_growth = row[2]
            if z_10s > 2.0 or short_growth > 2.0:
                scores.append(-0.35)  # anomaly -> anomaly_score = 0.85
            else:
                scores.append(0.25)   # normal  -> anomaly_score = 0.25
        return scores

    def predict(self, X):
        return [-1 if s < 0 else 1 for s in self.decision_function(X)]


sys.modules["sklearn.ensemble"].IsolationForest = MockIsolationForest

from logai.config import AppConfig
from logai.models import AlertStateEnum, FeatureVector, GroupState
from logai.realtime.realtime_pipeline import RealtimePipeline


def _fv(group_id: str, timestamp: float, z_score_10s: float = 0.0) -> FeatureVector:
    """A minimal feature vector; z_score_10s drives the mock anomaly decision."""
    return FeatureVector(
        group_id=group_id,
        timestamp=timestamp,
        z_score_10s=z_score_10s,
        z_score_1m=0.0,
        short_growth_rate=1.0,
        growth_rate=1.0,
        burstiness_10s=0.0,
        rate_delta_norm=0.0,
        slope_norm=0.0,
        spike_ratio_10s=1.0,
    )


class TestStuckAlertIdleTick(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.temp_dir
        self.cfg.storage.model_dir = f"{self.temp_dir}/models"
        self.cfg.doc_matcher.corpus_path = f"{self.temp_dir}/missing.yaml"

        self.pipeline = RealtimePipeline(self.cfg)
        self.pipeline.start_metrics_server = MagicMock()
        self.pipeline.anomaly_model._model = MockIsolationForest()

        # A pre-existing group last seen at t=1000.
        self.pipeline.group_registry.upsert(
            GroupState(group_id="G_AUTH", service="auth", first_seen=1000.0, last_seen=1000.0)
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _drive_to_alerting(self, group_id: str) -> None:
        """Feed high-scoring feature vectors until the group reaches ALERTING.

        Scoring is batched now (TODO #7): buffer the vectors then flush once,
        which scores them and applies the transitions in order."""
        for i in range(5):
            self.pipeline._pending_predictions.append(
                (group_id, _fv(group_id, 1000.0 + i, z_score_10s=5.0))
            )
        self.pipeline._flush_predictions()
        self.assertEqual(
            self.pipeline.alert_sm._load(group_id).alert_state,
            AlertStateEnum.ALERTING.value,
        )
        self.assertIn(group_id, self.pipeline.alert_sm.groups_not_normal())

    def test_alert_cools_down_when_events_stop(self):
        """A stuck ALERTING group with no further events cools to NORMAL and the
        alert-state gauge bookkeeping follows it down."""
        self._drive_to_alerting("G_AUTH")

        # No new events - only the periodic idle tick runs, well past the
        # silence guard. Each tick appends a cold-start snapshot to the buffer;
        # flushing scores it (normal) and cools the state machine down.
        for t in (9000.0, 9010.0, 9020.0, 9030.0):
            self.pipeline._evaluate_idle_alerting_groups(t)
            self.pipeline._flush_predictions()

        self.assertEqual(
            self.pipeline.alert_sm._load("G_AUTH").alert_state,
            AlertStateEnum.NORMAL.value,
        )
        # set_alert_state's per-group bookkeeping recorded the recovery.
        self.assertEqual(
            self.pipeline.metrics._last_alert_state["G_AUTH"],
            AlertStateEnum.NORMAL.value,
        )
        # Self-terminating: once NORMAL, the group leaves the non-NORMAL set.
        self.assertNotIn("G_AUTH", self.pipeline.alert_sm.groups_not_normal())

    def test_idle_groups_pruned_to_zero_rate(self):
        """snapshot() past the retention horizon empties the window and returns
        the neutral cold-start vector (z_score_10s == 0.0)."""
        for ts in (1000.0, 1000.5, 1001.0, 1001.5):
            self.pipeline.feature_engine.update("G_AUTH", ts)

        far_future = 1000.0 + self.cfg.features.history_retention_seconds + 5000.0
        fv = self.pipeline.feature_engine.snapshot("G_AUTH", far_future)
        self.assertEqual(fv.z_score_10s, 0.0)

    def test_normal_idle_group_is_snapshotted(self):
        """A NORMAL group that has gone silent IS snapshotted so its state is
        re-evaluated each idle interval (TODO #7 extends the idle tick to NORMAL
        groups, not just non-NORMAL ones), and the vector joins the same buffer
        as live events."""
        # G_AUTH is untouched -> NORMAL, but last_seen=1000 is well past the
        # silence guard relative to t=9000.
        self.assertEqual(self.pipeline.alert_sm.groups_not_normal(), [])
        with patch.object(
            self.pipeline.feature_engine, "snapshot",
            return_value=_fv("G_AUTH", 9000.0),
        ) as snap:
            self.pipeline._evaluate_idle_alerting_groups(9000.0)
            snap.assert_called_once_with("G_AUTH", 9000.0)
        self.assertEqual(len(self.pipeline._pending_predictions), 1)
        self.assertEqual(self.pipeline._pending_predictions[0][0], "G_AUTH")

    def test_active_group_skipped_by_guard(self):
        """A non-NORMAL group that is still receiving logs (last_seen within the
        idle threshold of now) is skipped - the per-event path owns it."""
        self._drive_to_alerting("G_AUTH")

        group = self.pipeline.group_registry.get("G_AUTH")
        now = 2000.0
        group.last_seen = now - (self.cfg.alert.idle_eval_seconds / 2.0)  # < threshold
        self.pipeline.group_registry.upsert(group)

        with patch.object(self.pipeline.feature_engine, "snapshot") as snap:
            self.pipeline._evaluate_idle_alerting_groups(now)
            snap.assert_not_called()

    def test_tick_throttled_to_interval(self):
        """_maybe_tick_idle runs the evaluation at most once per idle_eval_seconds."""
        calls = []
        with patch.object(
            self.pipeline, "_evaluate_idle_alerting_groups", side_effect=lambda t: calls.append(t)
        ):
            self.pipeline._maybe_tick_idle(1000.0)          # first ever -> runs
            self.pipeline._maybe_tick_idle(1001.0)          # within interval -> skipped
            self.pipeline._maybe_tick_idle(
                1000.0 + self.cfg.alert.idle_eval_seconds + 0.1
            )                                               # past interval -> runs
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

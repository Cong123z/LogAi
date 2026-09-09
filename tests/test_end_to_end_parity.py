"""End-to-end pipeline test verifying 8 features and per-event realtime detection parity."""
import sys
import unittest
from unittest.mock import MagicMock

class MockIsolationForest:
    def __init__(self, n_estimators=100, contamination=0.05, random_state=42):
        self.n_estimators = n_estimators
        self.contamination = contamination
        self.random_state = random_state
        self.n_features_in_ = None

    def fit(self, X):
        self.n_features_in_ = X.shape[1]
        return self

    def decision_function(self, X):
        scores = []
        for row in X:
            z_10s = row[0]
            # When z_score_10s exceeds 2.0, flag as anomaly (negative decision score)
            if z_10s >= 2.0:
                scores.append(-0.25)
            else:
                scores.append(0.35)
        return scores

    def predict(self, X):
        return [-1 if s < 0 else 1 for s in self.decision_function(X)]

class MockNumpy:
    float64 = float

    @staticmethod
    def array(data, dtype=None):
        import numpy_mock_helper
        return numpy_mock_helper.MockArray(data)

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

import types
helper_mod = types.ModuleType("numpy_mock_helper")
helper_mod.MockArray = MockArray
sys.modules["numpy_mock_helper"] = helper_mod

mock_np = MockNumpy()
mock_sklearn = MagicMock()
mock_sklearn_ensemble = MagicMock()
mock_sklearn_ensemble.IsolationForest = MockIsolationForest
mock_sklearn.ensemble = mock_sklearn_ensemble

sys.modules["numpy"] = mock_np
sys.modules["sklearn"] = mock_sklearn
sys.modules["sklearn.ensemble"] = mock_sklearn_ensemble

from logai.config import FeatureConfig, AnomalyConfig, AlertConfig
from logai.features.feature_engine import FeatureEngine
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel, MODEL_VERSION, EXPECTED_NUM_FEATURES
from logai.alert.alert_state_machine import AlertStateMachine
from logai.models import FeatureVector, AlertStateEnum, AnomalyResult, AnomalyState


class TestEndToEndParityAndRealtimeAlert(unittest.TestCase):
    def setUp(self):
        self.feat_cfg = FeatureConfig(
            windows_seconds=(10, 60, 300),
            rolling_window_points=30,
            history_retention_seconds=3600.0,
        )
        self.anomaly_cfg = AnomalyConfig(
            contamination=0.05,
            n_estimators=100,
            min_training_samples=10,
            score_alert_threshold=0.6,
        )
        self.alert_cfg = AlertConfig(
            warm_consecutive=2,
            alert_consecutive=3,
            cool_consecutive=3,
            score_high=0.6,
            score_low=0.4,
        )

        self.model_store = MagicMock()
        self.model_store_data = {}
        self.model_store.save.side_effect = lambda k, v: self.model_store_data.update({k: v})
        self.model_store.load.side_effect = lambda k: self.model_store_data.get(k)
        self.model_store.exists.side_effect = lambda k: k in self.model_store_data

        self.alert_store = MagicMock()
        self.alert_store_data = {}
        self.alert_store.get.side_effect = lambda k: self.alert_store_data.get(k)
        self.alert_store.set.side_effect = lambda k, v: self.alert_store_data.update({k: v})

    def test_train_and_realtime_parity_with_8_features_per_event(self):
        """Verify:
        1. Training pipeline extracts 8D vectors per-event and fits Global model.
        2. Realtime pipeline extracts 8D vectors per-event and immediately detects anomalies.
        3. Alert state machine transitions NORMAL -> WARMING -> ALERTING in real time.
        """
        # ==========================================
        # 1. TRAINING PHASE (Per-event 8D extraction)
        # ==========================================
        train_engine = FeatureEngine(self.feat_cfg)
        train_vectors = []
        base_ts = 10000.0

        # Simulate normal steady traffic for 70 events (1 event per second)
        for i in range(70):
            fv = train_engine.update("G_PAYMENT", base_ts + i)
            self.assertEqual(len(fv.as_vector()), 8)
            train_vectors.append(fv)

        # Train GlobalAnomalyModel with the 8D vectors
        model = GlobalAnomalyModel(self.anomaly_cfg, self.model_store)
        trained = model.train(train_vectors)
        self.assertTrue(trained)
        self.assertEqual(model._model.n_features_in_, 8)
        self.assertEqual(MODEL_VERSION, "if-global-v2")

        # ==========================================
        # 2. REALTIME PHASE (Per-event immediate evaluation)
        # ==========================================
        realtime_engine = FeatureEngine(self.feat_cfg)
        realtime_model = GlobalAnomalyModel(self.anomaly_cfg, self.model_store)
        alert_sm = AlertStateMachine(self.alert_cfg, self.alert_store)

        rt_base = 20000.0

        # Feed 70 steady events: Once baseline settles, alert state should be NORMAL
        for i in range(70):
            fv = realtime_engine.update("G_PAYMENT", rt_base + i)
            vec = fv.as_vector()
            self.assertEqual(len(vec), 8)

            result = realtime_model.predict(fv)
            self.assertIsNotNone(result)
            self.assertEqual(result.model_version, "if-global-v2")

            state = alert_sm.transition(result)
            # After 60 seconds of steady traffic, z_10s is around 0.0 and state is NORMAL
            if i >= 60:
                self.assertFalse(result.anomaly)
                self.assertEqual(state.alert_state, AlertStateEnum.NORMAL.value)

        # ==========================================
        # 3. REALTIME MICRO-BURST (Immediate Anomaly & Alerting)
        # ==========================================
        # Sudden error storm: 20 events in 1 second immediately following steady traffic
        burst_ts = rt_base + 71.0
        final_state = None

        for j in range(20):
            ts = burst_ts + (j * 0.05)
            # Per-event update
            fv = realtime_engine.update("G_PAYMENT", ts)

            # Per-event prediction
            result = realtime_model.predict(fv)

            # Per-event alert state transition
            final_state = alert_sm.transition(result)

        # Confirm that the alert machine reached ALERTING in real time
        self.assertEqual(final_state.alert_state, AlertStateEnum.ALERTING.value)

        # ==========================================
        # 4. TRAIN/SERVE PARITY CHECK
        # ==========================================
        # Same sequence of 5 events starting at 50000.0 must yield identical vectors
        test_engine_1 = FeatureEngine(self.feat_cfg)
        test_engine_2 = FeatureEngine(self.feat_cfg)
        stream = [50000.0, 50001.0, 50002.5, 50003.0, 50004.2]

        for t in stream:
            v1 = test_engine_1.update("G_CHECK", t).as_vector()
            v2 = test_engine_2.update("G_CHECK", t).as_vector()
            self.assertEqual(v1, v2)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for 8-dimensional FeatureVector and FeatureEngine."""
import unittest
from logai.config import FeatureConfig
from logai.features.feature_engine import FeatureEngine
from logai.models import FeatureVector


class TestFeatureVector8D(unittest.TestCase):
    def setUp(self):
        self.config = FeatureConfig(
            windows_seconds=(10, 60, 300),
            rolling_window_points=30,
            history_retention_seconds=3600.0,
        )
        self.engine = FeatureEngine(self.config)

    def test_feature_vector_schema_and_names(self):
        names = FeatureVector.feature_names()
        self.assertEqual(len(names), 8)
        self.assertEqual(
            names,
            [
                "z_score_10s",
                "z_score_1m",
                "short_growth_rate",
                "growth_rate",
                "burstiness_10s",
                "rate_delta_norm",
                "slope_norm",
                "spike_ratio_10s",
            ],
        )

        fv = FeatureVector(group_id="G001", timestamp=1000.0)
        vec = fv.as_vector()
        self.assertEqual(len(vec), 8)

    def test_cold_start_neutral_baseline(self):
        """First event should return neutral baseline [0, 0, 1, 1, 0, 0, 0, 1]."""
        fv = self.engine.update("G001", 1000.0)
        self.assertEqual(
            fv.as_vector(),
            [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        )

    def test_window_maxlen_matches_config(self):
        """_GroupWindow should respect rolling_window_points from config."""
        # Touch window
        self.engine.update("G001", 1000.0)
        gw = self.engine._windows["G001"]
        self.assertEqual(gw.rate_10s_history.maxlen, 30)
        self.assertEqual(gw.rate_1m_history.maxlen, 30)

    def test_steady_stream(self):
        """A steady stream of 1 event every second should produce near-constant rates."""
        base_t = 1000.0
        # Feed 65 events over 65 seconds
        fv = None
        for i in range(65):
            fv = self.engine.update("G001", base_t + i)

        vec = fv.as_vector()
        self.assertEqual(len(vec), 8)
        # short_growth_rate = rate_10s / rate_1m = (10/10) / (60/60) = 1.0
        self.assertAlmostEqual(fv.short_growth_rate, 1.0, delta=0.15)
        # growth_rate = rate_1m / rate_5m = (60/60) / (65/300) -> positive
        self.assertGreater(fv.growth_rate, 0.0)

    def test_micro_burst_detection(self):
        """Micro-burst in 10s should trigger high short_growth_rate and z_score_10s."""
        base_t = 1000.0
        # First 60 seconds: low baseline of 1 event every 10 seconds (total 6 events)
        for i in range(6):
            self.engine.update("G001", base_t + i * 10)

        # Micro-burst: 30 events in 3 seconds
        burst_t = base_t + 70.0
        last_fv = None
        for j in range(30):
            last_fv = self.engine.update("G001", burst_t + (j * 0.1))

        # Under burst, rate_10s should be high (30/10 = 3.0 logs/s)
        # while rate_1m was previously low
        self.assertGreater(last_fv.short_growth_rate, 1.5)
        self.assertGreater(last_fv.spike_ratio_10s, 1.5)

    def test_clipping_bounds(self):
        """Values should strictly respect numerical clipping bounds."""
        base_t = 1000.0
        # Extreme scenario: 200 events at the exact same millisecond
        fv = None
        for _ in range(200):
            fv = self.engine.update("G_EXTREME", base_t)

        self.assertGreaterEqual(fv.z_score_10s, -10.0)
        self.assertLessEqual(fv.z_score_10s, 10.0)
        self.assertGreaterEqual(fv.z_score_1m, -10.0)
        self.assertLessEqual(fv.z_score_1m, 10.0)
        self.assertGreaterEqual(fv.short_growth_rate, 0.0)
        self.assertLessEqual(fv.short_growth_rate, 6.0)
        self.assertGreaterEqual(fv.growth_rate, 0.0)
        self.assertLessEqual(fv.growth_rate, 5.0)
        self.assertGreaterEqual(fv.burstiness_10s, 0.0)
        self.assertLessEqual(fv.burstiness_10s, 20.0)
        self.assertGreaterEqual(fv.rate_delta_norm, -10.0)
        self.assertLessEqual(fv.rate_delta_norm, 10.0)
        self.assertGreaterEqual(fv.slope_norm, -10.0)
        self.assertLessEqual(fv.slope_norm, 10.0)
        self.assertGreaterEqual(fv.spike_ratio_10s, 0.0)
        self.assertLessEqual(fv.spike_ratio_10s, 20.0)


if __name__ == "__main__":
    unittest.main()

import sys
from unittest.mock import MagicMock

# Mock numpy and sklearn for testing GlobalAnomalyModel without external dependencies
mock_np = MagicMock()
mock_sklearn = MagicMock()
mock_ensemble = MagicMock()
sys.modules["numpy"] = mock_np
sys.modules["sklearn"] = mock_sklearn
sys.modules["sklearn.ensemble"] = mock_ensemble

from logai.anomaly.isolation_forest_model import (
    GlobalAnomalyModel,
    MODEL_VERSION,
    EXPECTED_NUM_FEATURES,
)
from logai.config import AnomalyConfig


class TestGlobalAnomalyModel8D(unittest.TestCase):
    def setUp(self):
        self.config = AnomalyConfig(min_training_samples=5)
        self.store = MagicMock()
        self.anomaly_model = GlobalAnomalyModel(self.config, self.store)

    def test_model_version_and_expected_features(self):
        self.assertEqual(MODEL_VERSION, "if-global-v2")
        self.assertEqual(EXPECTED_NUM_FEATURES, 8)

    def test_predict_rejects_dimension_mismatch(self):
        mock_forest = MagicMock()
        mock_forest.n_features_in_ = 6  # Old 6D model loaded
        self.anomaly_model._model = mock_forest

        fv = FeatureVector(group_id="G001", timestamp=1000.0)
        # Should gracefully return None instead of crashing
        res = self.anomaly_model.predict(fv)
        self.assertIsNone(res)

    def test_predict_success_8d(self):
        mock_forest = MagicMock()
        mock_forest.n_features_in_ = 8
        mock_forest.decision_function.return_value = [0.2]
        mock_forest.predict.return_value = [1]
        self.anomaly_model._model = mock_forest

        fv = FeatureVector(group_id="G001", timestamp=1000.0)
        res = self.anomaly_model.predict(fv)
        self.assertIsNotNone(res)
        self.assertEqual(res.model_version, "if-global-v2")
        self.assertEqual(res.group_id, "G001")



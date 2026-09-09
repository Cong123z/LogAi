"""Global Isolation Forest anomaly detection (Issue 1)."""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
from sklearn.ensemble import IsolationForest

from logai.config import AnomalyConfig
from logai.models import AnomalyResult, FeatureVector
from logai.storage.base import ModelStore

logger = logging.getLogger(__name__)
MODEL_VERSION = "if-global-v2"
GLOBAL_MODEL_KEY = "global"
EXPECTED_NUM_FEATURES = 8


class GlobalAnomalyModel:
    """Owns a single global IsolationForest trained on dimensionless feature vectors
    across all log groups, persisted via ModelStore."""

    def __init__(self, config: AnomalyConfig, model_store: ModelStore):
        self.config = config
        self.model_store = model_store
        self._model: Optional[IsolationForest] = None

    def train(self, feature_vectors: List[FeatureVector]) -> bool:
        """Fit one global Isolation Forest on dimensionless feature vectors from all groups."""
        if len(feature_vectors) < self.config.min_training_samples:
            logger.warning(
                "Not enough samples to train global model: %d < %d",
                len(feature_vectors),
                self.config.min_training_samples,
            )
            return False

        X = np.array([fv.as_vector() for fv in feature_vectors], dtype=np.float64)
        if X.ndim != 2 or X.shape[1] != EXPECTED_NUM_FEATURES:
            logger.error(
                "Invalid feature dimensions for training: expected shape (*, %d), got %s",
                EXPECTED_NUM_FEATURES,
                X.shape,
            )
            return False

        model = IsolationForest(
            n_estimators=self.config.n_estimators,
            contamination=self.config.contamination,
            random_state=self.config.random_state,
        )
        model.fit(X)
        self._model = model
        self.model_store.save(GLOBAL_MODEL_KEY, model)
        logger.info(
            "Trained global Isolation Forest on %d samples across groups.", len(feature_vectors)
        )
        return True

    def _load(self) -> Optional[IsolationForest]:
        if self._model is not None:
            return self._model
        model = self.model_store.load(GLOBAL_MODEL_KEY)
        if model is not None:
            self._model = model
        return model

    def has_model(self, group_id: Optional[str] = None) -> bool:
        """Check if global model exists in memory or store."""
        return self._model is not None or self.model_store.exists(GLOBAL_MODEL_KEY)

    def train_group(self, group_id: str, feature_vectors: List[FeatureVector]) -> bool:
        """Backward compatibility: delegates to train()."""
        return self.train(feature_vectors)

    def predict(self, feature_vector: FeatureVector) -> Optional[AnomalyResult]:
        model = self._load()
        if model is None:
            return None
        vec = feature_vector.as_vector()
        if len(vec) != EXPECTED_NUM_FEATURES:
            logger.error(
                "Feature vector dimension mismatch: expected %d, got %d",
                EXPECTED_NUM_FEATURES,
                len(vec),
            )
            return None
        if hasattr(model, "n_features_in_") and model.n_features_in_ != EXPECTED_NUM_FEATURES:
            logger.warning(
                "Loaded model expects %d features, but feature vector has %d. Please retrain model.",
                model.n_features_in_,
                EXPECTED_NUM_FEATURES,
            )
            return None

        X = np.array([vec], dtype=np.float64)
        # decision_function: higher = more normal. We flip + normalize to a
        # 0..1 "anomaly score" so it reads naturally as a Prometheus gauge.
        raw_score = float(model.decision_function(X)[0])
        anomaly_score = max(0.0, min(1.0, 0.5 - raw_score))
        is_outlier = model.predict(X)[0] == -1
        anomaly = is_outlier or anomaly_score >= self.config.score_alert_threshold
        return AnomalyResult(
            group_id=feature_vector.group_id,
            timestamp=feature_vector.timestamp,
            anomaly_score=anomaly_score,
            anomaly=anomaly,
            model_version=MODEL_VERSION,
        )


# Backward compatibility alias
GroupAnomalyModels = GlobalAnomalyModel

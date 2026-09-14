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
MODEL_VERSION = "if-global-v3"
GLOBAL_MODEL_KEY = "global_v3"
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
        """Single-event convenience wrapper over predict_batch().

        Kept for callers/tests that score one vector at a time; the realtime
        pipeline batches events and calls predict_batch() directly.
        """
        return self.predict_batch([feature_vector])[0]

    def predict_batch(
        self, feature_vectors: List[FeatureVector]
    ) -> List[Optional[AnomalyResult]]:
        """Score a batch of feature vectors in a single vectorized inference.

        Returns a list aligned 1:1 with `feature_vectors` (same order, same
        length). An element is None when the model is not yet trained, when the
        loaded model's feature count is incompatible, or when that specific
        vector has the wrong dimension - preserving predict()'s semantics.

        Batching is result-equivalent to per-event scoring: decision_function
        is a pure function of each row, and `is_outlier = raw_score < 0` is
        exactly IsolationForest.predict()==-1, so the redundant predict() call
        is dropped (one tree traversal instead of two).
        """
        n = len(feature_vectors)
        if n == 0:
            return []

        model = self._load()
        if model is None:
            return [None] * n  # global model not yet trained
        if hasattr(model, "n_features_in_") and model.n_features_in_ != EXPECTED_NUM_FEATURES:
            logger.warning(
                "Loaded model expects %d features, but feature vectors have %d. "
                "Please retrain model.",
                model.n_features_in_,
                EXPECTED_NUM_FEATURES,
            )
            return [None] * n

        # Collect only correctly-dimensioned vectors, remembering their
        # original positions so None can be slotted back for the bad ones.
        rows: List[List[float]] = []
        valid_indices: List[int] = []
        for i, fv in enumerate(feature_vectors):
            vec = fv.as_vector()
            if len(vec) != EXPECTED_NUM_FEATURES:
                logger.error(
                    "Feature vector dimension mismatch: expected %d, got %d",
                    EXPECTED_NUM_FEATURES,
                    len(vec),
                )
                continue
            rows.append(vec)
            valid_indices.append(i)

        results: List[Optional[AnomalyResult]] = [None] * n
        if not rows:
            return results

        X = np.array(rows, dtype=np.float64)
        # decision_function: higher = more normal. One vectorized call scores
        # every row in a single pass over the forest (the batched speed-up); we
        # then flip + normalize each score to a 0..1 "anomaly score" so it reads
        # naturally as a Prometheus gauge. `raw_score < 0` is exactly
        # IsolationForest.predict()==-1, so the redundant predict() is dropped.
        raw_scores = model.decision_function(X)
        threshold = self.config.score_alert_threshold
        for j, i in enumerate(valid_indices):
            raw = float(raw_scores[j])
            score = max(0.0, min(1.0, 0.5 - raw))
            fv = feature_vectors[i]
            results[i] = AnomalyResult(
                group_id=fv.group_id,
                timestamp=fv.timestamp,
                anomaly_score=score,
                anomaly=raw < 0 or score >= threshold,
                model_version=MODEL_VERSION,
                count_1m=fv.count_1m,
            )
        return results


# Backward compatibility alias
GroupAnomalyModels = GlobalAnomalyModel

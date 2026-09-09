"""HDBSCAN clustering of template embeddings into semantic groups
(plan section 3.5), plus centroid computation (3.6) and realtime
nearest-centroid assignment for unknown templates (4.4)."""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

try:
    import hdbscan
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "hdbscan is required: pip install hdbscan"
    ) from exc

from logai.config import ClusteringConfig

NOISE_LABEL = -1


class GroupClusterer:
    def __init__(self, config: ClusteringConfig):
        self.config = config

    def cluster(
        self, template_ids: List[str], embeddings: np.ndarray
    ) -> Dict[str, int]:
        """Run HDBSCAN over all template embeddings.

        Returns: template_id -> cluster label (int). Noise points (-1) are
        each assigned their own singleton group later by the caller.
        """
        if len(template_ids) == 0:
            return {}
        if len(template_ids) == 1:
            return {template_ids[0]: 0}

        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=max(2, self.config.min_cluster_size),
            min_samples=self.config.min_samples,
            metric=self.config.metric,
        )
        labels = clusterer.fit_predict(embeddings)
        return dict(zip(template_ids, labels.tolist()))

    @staticmethod
    def compute_centroid(embeddings: np.ndarray) -> np.ndarray:
        centroid = embeddings.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        return centroid

    def assign_to_nearest_group(
        self, embedding: np.ndarray, group_centroids: Dict[str, np.ndarray]
    ) -> Tuple[str | None, float]:
        """Cosine similarity (dot product, embeddings assumed normalized)
        against every known group centroid. Returns (group_id, similarity)
        or (None, best_similarity) if nothing clears the threshold - the
        caller should treat that as Unknown/Pending (plan 4.4)."""
        if not group_centroids:
            return None, 0.0
        best_group, best_sim = None, -1.0
        for group_id, centroid in group_centroids.items():
            sim = float(np.dot(embedding, centroid))
            if sim > best_sim:
                best_group, best_sim = group_id, sim
        if best_sim >= self.config.assignment_similarity_threshold:
            return best_group, best_sim
        return None, best_sim

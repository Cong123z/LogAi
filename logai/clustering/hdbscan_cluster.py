"""HDBSCAN clustering of template embeddings into semantic groups
(plan section 3.5), plus centroid computation (3.6) and realtime
nearest-centroid assignment for unknown templates (4.4)."""
from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

try:
    import hdbscan
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "hdbscan is required: pip install hdbscan"
    ) from exc

from logai.config import ClusteringConfig

NOISE_LABEL = -1

# HDBSCAN holds the GIL for its whole fit, O(n^2) on 1024-d vectors (8k
# templates: ~60 s, ~27k: ~12 min on one core). In-process that freezes every
# engine thread: the retrain heartbeat goes stale (the web shows the engine as
# unavailable) and the metrics port stops answering. From this size the fit
# runs in a child process instead.
SUBPROCESS_MIN_TEMPLATES = 2000
WAIT_REPORT_SECONDS = 30.0


def _fit_labels(embeddings: np.ndarray, min_cluster_size: int, min_samples: int, metric: str):
    return hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size, min_samples=min_samples, metric=metric,
    ).fit_predict(embeddings)


class GroupClusterer:
    def __init__(self, config: ClusteringConfig):
        self.config = config

    def cluster(
        self, template_ids: List[str], embeddings: np.ndarray,
        on_wait: Optional[Callable[[float], None]] = None,
    ) -> Dict[str, int]:
        """Run HDBSCAN over all template embeddings.

        Returns: template_id -> cluster label (int). Noise points (-1) are
        each assigned their own singleton group later by the caller.
        `on_wait(elapsed_seconds)` is called every WAIT_REPORT_SECONDS while
        a large fit runs in its child process.
        """
        if len(template_ids) == 0:
            return {}
        if len(template_ids) == 1:
            return {template_ids[0]: 0}

        args = (
            embeddings, max(2, self.config.min_cluster_size),
            self.config.min_samples, self.config.metric,
        )
        if len(template_ids) < SUBPROCESS_MIN_TEMPLATES:
            return dict(zip(template_ids, _fit_labels(*args).tolist()))
        # spawn, not fork: forking a process with live threads can inherit a
        # held lock (logging, stores) and deadlock the child.
        started = time.time()
        with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
            future = pool.submit(_fit_labels, *args)
            while True:
                try:
                    labels = future.result(timeout=WAIT_REPORT_SECONDS)
                    break
                except FutureTimeout:
                    if on_wait is not None:
                        on_wait(time.time() - started)
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

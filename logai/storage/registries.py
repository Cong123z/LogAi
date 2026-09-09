"""Template Registry, Group Registry, Group Centroid Registry and
Documentation Registry - the persistent artifacts produced by training and
consumed by the realtime pipeline (plan section 3.3 / 3.6 / 3.7 / 6).
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from logai.config import StorageConfig
from logai.models import TemplateState, GroupState
from logai.storage.base import JSONStore, PickleStore


class TemplateRegistry:
    """template_id -> TemplateState (metadata) + template_id -> embedding."""

    def __init__(self, storage: StorageConfig):
        base = Path(storage.base_dir)
        self._meta = JSONStore(base / storage.template_registry_file)
        self._embeddings = PickleStore(base / storage.template_embeddings_file)
        self._embedding_cache: Dict[str, np.ndarray] = self._embeddings.load({})

    def get(self, template_id: str) -> Optional[TemplateState]:
        raw = self._meta.get(template_id)
        return TemplateState(**raw) if raw else None

    def upsert(self, state: TemplateState, flush: bool = True) -> None:
        self._meta.set(state.template_id, asdict(state), flush=flush)

    def set_embedding(self, template_id: str, embedding: np.ndarray) -> None:
        self._embedding_cache[template_id] = embedding
        self._embeddings.save(self._embedding_cache)

    def get_embedding(self, template_id: str) -> Optional[np.ndarray]:
        return self._embedding_cache.get(template_id)

    def all_templates(self) -> List[TemplateState]:
        return [TemplateState(**v) for v in self._meta.all().values()]

    def all_embeddings(self) -> Dict[str, np.ndarray]:
        return dict(self._embedding_cache)

    def set_group(self, template_id: str, group_id: str) -> None:
        state = self.get(template_id)
        if state:
            state.group_id = group_id
            self.upsert(state)

    def flush(self) -> None:
        self._meta.flush()
        self._embeddings.save(self._embedding_cache)


class GroupRegistry:
    """group_id -> GroupState (metadata) + group_id -> centroid embedding."""

    def __init__(self, storage: StorageConfig):
        base = Path(storage.base_dir)
        self._meta = JSONStore(base / storage.group_registry_file)
        self._centroids = PickleStore(base / storage.group_centroids_file)
        self._centroid_cache: Dict[str, np.ndarray] = self._centroids.load({})

    def get(self, group_id: str) -> Optional[GroupState]:
        raw = self._meta.get(group_id)
        return GroupState(**raw) if raw else None

    def upsert(self, state: GroupState, flush: bool = True) -> None:
        self._meta.set(state.group_id, asdict(state), flush=flush)

    def set_centroid(self, group_id: str, centroid: np.ndarray) -> None:
        self._centroid_cache[group_id] = centroid
        self._centroids.save(self._centroid_cache)

    def get_centroid(self, group_id: str) -> Optional[np.ndarray]:
        return self._centroid_cache.get(group_id)

    def all_groups(self) -> List[GroupState]:
        return [GroupState(**v) for v in self._meta.all().values()]

    def all_centroids(self) -> Dict[str, np.ndarray]:
        return dict(self._centroid_cache)

    def flush(self) -> None:
        self._meta.flush()
        self._centroids.save(self._centroid_cache)

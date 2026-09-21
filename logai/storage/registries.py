"""Template Registry, Group Registry, Group Centroid Registry and
Documentation Registry - the persistent artifacts produced by training and
consumed by the realtime pipeline (plan section 3.3 / 3.6 / 3.7 / 6).
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import threading
from typing import Dict, List, Optional

import numpy as np

from logai.config import StorageConfig
from logai.models import TemplateState, GroupState
from logai.storage.base import JSONStore, PickleStore


class TemplateRegistry:
    """template_id -> TemplateState (metadata) + template_id -> embedding."""

    def __init__(self, storage: StorageConfig):
        base = Path(storage.base_dir)
        self._lock = threading.RLock()
        self._meta = JSONStore(base / storage.template_registry_file)
        self._embeddings = PickleStore(base / storage.template_embeddings_file)
        self._embedding_cache: Dict[str, np.ndarray] = self._embeddings.load({})
        self._counts_by_service: Dict[str, int] = {}
        for raw in self._meta.all().values():
            svc = ((raw.get("service") or "unknown").strip()) or "unknown"
            self._counts_by_service[svc] = self._counts_by_service.get(svc, 0) + 1

    def get(self, template_id: str) -> Optional[TemplateState]:
        raw = self._meta.get(template_id)
        return TemplateState(**raw) if raw else None

    def upsert(self, state: TemplateState, flush: bool = True) -> bool:
        with self._lock:
            old_raw = self._meta.get(state.template_id)
            is_new = old_raw is None
            if is_new:
                svc = ((state.service or "unknown").strip()) or "unknown"
                self._counts_by_service[svc] = self._counts_by_service.get(svc, 0) + 1
            else:
                old_svc = ((old_raw.get("service") or "unknown").strip()) or "unknown"
                new_svc = ((state.service or "unknown").strip()) or "unknown"
                if old_svc != new_svc:
                    if old_svc in self._counts_by_service and self._counts_by_service[old_svc] > 0:
                        self._counts_by_service[old_svc] -= 1
                        if self._counts_by_service[old_svc] == 0:
                            del self._counts_by_service[old_svc]
                    self._counts_by_service[new_svc] = self._counts_by_service.get(new_svc, 0) + 1

            self._meta.set(state.template_id, asdict(state), flush=flush)
            return is_new

    def delete(self, template_id: str, flush: bool = True) -> bool:
        with self._lock:
            raw = self._meta.get(template_id)
            if raw is not None:
                svc = ((raw.get("service") or "unknown").strip()) or "unknown"
                if svc in self._counts_by_service and self._counts_by_service[svc] > 0:
                    self._counts_by_service[svc] -= 1
                    if self._counts_by_service[svc] == 0:
                        del self._counts_by_service[svc]
                self._meta.delete(template_id, flush=flush)
                self._embedding_cache.pop(template_id, None)
                return True
            return False

    def count_by_service(self, service: str) -> int:
        with self._lock:
            svc = ((service or "unknown").strip()) or "unknown"
            return self._counts_by_service.get(svc, 0)

    def all_counts_by_service(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts_by_service)

    def total_count(self) -> int:
        with self._lock:
            return sum(self._counts_by_service.values())

    def set_embedding(
        self, template_id: str, embedding: np.ndarray, flush: bool = True
    ) -> None:
        self._embedding_cache[template_id] = embedding
        if flush:
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

    def replace_all(self, states: List[TemplateState]) -> None:
        """Persist a consistent template snapshot for a training run."""
        with self._lock:
            self._counts_by_service = {}
            self._embedding_cache = {}
            self._meta.replace_all({})
            for state in states:
                self.upsert(state, flush=False)
            self.flush()


class GroupRegistry:
    """group_id -> GroupState (metadata) + group_id -> centroid embedding."""

    def __init__(self, storage: StorageConfig):
        base = Path(storage.base_dir)
        self._lock = threading.RLock()
        self._meta = JSONStore(base / storage.group_registry_file)
        self._centroids = PickleStore(base / storage.group_centroids_file)
        self._centroid_cache: Dict[str, np.ndarray] = self._centroids.load({})

    def get(self, group_id: str) -> Optional[GroupState]:
        raw = self._meta.get(group_id)
        return GroupState(**raw) if raw else None

    def upsert(self, state: GroupState, flush: bool = True) -> None:
        with self._lock:
            self._meta.set(state.group_id, asdict(state), flush=flush)

    def touch(
        self, group_id: str, timestamp: float, new_template_id: Optional[str] = None
    ) -> GroupState:
        """Atomically update activity fields without racing documentation refresh."""
        with self._lock:
            state = self.get(group_id)
            if state is None:
                state = GroupState(group_id=group_id, first_seen=timestamp, last_seen=timestamp)
            state.last_seen = timestamp
            state.event_count += 1
            if new_template_id and new_template_id not in state.template_ids:
                state.template_ids.append(new_template_id)
            self._meta.set(group_id, asdict(state), flush=False)
            return state

    def apply_documentation_updates(self, updates: Dict[str, Dict[str, object]]) -> None:
        """Update only documentation fields, preserving concurrent activity data."""
        allowed = {
            "documented", "documentation_id", "confidence", "error_code",
            "documentation_source",
        }
        with self._lock:
            for group_id, fields in updates.items():
                state = self.get(group_id)
                if state is None:
                    continue
                for key, value in fields.items():
                    if key in allowed:
                        setattr(state, key, value)
                self._meta.set(group_id, asdict(state), flush=False)
            self._meta.flush()

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
        with self._lock:
            self._meta.flush()
            self._centroids.save(self._centroid_cache)

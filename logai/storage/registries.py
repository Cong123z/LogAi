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
        self._embeddings_dirty = False
        self._mutation_generation = 0
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
                self._mutation_generation += 1
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
                self._meta.delete(template_id, flush=False)
                if self._embedding_cache.pop(template_id, None) is not None:
                    self._embeddings_dirty = True
                self._mutation_generation += 1
                if flush:
                    self.flush()
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
        with self._lock:
            self._embedding_cache[template_id] = embedding
            self._embeddings_dirty = True
            self._mutation_generation += 1
            if flush:
                self._embeddings.save(self._embedding_cache)
                self._embeddings_dirty = False

    def get_embedding(self, template_id: str) -> Optional[np.ndarray]:
        with self._lock:
            return self._embedding_cache.get(template_id)

    def all_templates(self) -> List[TemplateState]:
        return [TemplateState(**v) for v in self._meta.all().values()]

    def all_embeddings(self) -> Dict[str, np.ndarray]:
        with self._lock:
            return dict(self._embedding_cache)

    def set_group(self, template_id: str, group_id: str, flush: bool = True) -> None:
        with self._lock:
            state = self.get(template_id)
            if state and state.group_id != group_id:
                state.group_id = group_id
                self._mutation_generation += 1
                self.upsert(state, flush=flush)

    @property
    def mutation_generation(self) -> int:
        with self._lock:
            return self._mutation_generation

    def flush(self) -> None:
        with self._lock:
            self._meta.flush()
            if self._embeddings_dirty:
                self._embeddings.save(self._embedding_cache)
                self._embeddings_dirty = False

    def replace_all(self, states: List[TemplateState]) -> None:
        """Persist a consistent template snapshot for a training run.

        Embeddings of templates that stay are kept; the caller drops the ones
        whose text changed.
        """
        with self._lock:
            self._counts_by_service = {}
            keep = {state.template_id for state in states}
            self._embedding_cache = {
                tid: emb for tid, emb in self._embedding_cache.items() if tid in keep
            }
            self._embeddings_dirty = True
            self._mutation_generation += 1
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
        self._centroids_dirty = False
        self._membership_generation = 0

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

    def apply_documentation_updates(
        self,
        updates: Dict[str, Dict[str, object]],
        expected_generation: Optional[int] = None,
    ) -> bool:
        """Update only documentation fields, preserving concurrent activity data."""
        allowed = {
            "documented", "documentation_id", "confidence", "error_code",
            "documentation_source",
        }
        with self._lock:
            if (
                expected_generation is not None
                and expected_generation != self._membership_generation
            ):
                return False
            for group_id, fields in updates.items():
                state = self.get(group_id)
                if state is None:
                    continue
                for key, value in fields.items():
                    if key in allowed:
                        setattr(state, key, value)
                self._meta.set(group_id, asdict(state), flush=False)
            self._meta.flush()
            return True

    def set_centroid(
        self, group_id: str, centroid: np.ndarray, flush: bool = True
    ) -> None:
        with self._lock:
            self._centroid_cache[group_id] = centroid
            self._centroids_dirty = True
            if flush:
                self._centroids.save(self._centroid_cache)
                self._centroids_dirty = False

    def delete(self, group_id: str, flush: bool = True) -> None:
        with self._lock:
            self._meta.delete(group_id, flush=False)
            self._membership_generation += 1
            if flush:
                self.flush()

    def delete_centroid(self, group_id: str, flush: bool = True) -> None:
        with self._lock:
            if group_id in self._centroid_cache:
                del self._centroid_cache[group_id]
                self._centroids_dirty = True
            if flush:
                self.flush()

    def get_centroid(self, group_id: str) -> Optional[np.ndarray]:
        return self._centroid_cache.get(group_id)

    def all_groups(self) -> List[GroupState]:
        return [GroupState(**v) for v in self._meta.all().values()]

    def all_centroids(self) -> Dict[str, np.ndarray]:
        with self._lock:
            return dict(self._centroid_cache)

    @property
    def membership_generation(self) -> int:
        with self._lock:
            return self._membership_generation

    def membership_snapshot(
        self,
    ) -> tuple[int, List[GroupState], Dict[str, np.ndarray]]:
        with self._lock:
            return (
                self._membership_generation,
                self.all_groups(),
                dict(self._centroid_cache),
            )

    def apply_grouping_changes(
        self,
        groups: Dict[str, GroupState],
        centroids: Dict[str, np.ndarray],
        deleted_group_ids: set[str],
        *,
        flush: bool = True,
    ) -> None:
        """Apply one membership transaction while excluding doc refresh writes."""
        with self._lock:
            for group_id in deleted_group_ids:
                self._meta.delete(group_id, flush=False)
                if group_id in self._centroid_cache:
                    del self._centroid_cache[group_id]
                    self._centroids_dirty = True
            for group_id, state in groups.items():
                # A documentation refresh may have committed after the grouping
                # manager took its build snapshot. Preserve those latest fields
                # while this lock excludes any later refresh commit.
                current = self.get(group_id)
                if current is not None:
                    for field_name in (
                        "error_code",
                        "documented",
                        "documentation_id",
                        "confidence",
                        "documentation_source",
                    ):
                        setattr(state, field_name, getattr(current, field_name))
                self._meta.set(group_id, asdict(state), flush=False)
            for group_id, centroid in centroids.items():
                self._centroid_cache[group_id] = centroid
                self._centroids_dirty = True
            self._membership_generation += 1
            if flush:
                self.flush()

    def replace_all(
        self,
        groups: List[GroupState] | Dict[str, GroupState],
        centroids: Dict[str, np.ndarray],
    ) -> None:
        """Replace the complete training snapshot, including stale centroids."""
        with self._lock:
            values = groups.values() if isinstance(groups, dict) else groups
            mapping = {state.group_id: asdict(state) for state in values}
            self._meta.replace_all(mapping)
            self._centroid_cache = dict(centroids)
            self._centroids.save(self._centroid_cache)
            self._centroids_dirty = False
            self._membership_generation += 1

    def flush(self) -> None:
        with self._lock:
            self._meta.flush()
            if self._centroids_dirty:
                self._centroids.save(self._centroid_cache)
                self._centroids_dirty = False

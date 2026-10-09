"""Collector checkpoint (search_after cursor) - lets the engine resume
exactly where it left off after a restart (plan section 4.1 / 7)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from logai.config import StorageConfig
from logai.storage.base import JSONStore


class CheckpointStore:
    def __init__(self, storage: StorageConfig, checkpoint_file: Optional[str] = None):
        base = Path(storage.base_dir)
        filename = checkpoint_file or storage.checkpoint_file
        self.path = base / filename
        self._store = JSONStore(self.path)

    def get_search_after(self) -> Optional[List[Any]]:
        return self._store.get("search_after")

    def set_search_after(self, value: List[Any], flush: bool = True) -> None:
        self._store.set("search_after", value, flush=flush)

    def get_last_timestamp(self) -> Optional[float]:
        return self._store.get("last_timestamp")

    def get_start_ts(self) -> Optional[float]:
        """Epoch time the realtime pipeline first started, used as a floor
        so a fresh (checkpoint-less) start never rewinds into the historical
        window already consumed by training. Persisted so repeated empty
        polls (no new logs yet) don't keep pushing the floor forward."""
        return self._store.get("start_ts")

    def set_start_ts(self, ts: float, flush: bool = True) -> None:
        self._store.set("start_ts", ts, flush=flush)

    def commit(self, search_after: List[Any], last_timestamp: float) -> None:
        """Atomically persist the cursor and its timestamp after batch completion."""
        self._store.bulk_set(
            {
                "search_after": search_after,
                "last_timestamp": last_timestamp,
            }
        )

    # --- per-index cursors (multi-index realtime) ---------------------------
    # "indices": {index: {"search_after", "last_timestamp", "floor_ts"}}.
    # floor_ts is where an index with no cursor yet starts reading from.

    def _indices(self) -> Dict[str, Dict[str, Any]]:
        value = self._store.get("indices")
        return dict(value) if isinstance(value, dict) else {}

    def has_index_cursors(self) -> bool:
        return isinstance(self._store.get("indices"), dict)

    def index_names(self) -> List[str]:
        return list(self._indices())

    def get_index_cursor(self, index: str) -> Optional[Dict[str, Any]]:
        value = self._indices().get(index)
        return dict(value) if isinstance(value, dict) else None

    def set_index_floors(self, floors: Dict[str, float], drop: Iterable[str] = ()) -> None:
        """Start newly resolved indices at their floor and forget removed ones,
        in one atomic write. Existing cursors are never moved."""
        indices = self._indices()
        for index in drop:
            indices.pop(index, None)
        for index, floor_ts in floors.items():
            indices.setdefault(index, {"search_after": None, "last_timestamp": None,
                                       "floor_ts": floor_ts})
        self._store.set("indices", indices)

    def commit_indices(self, cursors: Dict[str, Tuple[List[Any], float]]) -> None:
        """Atomically persist each index's cursor after batch completion."""
        indices = self._indices()
        for index, (search_after, last_timestamp) in cursors.items():
            entry = dict(indices.get(index) or {})
            entry.update(search_after=search_after, last_timestamp=last_timestamp)
            indices[index] = entry
        self._store.set("indices", indices)

    def legacy_floor(self) -> Optional[float]:
        """Where a pre-multi-index checkpoint left off (its last committed event,
        else its start floor); None when there is none to migrate."""
        if self.has_index_cursors():
            return None
        return self.get_last_timestamp() or self.get_start_ts()

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

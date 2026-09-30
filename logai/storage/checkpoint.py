"""Collector checkpoint (search_after cursor) - lets the engine resume
exactly where it left off after a restart (plan section 4.1 / 7)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

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

    def set_last_timestamp(self, ts: float, flush: bool = True) -> None:
        self._store.set("last_timestamp", ts, flush=flush)

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

    def reset(self) -> None:
        self._store.set("search_after", None, flush=False)
        self._store.set("last_timestamp", None, flush=False)
        self._store.set("start_ts", None)

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

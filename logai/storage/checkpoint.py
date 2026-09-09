"""Collector checkpoint (search_after cursor) - lets the engine resume
exactly where it left off after a restart (plan section 4.1 / 7)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from logai.config import StorageConfig
from logai.storage.base import JSONStore


class CheckpointStore:
    def __init__(self, storage: StorageConfig):
        base = Path(storage.base_dir)
        self._store = JSONStore(base / storage.checkpoint_file)

    def get_search_after(self) -> Optional[List[Any]]:
        return self._store.get("search_after")

    def set_search_after(self, value: List[Any]) -> None:
        self._store.set("search_after", value)

    def get_last_timestamp(self) -> Optional[float]:
        return self._store.get("last_timestamp")

    def set_last_timestamp(self, ts: float) -> None:
        self._store.set("last_timestamp", ts)

    def reset(self) -> None:
        self._store.set("search_after", None, flush=False)
        self._store.set("last_timestamp", None)

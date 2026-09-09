"""Idempotency index keyed by event_id (plan section 7).

Keeps a TTL-bounded set of recently processed event_ids so that re-delivery
(e.g. after a crash before the checkpoint advanced) never double-counts an
event in features / metrics.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict

from logai.config import StorageConfig
from logai.storage.base import JSONStore


class DedupIndex:
    def __init__(self, storage: StorageConfig, ttl_seconds: float = 86400.0):
        base = Path(storage.base_dir)
        self._store = JSONStore(base / storage.dedup_index_file)
        self._ttl = ttl_seconds

    def seen(self, event_id: str) -> bool:
        return event_id in self._store

    def mark(self, event_id: str) -> None:
        self._store.set(event_id, time.time(), flush=False)

    def flush(self) -> None:
        self._store.flush()

    def gc(self) -> int:
        """Remove entries older than TTL. Returns number removed."""
        now = time.time()
        data: Dict[str, float] = self._store.all()
        removed = 0
        for event_id, seen_at in list(data.items()):
            if now - seen_at > self._ttl:
                self._store.delete(event_id, flush=False)
                removed += 1
        self._store.flush()
        return removed

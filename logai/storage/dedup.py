"""Bounded idempotency index keyed by event_id (Issue 2).

Maintains a bounded LRU cache (OrderedDict) of recently processed event_ids
capped at `max_size` entries. Eliminates unbounded RAM growth and O(D) GC stalls,
while persisting snapshots to disk for crash recovery.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from logai.config import StorageConfig


class DedupIndex:
    def __init__(
        self,
        storage: StorageConfig,
        ttl_seconds: float = 86400.0,
        max_size: int = 200_000,
        flush_interval_seconds: float = 0.0,
    ):
        base = Path(storage.base_dir)
        self.path = base / storage.dedup_index_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._flush_interval = flush_interval_seconds
        self._last_flush_ts = 0.0
        self._lock = threading.RLock()
        self._data: OrderedDict[str, None] = OrderedDict()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # Support both legacy dict format {"event_id": timestamp}
                # and compact list format ["event_id1", "event_id2", ...]
                if isinstance(raw, dict):
                    keys = list(raw.keys())
                elif isinstance(raw, list):
                    keys = raw
                else:
                    keys = []
                # Keep only up to max_size latest keys
                if len(keys) > self._max_size:
                    keys = keys[-self._max_size:]
                self._data = OrderedDict((k, None) for k in keys)
            except (json.JSONDecodeError, OSError):
                self._data = OrderedDict()

    def seen(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._data

    def mark(self, event_id: str) -> None:
        with self._lock:
            self._data[event_id] = None
            self._data.move_to_end(event_id)
            if len(self._data) > self._max_size:
                self._data.popitem(last=False)
            self._dirty = True

    def flush(self, force: bool = False) -> None:
        """Persist current bounded keys to disk via atomic replace."""
        with self._lock:
            if not self._dirty and self.path.exists():
                return
            now = time.monotonic()
            if not force and self._flush_interval > 0.0:
                if now - self._last_flush_ts < self._flush_interval:
                    return
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                # Store compact list of keys without formatting indentation to minimize file size
                json.dump(list(self._data.keys()), f, separators=(",", ":"))
            os.replace(tmp_path, self.path)
            self._dirty = False
            self._last_flush_ts = now

    def gc(self, force: bool = False) -> int:
        """Bounded LRU evicts automatically in O(1) during mark().
        gc() flushes modified state to disk and returns 0 removed items."""
        self.flush(force=force)
        return 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, event_id: str) -> bool:
        return self.seen(event_id)


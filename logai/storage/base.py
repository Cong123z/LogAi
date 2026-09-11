"""Generic file-based persistence primitives.

Design goals (per plan section 6/7 - state & reliability):
  - Atomic writes (write to tmp file, os.replace) so a crash mid-write never
    corrupts the registry.
  - Thread-safe (a single process, multiple threads: collector + processor).
  - Human-inspectable JSON for metadata/state, pickle for numpy arrays /
    sklearn objects that don't serialize well to JSON.

This is intentionally simple (no external DB) per the MVP decision to use
file-based storage. It is NOT safe for multiple concurrent OS processes
writing the same file - the realtime pipeline is expected to run as a single
process (optionally multi-threaded).
"""
from __future__ import annotations

import json
import os
import pickle
import threading
from pathlib import Path
from typing import Any, Dict


class JSONStore:
    """A dict-like store persisted as a single JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        # Set on every mutation; lets _flush() short-circuit when nothing has
        # changed since the last write (same pattern as DedupIndex.flush), so a
        # per-batch registry flush is a no-op when no template/group was added.
        self._dirty = False
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        self._data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    # Corrupt file - fall back to empty state rather than crash.
                    self._data = {}
            else:
                self._data = {}

    def _flush(self) -> None:
        with self._lock:
            if not self._dirty and self.path.exists():
                return
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, default=str)
            os.replace(tmp_path, self.path)
            self._dirty = False

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any, flush: bool = True) -> None:
        with self._lock:
            self._data[key] = value
            self._dirty = True
            if flush:
                self._flush()

    def delete(self, key: str, flush: bool = True) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._dirty = True
            if flush:
                self._flush()

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def bulk_set(self, mapping: Dict[str, Any]) -> None:
        with self._lock:
            self._data.update(mapping)
            self._dirty = True
            self._flush()

    def replace_all(self, mapping: Dict[str, Any]) -> None:
        with self._lock:
            self._data = dict(mapping)
            self._dirty = True
            self._flush()

    def flush(self) -> None:
        self._flush()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data


class PickleStore:
    """A single-object store persisted via pickle (for numpy arrays, sklearn
    models, or any dict of such objects)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def load(self, default: Any = None) -> Any:
        with self._lock:
            if not self.path.exists():
                return default if default is not None else {}
            try:
                with open(self.path, "rb") as f:
                    return pickle.load(f)
            except (pickle.PickleError, EOFError, OSError):
                return default if default is not None else {}

    def save(self, obj: Any) -> None:
        with self._lock:
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "wb") as f:
                pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, self.path)


class ModelStore:
    """Directory of per-key pickled objects, e.g. one IsolationForest per
    group_id: data/models/<group_id>.pkl"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path:
        safe_key = key.replace("/", "_")
        return self.base_dir / f"{safe_key}.pkl"

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def load(self, key: str) -> Any:
        p = self._path(key)
        if not p.exists():
            return None
        with self._lock:
            with open(p, "rb") as f:
                return pickle.load(f)

    def save(self, key: str, obj: Any) -> None:
        p = self._path(key)
        with self._lock:
            tmp_path = p.with_suffix(".tmp")
            with open(tmp_path, "wb") as f:
                pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, p)

    def keys(self) -> list:
        return [p.stem for p in self.base_dir.glob("*.pkl")]

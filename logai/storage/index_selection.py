"""Elasticsearch indices chosen on the web "Data sources" page.

The web is the only writer of the selection file and the engine only reads
it; the engine is the only writer of the status file (available indices, what
each pattern resolved to, per-index progress) and the web only reads that.

Each entry keeps `added_at`: a concrete index first seen under an entry is
read from that moment on ("from now"), so a new daily index under an old
pattern is read from its first document while a newly added entry never
backfills history.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from logai.storage.base import atomic_write_json

SCHEMA_VERSION = 1
MAX_ENTRIES = 50
PATTERN_LIMIT = 255
# Lowercase ES index name characters plus the `*` wildcard; must not start
# with - _ + (reserved by Elasticsearch) and never contain a comma.
_PATTERN_RE = re.compile(r"^[a-z0-9*][a-z0-9*._#-]*$")

_write_lock = threading.Lock()


class IndexSelectionError(ValueError):
    """Invalid selection data."""


class IndexSelectionConflict(IndexSelectionError):
    def __init__(self, current_revision: Optional[str]):
        super().__init__("The index selection changed; reload and try again")
        self.current_revision = current_revision


def _revision(entries: List[Dict[str, Any]]) -> str:
    patterns = [entry["pattern"] for entry in entries]
    encoded = json.dumps(patterns, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_patterns(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        raise IndexSelectionError("patterns must be a list")
    patterns: List[str] = []
    for value in raw:
        pattern = value.strip() if isinstance(value, str) else ""
        if not pattern or len(pattern) > PATTERN_LIMIT or not _PATTERN_RE.fullmatch(pattern):
            raise IndexSelectionError(
                f"Invalid index or pattern {value!r}: use lowercase letters, digits, "
                "'.', '_', '-', '#' and '*'"
            )
        if pattern not in patterns:
            patterns.append(pattern)
    if len(patterns) > MAX_ENTRIES:
        raise IndexSelectionError(f"At most {MAX_ENTRIES} indices or patterns")
    return patterns


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    atomic_write_json(path, payload, indent=2, ensure_ascii=False)


def read_json(path: str | Path) -> Dict[str, Any]:
    """A missing or corrupt file reads as {}."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


class IndexSelectionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def mtime_ns(self) -> Optional[int]:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def load(self) -> Optional[Dict[str, Any]]:
        """{revision, updated_at, entries}, or None while the web has never
        saved a selection (the engine then uses its configured index)."""
        raw = read_json(self.path)
        if not isinstance(raw.get("entries"), list):
            return None
        entries = []
        for item in raw["entries"]:
            if not isinstance(item, dict):
                continue
            try:
                pattern = validate_patterns([item.get("pattern")])[0]
            except IndexSelectionError:
                continue
            added_at = item.get("added_at")
            if isinstance(added_at, bool) or not isinstance(added_at, (int, float)):
                added_at = 0.0
            entries.append({"pattern": pattern, "added_at": float(added_at)})
        return {
            "revision": _revision(entries),
            "updated_at": raw.get("updated_at"),
            "entries": entries,
        }

    def replace(
        self,
        patterns: Any,
        expected_revision: Optional[str],
        known_added_at: Optional[Dict[str, float]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Save the selection. An entry already selected (or known to the
        engine, via `known_added_at`) keeps its added_at; new ones start now."""
        cleaned = validate_patterns(patterns)
        with _write_lock:
            current = self.load()
            current_revision = current["revision"] if current else None
            if expected_revision != current_revision:
                raise IndexSelectionConflict(current_revision)
            added = dict(known_added_at or {})
            added.update({e["pattern"]: e["added_at"] for e in (current or {}).get("entries", [])})
            timestamp = time.time() if now is None else now
            entries = [
                {"pattern": pattern, "added_at": added.get(pattern) or timestamp}
                for pattern in cleaned
            ]
            _atomic_write(self.path, {
                "schema_version": SCHEMA_VERSION,
                "updated_at": timestamp,
                "entries": entries,
            })
        return {"revision": _revision(entries), "updated_at": timestamp, "entries": entries}


def entries_from_config(index: str, added_at: float) -> List[Dict[str, Any]]:
    """The configured index string (comma separated) as selection entries."""
    return [
        {"pattern": pattern, "added_at": added_at}
        for pattern in dict.fromkeys(p.strip() for p in str(index or "").split(","))
        if pattern
    ]


def selection_revision(entries: Iterable[Dict[str, Any]]) -> str:
    return _revision(list(entries))


def write_status(path: str | Path, payload: Dict[str, Any]) -> None:
    _atomic_write(Path(path), {"schema_version": SCHEMA_VERSION, **payload})

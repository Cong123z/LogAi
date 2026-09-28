"""Persistent documentation corpus and manual group assignment storage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml


class DocumentationStoreError(ValueError):
    """Base error raised for invalid documentation storage operations."""


class RevisionConflict(DocumentationStoreError):
    def __init__(self, current_revision: str):
        super().__init__("The documentation data changed; reload and try again")
        self.current_revision = current_revision


class DocumentInUse(DocumentationStoreError):
    def __init__(self, group_ids: list[str]):
        super().__init__("Document is assigned to one or more groups")
        self.group_ids = group_ids


def _revision(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def group_fingerprint(template_ids: Iterable[str], template_texts: Iterable[str] = ()) -> str:
    members = sorted(str(value) for value in template_ids)
    texts = sorted(str(value) for value in template_texts)
    return _revision({"template_ids": members, "template_texts": texts})


class DocumentationCorpusStore:
    """Single web-writer store; engine and training processes read snapshots."""

    _ORDERED_ID_PATTERN = re.compile(r"^DOC-(\d{3,})$")

    def __init__(
        self,
        corpus_path: str | Path,
        overrides_path: str | Path,
        status_path: str | Path,
        seed_path: str | Path | None = None,
    ):
        self.corpus_path = Path(corpus_path)
        self.overrides_path = Path(overrides_path)
        self.status_path = Path(status_path)
        self.seed_path = Path(seed_path) if seed_path else None
        self._lock = threading.RLock()
        for path in (self.corpus_path, self.overrides_path, self.status_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls, config: Any) -> "DocumentationCorpusStore":
        base = Path(config.storage.base_dir)

        def runtime_path(value: str, filename: str) -> Path:
            path = Path(value)
            if not path.is_absolute() and (not value or path.parent == Path("data")):
                return base / filename
            return path

        return cls(
            runtime_path(config.doc_matcher.corpus_path, "documentation_corpus.json"),
            runtime_path(config.doc_matcher.overrides_path, "documentation_overrides.json"),
            runtime_path(config.doc_matcher.status_path, "documentation_status.json"),
            config.doc_matcher.seed_corpus_path,
        )

    @staticmethod
    def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, dict):
            raise DocumentationStoreError(f"{path.name} must contain a JSON object")
        return raw

    def _initialize(self) -> None:
        with self._lock:
            if not self.corpus_path.exists():
                entries: list[Dict[str, str]] = []
                if self.seed_path and self.seed_path.exists():
                    with open(self.seed_path, "r", encoding="utf-8") as stream:
                        seed = yaml.safe_load(stream) or []
                    entries = self.validate_entries(seed, allow_ids=True)
                self._write_corpus(
                    entries, self._derive_next_document_number(entries)
                )
            if not self.overrides_path.exists():
                self._write_overrides({}, {})

    @staticmethod
    def validate_entries(raw: Any, allow_ids: bool = True) -> list[Dict[str, str]]:
        if not isinstance(raw, list):
            raise DocumentationStoreError("Documentation entries must be a list")
        result: list[Dict[str, str]] = []
        seen: set[str] = set()
        for position, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise DocumentationStoreError(f"Entry {position} must be an object")
            doc_id = str(item.get("id") or "").strip() if allow_ids else ""
            if not doc_id:
                raise DocumentationStoreError("id is required")
            if doc_id in seen:
                raise DocumentationStoreError(f"Duplicate documentation id: {doc_id}")
            title = item.get("title", "")
            text = item.get("text")
            error_code = item.get("error_code", "")
            if not isinstance(title, str) or len(title.strip()) > 200:
                raise DocumentationStoreError("title must be a string of at most 200 characters")
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 20_000:
                raise DocumentationStoreError("text must be non-empty and at most 20000 characters")
            if not isinstance(error_code, str) or len(error_code.strip()) > 100:
                raise DocumentationStoreError("error_code must be a string of at most 100 characters")
            seen.add(doc_id)
            result.append({
                "id": doc_id,
                "title": title.strip(),
                "text": text.strip(),
                "error_code": error_code.strip(),
            })
        return result

    @classmethod
    def _derive_next_document_number(cls, entries: Iterable[Dict[str, str]]) -> int:
        numbers = []
        for entry in entries:
            match = cls._ORDERED_ID_PATTERN.fullmatch(entry["id"])
            if match:
                numbers.append(int(match.group(1)))
        return max(numbers, default=0) + 1

    def _write_corpus(
        self,
        entries: list[Dict[str, str]],
        next_document_number: int,
    ) -> Dict[str, Any]:
        payload = {
            "schema_version": 1,
            "revision": _revision(entries),
            "updated_at": time.time(),
            "next_document_number": next_document_number,
            "entries": entries,
        }
        self._atomic_json_write(self.corpus_path, payload)
        return payload

    @staticmethod
    def _override_revision(
        overrides: Dict[str, Dict[str, Any]],
        cleared_groups: Dict[str, Dict[str, Any]],
    ) -> str:
        return _revision({"overrides": overrides, "cleared_groups": cleared_groups})

    def _write_overrides(
        self,
        overrides: Dict[str, Dict[str, Any]],
        cleared_groups: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        cleared_groups = dict(cleared_groups or {})
        payload = {
            "schema_version": 1,
            "revision": self._override_revision(overrides, cleared_groups),
            "updated_at": time.time(),
            "overrides": overrides,
            "cleared_groups": cleared_groups,
        }
        self._atomic_json_write(self.overrides_path, payload)
        return payload

    def load_corpus(self) -> Dict[str, Any]:
        with self._lock:
            raw = self._read_json(self.corpus_path)
            entries = self.validate_entries(raw.get("entries", []), allow_ids=True)
            expected = _revision(entries)
            if raw.get("revision") != expected:
                raise DocumentationStoreError("Documentation corpus revision is invalid")
            stored_next = raw.get("next_document_number")
            if stored_next is not None and (
                isinstance(stored_next, bool)
                or not isinstance(stored_next, int)
                or stored_next < 1
            ):
                raise DocumentationStoreError(
                    "next_document_number must be a positive integer"
                )
            derived_next = self._derive_next_document_number(entries)
            next_document_number = max(stored_next or 1, derived_next)
            return {
                **raw,
                "entries": entries,
                "next_document_number": next_document_number,
            }

    def load_overrides(self) -> Dict[str, Any]:
        with self._lock:
            raw = self._read_json(self.overrides_path)
            overrides = raw.get("overrides", {})
            if not isinstance(overrides, dict):
                raise DocumentationStoreError("Documentation overrides must be an object")
            cleared_groups = raw.get("cleared_groups", {})
            if not isinstance(cleared_groups, dict):
                raise DocumentationStoreError("Cleared documentation groups must be an object")
            expected = self._override_revision(overrides, cleared_groups)
            legacy_expected = _revision(overrides)
            legacy_file = "cleared_groups" not in raw
            if raw.get("revision") != expected and not (
                legacy_file and raw.get("revision") == legacy_expected
            ):
                raise DocumentationStoreError("Documentation override revision is invalid")
            return {
                **raw,
                "overrides": dict(overrides),
                "cleared_groups": dict(cleared_groups),
            }

    @staticmethod
    def _check_revision(expected: Optional[str], current: str) -> None:
        if expected != current:
            raise RevisionConflict(current)

    def create_document(self, data: Dict[str, Any], expected_revision: str) -> tuple[Dict[str, str], Dict[str, Any]]:
        with self._lock:
            corpus = self.load_corpus()
            self._check_revision(expected_revision, corpus["revision"])
            next_number = corpus["next_document_number"]
            existing_ids = {entry["id"] for entry in corpus["entries"]}
            while f"DOC-{next_number:03d}" in existing_ids:
                next_number += 1
            entry = self.validate_entries(
                [{**data, "id": f"DOC-{next_number:03d}"}], allow_ids=True
            )[0]
            updated = self._write_corpus(
                [*corpus["entries"], entry], next_number + 1
            )
            return entry, updated

    def update_document(self, doc_id: str, data: Dict[str, Any], expected_revision: str) -> tuple[Dict[str, str], Dict[str, Any]]:
        with self._lock:
            corpus = self.load_corpus()
            self._check_revision(expected_revision, corpus["revision"])
            candidate = {"id": doc_id, **data}
            entry = self.validate_entries([candidate], allow_ids=True)[0]
            entries = list(corpus["entries"])
            for index, current in enumerate(entries):
                if current["id"] == doc_id:
                    entries[index] = entry
                    return entry, self._write_corpus(
                        entries, corpus["next_document_number"]
                    )
            raise KeyError(doc_id)

    def delete_document(
        self,
        doc_id: str,
        expected_revision: str,
        active_group_ids: Optional[set[str]] = None,
        assigned_group_ids: Optional[Iterable[str]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            corpus = self.load_corpus()
            self._check_revision(expected_revision, corpus["revision"])
            override_snapshot = self.load_overrides()
            overrides = override_snapshot["overrides"]
            users = {
                gid for gid, value in overrides.items()
                if isinstance(value, dict) and value.get("documentation_id") == doc_id
                and (active_group_ids is None or gid in active_group_ids)
            }
            users.update(str(group_id) for group_id in (assigned_group_ids or ()))
            if users and not force:
                raise DocumentInUse(sorted(users))
            entries = [entry for entry in corpus["entries"] if entry["id"] != doc_id]
            if len(entries) == len(corpus["entries"]):
                raise KeyError(doc_id)
            if force:
                overrides = {
                    group_id: value
                    for group_id, value in overrides.items()
                    if not (
                        isinstance(value, dict)
                        and value.get("documentation_id") == doc_id
                    )
                }
                self._write_overrides(overrides, override_snapshot.get("cleared_groups", {}))
            return self._write_corpus(entries, corpus["next_document_number"])

    def set_override(
        self,
        group_id: str,
        documentation_id: str,
        fingerprint: str,
        expected_revision: str,
    ) -> Dict[str, Any]:
        with self._lock:
            corpus = self.load_corpus()
            if documentation_id not in {entry["id"] for entry in corpus["entries"]}:
                raise KeyError(documentation_id)
            snapshot = self.load_overrides()
            self._check_revision(expected_revision, snapshot["revision"])
            overrides = snapshot["overrides"]
            cleared_groups = snapshot.get("cleared_groups", {})
            overrides[group_id] = {
                "documentation_id": documentation_id,
                "group_fingerprint": fingerprint,
                "assigned_at": time.time(),
            }
            cleared_groups.pop(group_id, None)
            return self._write_overrides(overrides, cleared_groups)

    def clear_override(self, group_id: str, expected_revision: str) -> Dict[str, Any]:
        with self._lock:
            snapshot = self.load_overrides()
            self._check_revision(expected_revision, snapshot["revision"])
            overrides = snapshot["overrides"]
            cleared_groups = snapshot.get("cleared_groups", {})
            overrides.pop(group_id, None)
            if group_id in cleared_groups:
                return snapshot
            cleared_groups[group_id] = {"cleared_at": time.time()}
            return self._write_overrides(overrides, cleared_groups)

    def load_status(self) -> Dict[str, Any]:
        if not self.status_path.exists():
            return {}
        try:
            return self._read_json(self.status_path)
        except (OSError, json.JSONDecodeError, DocumentationStoreError):
            return {}

    def write_status(self, payload: Dict[str, Any]) -> None:
        self._atomic_json_write(self.status_path, payload)

    def synchronization_status(self) -> Dict[str, Any]:
        corpus = self.load_corpus()
        overrides = self.load_overrides()
        status = self.load_status()
        corpus_revision = corpus["revision"]
        override_revision = overrides["revision"]
        if (
            status.get("applied_corpus_revision") == corpus_revision
            and status.get("applied_override_revision") == override_revision
        ):
            state = "applied"
        elif (
            status.get("attempted_corpus_revision") == corpus_revision
            and status.get("attempted_override_revision") == override_revision
            and status.get("error")
        ):
            state = "failed"
        elif status:
            state = "pending"
        else:
            state = "engine_unavailable"
        return {**status, "state": state}

"""Persistent manual template-to-group intent and engine application status."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


SCHEMA_VERSION = 1
# Numeric IDs are generated for new groups. The legacy hash form remains valid
# so existing persisted assignments keep their identity across an upgrade.
MANUAL_GROUP_RE = re.compile(r"^G_MANUAL_(?:[0-9]{3,}|[0-9a-f]{12})$")
NUMERIC_MANUAL_GROUP_RE = re.compile(r"^G_MANUAL_([0-9]{3,})$")


class GroupingStoreError(ValueError):
    """Base error for invalid grouping state or mutations."""


class GroupingSchemaError(GroupingStoreError):
    pass


class GroupingRevisionConflict(GroupingStoreError):
    def __init__(self, current_revision: str):
        super().__init__("The grouping data changed; reload and try again")
        self.current_revision = current_revision


class GroupingTargetError(GroupingStoreError):
    pass


class GroupingCycleError(GroupingStoreError):
    pass


def grouping_revision(assignments: Dict[str, Any], manual_groups: Dict[str, Any]) -> str:
    encoded = json.dumps(
        {"assignments": assignments, "manual_groups": manual_groups},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class GroupingOverrideStore:
    """Web-owned override file and engine-owned status file.

    A single web process is the override writer and a single engine/training
    process is the status writer. Atomic replacement protects readers from
    partial JSON, but this is intentionally not a distributed lock.
    """

    def __init__(self, overrides_path: str | Path, status_path: str | Path):
        self.overrides_path = Path(overrides_path)
        self.status_path = Path(status_path)
        self._lock = threading.RLock()
        self._cached_mtime_ns: Optional[int] = None
        self._cached_snapshot: Optional[Dict[str, Any]] = None
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.overrides_path.exists():
            self._write_overrides({}, {})

    @classmethod
    def from_config(cls, config: Any) -> "GroupingOverrideStore":
        base = Path(config.storage.base_dir)
        return cls(
            base / config.storage.grouping_overrides_file,
            base / config.storage.grouping_status_file,
        )

    @staticmethod
    def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise GroupingSchemaError(f"Unable to read {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise GroupingSchemaError(f"{path.name} must contain a JSON object")
        return value

    @staticmethod
    def _validate_timestamp(value: Any, field: str, *, optional: bool = False) -> None:
        if optional and value is None:
            return
        if not isinstance(value, (int, float)):
            raise GroupingSchemaError(f"{field} must be a number")

    @classmethod
    def validate_overrides(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise GroupingSchemaError("Unsupported grouping override schema_version")
        assignments = raw.get("assignments")
        manual_groups = raw.get("manual_groups")
        if not isinstance(assignments, dict) or not isinstance(manual_groups, dict):
            raise GroupingSchemaError("assignments and manual_groups must be objects")

        normalized_assignments: Dict[str, Dict[str, Any]] = {}
        for source_id, value in assignments.items():
            if not isinstance(source_id, str) or not source_id.strip():
                raise GroupingSchemaError("Assignment source IDs must be non-empty strings")
            if not isinstance(value, dict):
                raise GroupingSchemaError(f"Assignment {source_id} must be an object")
            kind = value.get("target_kind")
            target_id = value.get("target_id")
            if kind not in {"anchor", "manual_group"}:
                raise GroupingSchemaError(f"Assignment {source_id} has an invalid target_kind")
            if not isinstance(target_id, str) or not target_id.strip():
                raise GroupingSchemaError(f"Assignment {source_id} has an invalid target_id")
            cls._validate_timestamp(value.get("assigned_at"), f"{source_id}.assigned_at")
            if kind == "manual_group":
                if not MANUAL_GROUP_RE.fullmatch(target_id) or target_id not in manual_groups:
                    raise GroupingSchemaError(
                        f"Assignment {source_id} references an invalid manual group"
                    )
            normalized_assignments[source_id] = {
                "target_kind": kind,
                "target_id": target_id,
                "assigned_at": float(value["assigned_at"]),
            }

        normalized_groups: Dict[str, Dict[str, Any]] = {}
        for group_id, value in manual_groups.items():
            if not isinstance(group_id, str) or not MANUAL_GROUP_RE.fullmatch(group_id):
                raise GroupingSchemaError(f"Invalid manual group ID: {group_id}")
            if not isinstance(value, dict):
                raise GroupingSchemaError(f"Manual group {group_id} must be an object")
            cls._validate_timestamp(value.get("created_at"), f"{group_id}.created_at")
            normalized_groups[group_id] = {"created_at": float(value["created_at"])}

        cls._check_cycles(normalized_assignments)
        expected = grouping_revision(normalized_assignments, normalized_groups)
        if raw.get("revision") != expected:
            raise GroupingSchemaError("Grouping override revision is invalid")
        cls._validate_timestamp(raw.get("updated_at"), "updated_at")
        next_manual_group_number = raw.get("next_manual_group_number")
        if next_manual_group_number is None:
            numeric_ids = [
                int(match.group(1))
                for group_id in normalized_groups
                if (match := NUMERIC_MANUAL_GROUP_RE.fullmatch(group_id))
            ]
            next_manual_group_number = max(numeric_ids, default=0) + 1
        if (
            not isinstance(next_manual_group_number, int)
            or isinstance(next_manual_group_number, bool)
            or next_manual_group_number < 1
        ):
            raise GroupingSchemaError("next_manual_group_number must be a positive integer")
        highest_numeric_id = max(
            (
                int(match.group(1))
                for group_id in normalized_groups
                if (match := NUMERIC_MANUAL_GROUP_RE.fullmatch(group_id))
            ),
            default=0,
        )
        if next_manual_group_number <= highest_numeric_id:
            raise GroupingSchemaError(
                "next_manual_group_number must be greater than existing manual group IDs"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": expected,
            "updated_at": float(raw["updated_at"]),
            "assignments": normalized_assignments,
            "manual_groups": normalized_groups,
            "next_manual_group_number": next_manual_group_number,
        }

    @staticmethod
    def _check_cycles(assignments: Dict[str, Dict[str, Any]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(template_id: str) -> None:
            if template_id in visiting:
                raise GroupingCycleError(f"Anchor cycle includes {template_id}")
            if template_id in visited:
                return
            visiting.add(template_id)
            assignment = assignments.get(template_id)
            if assignment and assignment.get("target_kind") == "anchor":
                visit(str(assignment.get("target_id")))
            visiting.remove(template_id)
            visited.add(template_id)

        for source_id in assignments:
            visit(source_id)

    def _write_overrides(
        self,
        assignments: Dict[str, Dict[str, Any]],
        manual_groups: Dict[str, Dict[str, Any]],
        next_manual_group_number: int = 1,
    ) -> Dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "revision": grouping_revision(assignments, manual_groups),
            "updated_at": time.time(),
            "assignments": assignments,
            "manual_groups": manual_groups,
            "next_manual_group_number": next_manual_group_number,
        }
        self._atomic_write(self.overrides_path, payload)
        self._cached_mtime_ns = self.overrides_path.stat().st_mtime_ns
        self._cached_snapshot = payload
        return payload

    def load_overrides(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = self.validate_overrides(self._read(self.overrides_path))
            self._cached_mtime_ns = self.overrides_path.stat().st_mtime_ns
            self._cached_snapshot = snapshot
            return snapshot

    def load_if_changed(self) -> Optional[Dict[str, Any]]:
        """Return a parsed snapshot only when the atomic file changed."""
        with self._lock:
            try:
                mtime_ns = self.overrides_path.stat().st_mtime_ns
            except OSError as exc:
                raise GroupingSchemaError(f"Unable to stat grouping overrides: {exc}") from exc
            if self._cached_mtime_ns == mtime_ns:
                return None
            # Remember a corrupt revision's mtime too, so the engine reports it
            # once and waits for a writer to replace the file instead of parsing
            # the same invalid JSON on every poll boundary.
            self._cached_mtime_ns = mtime_ns
            return self.load_overrides()

    def cached_overrides(self) -> Dict[str, Any]:
        with self._lock:
            return self._cached_snapshot or self.load_overrides()

    @staticmethod
    def _check_revision(expected: Optional[str], current: str) -> None:
        if expected != current:
            raise GroupingRevisionConflict(current)

    def replace_assignment(
        self,
        source_template_id: str,
        target_kind: str,
        target_id: str,
        expected_revision: Optional[str],
        *,
        effective_group_ids: Iterable[str] = (),
    ) -> Dict[str, Any]:
        with self._lock:
            snapshot = self.load_overrides()
            self._check_revision(expected_revision, snapshot["revision"])
            if target_kind not in {"anchor", "manual_group"}:
                raise GroupingTargetError("target_kind must be anchor or manual_group")
            assignments = dict(snapshot["assignments"])
            manual_groups = dict(snapshot["manual_groups"])
            assignments[source_template_id] = {
                "target_kind": target_kind,
                "target_id": target_id,
                "assigned_at": time.time(),
            }
            self._check_cycles(assignments)

            referenced = {
                value["target_id"]
                for value in assignments.values()
                if value.get("target_kind") == "manual_group"
            }
            effective = set(effective_group_ids)
            manual_groups = {
                group_id: value
                for group_id, value in manual_groups.items()
                if group_id in referenced or group_id in effective
            }
            if target_kind == "manual_group" and target_id not in manual_groups:
                raise GroupingTargetError(f"Unknown manual group: {target_id}")
            return self._write_overrides(
                assignments,
                manual_groups,
                snapshot["next_manual_group_number"],
            )

    def create_manual_group_assignment(
        self,
        source_template_id: str,
        expected_revision: Optional[str],
        *,
        effective_group_ids: Iterable[str] = (),
    ) -> tuple[str, Dict[str, Any]]:
        with self._lock:
            snapshot = self.load_overrides()
            self._check_revision(expected_revision, snapshot["revision"])
            manual_groups = dict(snapshot["manual_groups"])
            effective = set(effective_group_ids)
            next_number = snapshot["next_manual_group_number"]
            while True:
                group_id = f"G_MANUAL_{next_number:03d}"
                next_number += 1
                if group_id not in manual_groups and group_id not in effective:
                    break
            manual_groups[group_id] = {"created_at": time.time()}
            assignments = dict(snapshot["assignments"])
            assignments[source_template_id] = {
                "target_kind": "manual_group",
                "target_id": group_id,
                "assigned_at": time.time(),
            }
            referenced = {
                value["target_id"]
                for value in assignments.values()
                if value.get("target_kind") == "manual_group"
            }
            manual_groups = {
                gid: value
                for gid, value in manual_groups.items()
                if gid in referenced or gid in effective
            }
            return group_id, self._write_overrides(
                assignments, manual_groups, next_number
            )

    @classmethod
    def validate_status(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise GroupingSchemaError("Unsupported grouping status schema_version")
        state = raw.get("state")
        if state not in {"pending", "partial", "applied", "failed"}:
            raise GroupingSchemaError("Grouping status has an invalid state")
        results = raw.get("results", {})
        if not isinstance(results, dict):
            raise GroupingSchemaError("Grouping status results must be an object")
        error = raw.get("error")
        if error is not None and not isinstance(error, dict):
            raise GroupingSchemaError("Grouping status error must be an object or null")
        return dict(raw)

    def load_status(self) -> Dict[str, Any]:
        with self._lock:
            if not self.status_path.exists():
                return {}
            return self.validate_status(self._read(self.status_path))

    def write_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            value = {"schema_version": SCHEMA_VERSION, **payload}
            value.setdefault("results", {})
            value.setdefault("unresolved", {})
            value.setdefault("error", None)
            self.validate_status(value)
            self._atomic_write(self.status_path, value)
            return value

    def update_heartbeat(
        self,
        now: Optional[float] = None,
        runtime: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            timestamp = time.time() if now is None else now
            try:
                status = self.load_status()
            except GroupingStoreError:
                status = {}
            if not status:
                status = {
                    "schema_version": SCHEMA_VERSION,
                    "applied_revision": None,
                    "attempted_revision": None,
                    "last_attempt_at": None,
                    "last_applied_at": None,
                    "state": "pending",
                    "error": None,
                    "results": {},
                    "unresolved": {},
                }
            status["last_heartbeat_at"] = timestamp
            if runtime is not None:
                status["runtime"] = runtime
            return self.write_status(status)

    def synchronization_status(
        self,
        *,
        stale_seconds: float,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        current_time = time.time() if now is None else now
        try:
            override = self.load_overrides()
        except GroupingStoreError as exc:
            return {
                "revision": None,
                "state": "failed",
                "retryable": False,
                "error": {
                    "reason_code": "invalid_override_schema",
                    "message": str(exc)[:500],
                    "retryable": False,
                },
                "results": {},
            }
        try:
            status = self.load_status()
        except GroupingStoreError as exc:
            return {
                "revision": override["revision"],
                "state": "engine_unavailable",
                "retryable": True,
                "error": {
                    "reason_code": "invalid_status_schema",
                    "message": str(exc)[:500],
                    "retryable": True,
                },
                "results": {},
            }

        heartbeat = status.get("last_heartbeat_at")
        if not isinstance(heartbeat, (int, float)) or current_time - heartbeat > stale_seconds:
            state = "engine_unavailable"
        elif status.get("attempted_revision") == override["revision"]:
            state = status.get("state", "pending")
        elif status.get("applied_revision") == override["revision"]:
            state = "applied"
        else:
            state = "pending"
        results = status.get("results", {}) if isinstance(status.get("results"), dict) else {}
        retryable = state in {"pending", "engine_unavailable"} or any(
            isinstance(value, dict) and bool(value.get("retryable"))
            for value in results.values()
        )
        return {
            **status,
            "revision": override["revision"],
            "state": state,
            "retryable": retryable,
        }

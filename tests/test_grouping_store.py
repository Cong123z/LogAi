from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from logai.storage.grouping import (
    GroupingCycleError,
    GroupingOverrideStore,
    GroupingRevisionConflict,
    grouping_revision,
)


def test_empty_initialization_and_deterministic_revision():
    with tempfile.TemporaryDirectory() as tmp:
        store = GroupingOverrideStore(
            Path(tmp) / "overrides.json", Path(tmp) / "status.json"
        )
        snapshot = store.load_overrides()
        assert snapshot["revision"] == grouping_revision({}, {})
        assert snapshot["assignments"] == {}


def test_manual_assignment_is_atomic_and_conflicts_are_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        store = GroupingOverrideStore(
            Path(tmp) / "overrides.json", Path(tmp) / "status.json"
        )
        original = store.load_overrides()
        group_id, changed = store.create_manual_group_assignment(
            "T1", original["revision"]
        )
        assert group_id == "G_MANUAL_001"
        assert changed["assignments"]["T1"]["target_id"] == group_id
        assert store.load_overrides()["revision"] == changed["revision"]
        with pytest.raises(GroupingRevisionConflict):
            store.create_manual_group_assignment("T2", original["revision"])


def test_manual_group_numbers_are_monotonic_across_restart_and_pruning():
    with tempfile.TemporaryDirectory() as tmp:
        overrides = Path(tmp) / "overrides.json"
        status = Path(tmp) / "status.json"
        store = GroupingOverrideStore(overrides, status)
        first = store.load_overrides()
        first_id, second = store.create_manual_group_assignment(
            "T1", first["revision"]
        )
        second_id, third = store.create_manual_group_assignment(
            "T2", second["revision"], effective_group_ids={first_id}
        )
        assert (first_id, second_id) == ("G_MANUAL_001", "G_MANUAL_002")

        # Moving T1 away prunes its now-unused manual group, but the durable
        # counter prevents that retired identity from being allocated again.
        fourth = store.replace_assignment(
            "T1",
            "anchor",
            "T2",
            third["revision"],
            effective_group_ids={second_id},
        )
        restarted = GroupingOverrideStore(overrides, status)
        third_id, fifth = restarted.create_manual_group_assignment(
            "T3", fourth["revision"], effective_group_ids={second_id}
        )

        assert first_id not in fifth["manual_groups"]
        assert third_id == "G_MANUAL_003"
        assert fifth["next_manual_group_number"] == 4


def test_legacy_hashed_manual_group_remains_readable():
    with tempfile.TemporaryDirectory() as tmp:
        overrides = Path(tmp) / "overrides.json"
        legacy_group_id = "G_MANUAL_a1b2c3d4e5f6"
        assignments = {
            "T1": {
                "target_kind": "manual_group",
                "target_id": legacy_group_id,
                "assigned_at": 1.0,
            }
        }
        manual_groups = {legacy_group_id: {"created_at": 1.0}}
        overrides.write_text(json.dumps({
            "schema_version": 1,
            "revision": grouping_revision(assignments, manual_groups),
            "updated_at": 1.0,
            "assignments": assignments,
            "manual_groups": manual_groups,
        }), encoding="utf-8")

        store = GroupingOverrideStore(overrides, Path(tmp) / "status.json")
        snapshot = store.load_overrides()

        assert legacy_group_id in snapshot["manual_groups"]
        assert snapshot["next_manual_group_number"] == 1


def test_anchor_cycle_is_rejected_without_changing_revision():
    with tempfile.TemporaryDirectory() as tmp:
        store = GroupingOverrideStore(
            Path(tmp) / "overrides.json", Path(tmp) / "status.json"
        )
        first = store.load_overrides()
        second = store.replace_assignment("T1", "anchor", "T2", first["revision"])
        with pytest.raises(GroupingCycleError):
            store.replace_assignment("T2", "anchor", "T1", second["revision"])
        assert store.load_overrides()["revision"] == second["revision"]


def test_status_freshness_distinguishes_applied_from_unavailable():
    with tempfile.TemporaryDirectory() as tmp:
        store = GroupingOverrideStore(
            Path(tmp) / "overrides.json", Path(tmp) / "status.json"
        )
        revision = store.load_overrides()["revision"]
        store.write_status({
            "applied_revision": revision,
            "attempted_revision": revision,
            "last_attempt_at": 100.0,
            "last_applied_at": 100.0,
            "last_heartbeat_at": 100.0,
            "state": "applied",
            "error": None,
            "results": {},
            "unresolved": {},
        })
        assert store.synchronization_status(stale_seconds=10, now=105)["state"] == "applied"
        assert store.synchronization_status(stale_seconds=10, now=111)["state"] == "engine_unavailable"


def test_unchanged_check_does_not_reparse(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        store = GroupingOverrideStore(
            Path(tmp) / "overrides.json", Path(tmp) / "status.json"
        )
        store.load_overrides()
        monkeypatch.setattr(store, "_read", lambda _path: (_ for _ in ()).throw(AssertionError()))
        assert store.load_if_changed() is None

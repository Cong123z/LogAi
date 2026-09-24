from __future__ import annotations

import tempfile

import numpy as np

from logai.config import StorageConfig
from logai.grouping.assignment_manager import GroupAssignmentManager
from logai.models import GroupState, TemplateState
from logai.storage.grouping import grouping_revision
from logai.storage.registries import GroupRegistry, TemplateRegistry


def _snapshot(assignments, manual_groups=None):
    manual_groups = manual_groups or {}
    return {
        "schema_version": 1,
        "revision": grouping_revision(assignments, manual_groups),
        "updated_at": 1.0,
        "assignments": assignments,
        "manual_groups": manual_groups,
    }


def test_realtime_move_rebuilds_centroid_and_deletes_empty_source():
    with tempfile.TemporaryDirectory() as tmp:
        storage = StorageConfig(base_dir=tmp)
        templates = TemplateRegistry(storage)
        groups = GroupRegistry(storage)
        templates.upsert(TemplateState("T1", "one", "api", event_count=2, group_id="GA"))
        templates.upsert(TemplateState("T2", "two", "api", event_count=5, group_id="GB"))
        templates.set_embedding("T1", np.array([1.0, 0.0]))
        templates.set_embedding("T2", np.array([0.0, 1.0]))
        groups.replace_all(
            [
                GroupState("GA", template_ids=["T1"], event_count=2),
                GroupState("GB", template_ids=["T2"], event_count=5),
            ],
            {"GA": np.array([1.0, 0.0]), "GB": np.array([0.0, 1.0])},
        )
        assignment = {"target_kind": "anchor", "target_id": "T2", "assigned_at": 1.0}
        outcome = GroupAssignmentManager(templates, groups).apply_realtime(
            _snapshot({"T1": assignment})
        )

        assert outcome.deleted_groups == {"GA"}
        assert groups.get("GA") is None
        assert groups.get_centroid("GA") is None
        assert templates.get("T1").group_id == "GB"
        rebuilt = groups.get("GB")
        assert rebuilt.template_ids == ["T1", "T2"]
        assert rebuilt.event_count == 7
        assert np.isclose(np.linalg.norm(groups.get_centroid("GB")), 1.0)


def test_missing_anchor_is_retryable_and_preserves_mapping():
    with tempfile.TemporaryDirectory() as tmp:
        storage = StorageConfig(base_dir=tmp)
        templates = TemplateRegistry(storage)
        groups = GroupRegistry(storage)
        state = TemplateState("T1", "one", "api", group_id="GA")
        templates.upsert(state)
        manager = GroupAssignmentManager(templates, groups)
        resolution = manager.resolve(
            {"T1": "GA"},
            {"T1": state},
            _snapshot({
                "T1": {"target_kind": "anchor", "target_id": "MISSING", "assigned_at": 1.0}
            }),
        )
        assert resolution.effective_mapping["T1"] == "GA"
        assert resolution.results["T1"]["reason_code"] == "missing_anchor"
        assert resolution.results["T1"]["retryable"] is True


def test_unresolved_anchor_chain_reports_every_source():
    with tempfile.TemporaryDirectory() as tmp:
        storage = StorageConfig(base_dir=tmp)
        templates = TemplateRegistry(storage)
        groups = GroupRegistry(storage)
        first = TemplateState("T1", "one", "api", group_id="GA")
        second = TemplateState("T2", "two", "api", group_id="GB")
        manager = GroupAssignmentManager(templates, groups)
        assignments = {
            "T1": {
                "target_kind": "anchor",
                "target_id": "MISSING",
                "assigned_at": 1.0,
            },
            "T2": {
                "target_kind": "anchor",
                "target_id": "T1",
                "assigned_at": 1.0,
            },
        }

        resolution = manager.resolve(
            {"T1": "GA", "T2": "GB"},
            {"T1": first, "T2": second},
            _snapshot(assignments),
        )

        assert resolution.effective_mapping == {"T1": "GA", "T2": "GB"}
        assert set(resolution.results) == {"T1", "T2"}
        assert resolution.results["T2"]["state"] == "unresolved"
        assert resolution.results["T2"]["retryable"] is True


def test_replace_all_removes_stale_groups_and_centroids():
    with tempfile.TemporaryDirectory() as tmp:
        groups = GroupRegistry(StorageConfig(base_dir=tmp))
        groups.replace_all([GroupState("OLD")], {"OLD": np.array([1.0, 0.0])})
        groups.replace_all([GroupState("NEW")], {"NEW": np.array([0.0, 1.0])})
        assert groups.get("OLD") is None
        assert groups.get_centroid("OLD") is None
        assert groups.get("NEW") is not None


def test_grouping_commit_preserves_latest_documentation_fields():
    with tempfile.TemporaryDirectory() as tmp:
        groups = GroupRegistry(StorageConfig(base_dir=tmp))
        groups.replace_all(
            [GroupState("GA", documented=False)],
            {"GA": np.array([1.0, 0.0])},
        )
        stale_rebuild = GroupState("GA", template_ids=["T1"], documented=False)
        current = groups.get("GA")
        current.documented = True
        current.documentation_id = "DOC-1"
        current.documentation_source = "automatic"
        groups.upsert(current)

        groups.apply_grouping_changes(
            {"GA": stale_rebuild},
            {"GA": np.array([0.0, 1.0])},
            set(),
        )

        committed = groups.get("GA")
        assert committed.documented is True
        assert committed.documentation_id == "DOC-1"
        assert committed.documentation_source == "automatic"

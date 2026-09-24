"""Resolve grouping intent and rebuild affected registry state."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Dict, Iterable, Optional

import numpy as np

from logai.grouping import PENDING_GROUP_ID
from logai.models import GroupState, TemplateState
from logai.storage.registries import GroupRegistry, TemplateRegistry


@dataclass
class AssignmentResolution:
    effective_mapping: Dict[str, Optional[str]]
    changed_templates: set[str] = field(default_factory=set)
    affected_groups: set[str] = field(default_factory=set)
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    unresolved: Dict[str, str] = field(default_factory=dict)


@dataclass
class AssignmentOutcome:
    resolution: AssignmentResolution
    deleted_groups: set[str]


class GroupAssignmentError(RuntimeError):
    def __init__(self, reason_code: str, message: str, *, rollback_succeeded: bool = True):
        super().__init__(message)
        self.reason_code = reason_code
        self.rollback_succeeded = rollback_succeeded


class GroupAssignmentManager:
    def __init__(
        self,
        templates: TemplateRegistry,
        groups: GroupRegistry,
        embed_one: Optional[Callable[[str], np.ndarray]] = None,
    ):
        self.templates = templates
        self.groups = groups
        self.embed_one = embed_one

    @staticmethod
    def _result(
        state: str,
        *,
        reason_code: Optional[str] = None,
        message: Optional[str] = None,
        retryable: Optional[bool] = None,
        effective_group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        value: Dict[str, Any] = {"state": state}
        if reason_code is not None:
            value["reason_code"] = reason_code
        if message is not None:
            value["message"] = message[:500]
        if retryable is not None:
            value["retryable"] = retryable
        if effective_group_id is not None:
            value["effective_group_id"] = effective_group_id
        return value

    def resolve(
        self,
        base_mapping: Dict[str, Optional[str]],
        template_states: Dict[str, TemplateState],
        override_snapshot: Dict[str, Any],
    ) -> AssignmentResolution:
        assignments = override_snapshot.get("assignments", {})
        manual_groups = override_snapshot.get("manual_groups", {})
        effective = dict(base_mapping)
        memo: Dict[str, Optional[str]] = {}
        visiting: set[str] = set()
        results: Dict[str, Dict[str, Any]] = {}
        unresolved: Dict[str, str] = {}

        def unresolved_result(
            source_id: str, reason_code: str, message: str, retryable: bool
        ) -> None:
            results[source_id] = self._result(
                "unresolved",
                reason_code=reason_code,
                message=message,
                retryable=retryable,
            )
            unresolved[source_id] = message[:500]

        def resolve_one(template_id: str, root_id: str) -> Optional[str]:
            if template_id in memo:
                value = memo[template_id]
                if value is None and root_id not in unresolved:
                    unresolved_result(
                        root_id,
                        "missing_target_group",
                        f"Anchor template {template_id} has no effective group",
                        True,
                    )
                return value
            if template_id in visiting:
                unresolved_result(root_id, "anchor_cycle", f"Anchor cycle includes {template_id}", False)
                return None
            if template_id not in template_states:
                code = "missing_source" if template_id == root_id else "missing_anchor"
                label = "Source" if code == "missing_source" else "Anchor"
                unresolved_result(root_id, code, f"{label} template {template_id} is not present", True)
                return None
            assignment = assignments.get(template_id)
            if not assignment:
                return effective.get(template_id)
            visiting.add(template_id)
            try:
                kind = assignment.get("target_kind")
                target_id = str(assignment.get("target_id") or "")
                if kind == "manual_group":
                    if target_id not in manual_groups:
                        unresolved_result(
                            root_id,
                            "missing_target_group",
                            f"Manual group {target_id} is not defined",
                            False,
                        )
                        value = None
                    else:
                        value = target_id
                elif kind == "anchor":
                    value = resolve_one(target_id, root_id)
                    if value == PENDING_GROUP_ID:
                        unresolved_result(
                            root_id,
                            "missing_target_group",
                            f"Anchor template {target_id} is pending",
                            False,
                        )
                        value = None
                else:
                    unresolved_result(
                        root_id,
                        "invalid_override_schema",
                        f"Assignment {template_id} has an invalid target kind",
                        False,
                    )
                    value = None
            finally:
                visiting.discard(template_id)
            memo[template_id] = value
            return value

        for source_id in sorted(assignments):
            if source_id not in template_states:
                unresolved_result(
                    source_id,
                    "missing_source",
                    f"Source template {source_id} is not present",
                    True,
                )
                continue
            group_id = resolve_one(source_id, source_id)
            if group_id is None or source_id in unresolved:
                continue
            effective[source_id] = group_id
            results[source_id] = self._result(
                "applied", effective_group_id=group_id
            )

        changed: set[str] = set()
        affected: set[str] = set()
        for template_id, new_group in effective.items():
            old_group = base_mapping.get(template_id)
            if old_group == new_group:
                continue
            changed.add(template_id)
            if old_group and old_group != PENDING_GROUP_ID:
                affected.add(old_group)
            if new_group and new_group != PENDING_GROUP_ID:
                affected.add(new_group)
        return AssignmentResolution(effective, changed, affected, results, unresolved)

    @staticmethod
    def build_groups(
        effective_mapping: Dict[str, Optional[str]],
        template_states: Dict[str, TemplateState],
        embeddings: Dict[str, np.ndarray],
        existing_groups: Dict[str, GroupState],
        group_ids: Optional[Iterable[str]] = None,
    ) -> tuple[Dict[str, GroupState], Dict[str, np.ndarray], set[str]]:
        selected = set(group_ids) if group_ids is not None else {
            gid for gid in effective_mapping.values() if gid and gid != PENDING_GROUP_ID
        }
        members: Dict[str, list[TemplateState]] = {group_id: [] for group_id in selected}
        for template_id, group_id in effective_mapping.items():
            if group_id not in members:
                continue
            state = template_states.get(template_id)
            if state is not None:
                members[group_id].append(state)

        groups: Dict[str, GroupState] = {}
        centroids: Dict[str, np.ndarray] = {}
        deleted: set[str] = set()
        for group_id in sorted(selected):
            group_members = members.get(group_id, [])
            if not group_members:
                deleted.add(group_id)
                continue
            group_members.sort(key=lambda state: (-state.event_count, state.template_id))
            representative = group_members[0]
            previous = existing_groups.get(group_id)
            group = GroupState(
                group_id=group_id,
                service=representative.service or "unknown",
                module=representative.module,
                template_ids=sorted(state.template_id for state in group_members),
                representative_template=representative.template_text,
                first_seen=min(state.first_seen for state in group_members),
                last_seen=max(state.last_seen for state in group_members),
                event_count=sum(state.event_count for state in group_members),
            )
            if previous is not None:
                for field_name in (
                    "error_code",
                    "documented",
                    "documentation_id",
                    "confidence",
                    "documentation_source",
                    "severity",
                    "active",
                ):
                    setattr(group, field_name, getattr(previous, field_name))
            # Training mocks and legacy registries can contain a member whose
            # embedding artifact is absent. Use every available vector and fail
            # only when the group has no usable representation at all.
            vectors = [
                list(embeddings[state.template_id])
                for state in group_members
                if state.template_id in embeddings
            ]
            if not vectors or not vectors[0] or any(
                len(vector) != len(vectors[0]) for vector in vectors
            ):
                raise GroupAssignmentError(
                    "centroid_failed", f"Invalid embeddings for {group_id}"
                )
            centroid_values = [
                sum(vector[index] for vector in vectors) / len(vectors)
                for index in range(len(vectors[0]))
            ]
            norm = math.sqrt(sum(float(value) ** 2 for value in centroid_values))
            if not math.isfinite(norm) or norm <= 0:
                raise GroupAssignmentError(
                    "centroid_failed", f"Unable to compute centroid for {group_id}"
                )
            groups[group_id] = group
            centroids[group_id] = np.array(
                [float(value) / norm for value in centroid_values], dtype=np.float64
            )
        return groups, centroids, deleted

    def apply_realtime(self, override_snapshot: Dict[str, Any]) -> AssignmentOutcome:
        template_states = {
            state.template_id: state for state in self.templates.all_templates()
        }
        base_mapping = {
            template_id: state.group_id for template_id, state in template_states.items()
        }
        resolution = self.resolve(base_mapping, template_states, override_snapshot)

        embeddings = self.templates.all_embeddings()
        for template_id in list(resolution.changed_templates):
            if template_id in embeddings:
                continue
            state = template_states[template_id]
            try:
                if self.embed_one is None:
                    raise RuntimeError("No embedder is configured")
                embedding = self.embed_one(state.template_text)
                embeddings[template_id] = embedding
                self.templates.set_embedding(template_id, embedding, flush=False)
            except Exception as exc:  # noqa: BLE001
                resolution.effective_mapping[template_id] = base_mapping.get(template_id)
                resolution.changed_templates.discard(template_id)
                message = f"Embedding failed for {template_id}: {exc}"
                resolution.results[template_id] = self._result(
                    "unresolved",
                    reason_code="embedding_failed",
                    message=message,
                    retryable=False,
                )
                resolution.unresolved[template_id] = message[:500]

        resolution.affected_groups = set()
        for template_id in resolution.changed_templates:
            old_group = base_mapping.get(template_id)
            new_group = resolution.effective_mapping.get(template_id)
            if old_group and old_group != PENDING_GROUP_ID:
                resolution.affected_groups.add(old_group)
            if new_group and new_group != PENDING_GROUP_ID:
                resolution.affected_groups.add(new_group)

        if not resolution.affected_groups:
            return AssignmentOutcome(resolution, set())

        existing_groups = {state.group_id: state for state in self.groups.all_groups()}
        try:
            rebuilt, centroids, deleted = self.build_groups(
                resolution.effective_mapping,
                template_states,
                embeddings,
                existing_groups,
                resolution.affected_groups,
            )
        except GroupAssignmentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GroupAssignmentError("centroid_failed", str(exc)) from exc

        old_templates = {
            template_id: template_states[template_id]
            for template_id in resolution.changed_templates
        }
        old_groups = {
            group_id: existing_groups.get(group_id)
            for group_id in resolution.affected_groups
        }
        old_centroids = {
            group_id: self.groups.get_centroid(group_id)
            for group_id in resolution.affected_groups
        }
        try:
            for template_id in resolution.changed_templates:
                self.templates.set_group(
                    template_id,
                    str(resolution.effective_mapping[template_id]),
                    flush=False,
                )
            self.groups.apply_grouping_changes(rebuilt, centroids, deleted, flush=False)
            self.templates.flush()
            self.groups.flush()
        except Exception as exc:  # noqa: BLE001
            rollback_succeeded = True
            try:
                for template_id, state in old_templates.items():
                    self.templates.upsert(state, flush=False)
                restored_groups = {
                    gid: state for gid, state in old_groups.items() if state is not None
                }
                restore_deleted = {
                    gid for gid, state in old_groups.items() if state is None
                }
                restored_centroids = {
                    gid: value for gid, value in old_centroids.items() if value is not None
                }
                self.groups.apply_grouping_changes(
                    restored_groups, restored_centroids, restore_deleted, flush=False
                )
                self.templates.flush()
                self.groups.flush()
            except Exception:  # noqa: BLE001
                rollback_succeeded = False
            raise GroupAssignmentError(
                "registry_write_failed",
                str(exc),
                rollback_succeeded=rollback_succeeded,
            ) from exc
        return AssignmentOutcome(resolution, deleted)

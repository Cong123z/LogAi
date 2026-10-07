"""Template Explorer Web API.

Reads engine registries and manages the shared documentation corpus/overrides.
The web process never writes engine-owned template or group registry files.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory

from logai.alert.alert_state_machine import group_id_key
from logai.incident.profiles import LLMProfileStore, ProfileError, ProfileStoreUnreadable
from logai.incident.cases import add_case, delete_case, load_cases, update_case
from logai.incident.requests import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    add_request,
    load_requests,
    request_key,
)
from logai.models import DEFAULT_LEVEL, LEVEL_RANK
from logai.storage.documentation import (
    DocumentInUse,
    DocumentationCorpusStore,
    DocumentationStoreError,
    RevisionConflict,
    group_fingerprint,
)
from logai.storage.index_selection import (
    IndexSelectionConflict,
    IndexSelectionError,
    IndexSelectionStore,
    read_json,
)
from logai.storage.grouping import (
    MANUAL_GROUP_RE,
    GroupingCycleError,
    GroupingOverrideStore,
    GroupingRevisionConflict,
    GroupingStoreError,
    GroupingTargetError,
)

logger = logging.getLogger("logai.web")

PENDING_GROUP_ID = "UNASSIGNED_PENDING"
# A request the engine has not picked up within this time is shown as failed.
STALE_REQUEST_SECONDS = 600
# Upper bound for "Triage all unknown" so one click cannot flood the LLM queue.
TRIAGE_ALL_LIMIT = 50


def create_app(
    data_dir: str = "data",
    corpus_path: str | None = None,
    overrides_path: str | None = None,
    status_path: str | None = None,
    seed_path: str | None = None,
    grouping_overrides_path: str | None = None,
    grouping_status_path: str | None = None,
    grouping_stale_seconds: float = 45.0,
    service_analysis_path: str | None = None,
    analysis_requests_path: str | None = None,
    llm_profiles_path: str | None = None,
    template_triage_path: str | None = None,
    index_selection_path: str | None = None,
    index_status_path: str | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    base = Path(data_dir)
    root = Path(__file__).resolve().parents[2]
    documentation = DocumentationCorpusStore(
        corpus_path or base / "documentation_corpus.json",
        overrides_path or base / "documentation_overrides.json",
        status_path or base / "documentation_status.json",
        seed_path or root / "docs" / "documentation_corpus.yaml",
    )
    grouping = GroupingOverrideStore(
        grouping_overrides_path or base / "grouping_overrides.json",
        grouping_status_path or base / "grouping_status.json",
    )

    service_analysis_file = Path(service_analysis_path or base / "service_analysis.json")
    template_triage_file = Path(template_triage_path or base / "template_triage.json")
    incident_analysis_file = base / "incident_analysis.json"
    incident_cases_file = base / "incident_cases.json"
    llm_profiles = LLMProfileStore(llm_profiles_path or base / "llm_profiles.json")
    index_selection = IndexSelectionStore(
        index_selection_path or base / "es_index_selection.json"
    )
    index_status_file = Path(index_status_path or base / "es_index_status.json")
    analysis_requests_file = Path(analysis_requests_path or base / "analysis_requests.json")

    def _json_body() -> Optional[Dict[str, Any]]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None

    def _load_json(filename: str) -> Dict[str, Any]:
        path = base / filename
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _is_known(group_id: Optional[str]) -> bool:
        return group_id is not None and group_id != PENDING_GROUP_ID

    def _mutation_error(exc: Exception):
        if isinstance(exc, GroupingRevisionConflict):
            return jsonify({
                "error": "revision_conflict",
                "message": str(exc),
                "current_revision": exc.current_revision,
            }), 409
        if isinstance(exc, GroupingCycleError):
            return jsonify({"error": "anchor_cycle", "message": str(exc)}), 409
        if isinstance(exc, RevisionConflict):
            return jsonify({
                "error": "revision_conflict",
                "message": str(exc),
                "current_revision": exc.current_revision,
            }), 409
        if isinstance(exc, DocumentInUse):
            return jsonify({
                "error": "document_in_use",
                "message": str(exc),
                "group_ids": exc.group_ids,
            }), 409
        if isinstance(exc, KeyError):
            return jsonify({"error": "not_found", "message": str(exc.args[0])}), 404
        if isinstance(exc, DocumentationStoreError):
            return jsonify({"error": "invalid_request", "message": str(exc)}), 400
        if isinstance(exc, (GroupingTargetError, GroupingStoreError)):
            return jsonify({"error": "invalid_request", "message": str(exc)}), 400
        logger.exception("Web mutation failed")
        return jsonify({"error": "storage_error", "message": str(exc)}), 500

    def _document_group_details(
        groups: Dict[str, Any], group_ids: List[str]
    ) -> List[Dict[str, str]]:
        details: List[Dict[str, str]] = []
        for group_id in sorted(set(group_ids)):
            group = groups.get(group_id, {})
            if not isinstance(group, dict):
                group = {}
            details.append({
                "group_id": group_id,
                "service": str(group.get("service") or "unknown"),
                "representative_template": str(
                    group.get("representative_template") or ""
                ),
            })
        return details

    def _grouping_context() -> tuple[Dict[str, Any], Dict[str, Any]]:
        synchronization = grouping.synchronization_status(
            stale_seconds=grouping_stale_seconds
        )
        try:
            overrides = grouping.load_overrides()
        except GroupingStoreError:
            overrides = {"revision": synchronization.get("revision"), "assignments": {}}
        return overrides, synchronization

    def _documentation_mutations_blocked() -> tuple[bool, Dict[str, Any]]:
        overrides, synchronization = _grouping_context()
        blocked = bool(overrides.get("assignments")) and (
            synchronization.get("applied_revision") != overrides.get("revision")
        )
        return blocked, synchronization

    def _grouping_pending_response(synchronization: Dict[str, Any]):
        return jsonify({
            "error": "grouping_pending",
            "message": "Wait for template grouping to finish before changing group documentation",
            "grouping_state": synchronization.get("state"),
            "grouping_revision": synchronization.get("revision"),
            "applied_revision": synchronization.get("applied_revision"),
        }), 409

    @staticmethod
    def _template_grouping_fields(
        template_id: str,
        effective_group_id: Optional[str],
        overrides: Dict[str, Any],
        synchronization: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "manual_assignment": overrides.get("assignments", {}).get(template_id),
            "effective_group_id": effective_group_id,
            "grouping_result": synchronization.get("results", {}).get(template_id),
        }

    # ── API Routes ────────────────────────────────────────────────────

    @app.route("/api/templates")
    def list_templates():
        """List templates with optional filters.

        Query params:
          - status: "known" | "unknown" | "all" (default: "all")
          - service: filter by service name (substring match)
          - level: filter by log level (exact match, case-insensitive)
          - search: search template text (substring, case-insensitive)
          - sort: field to sort by (default: "last_seen")
          - order: "asc" | "desc" (default: "desc")
          - page: page number (default: 1)
          - per_page: items per page (default: 50)
        """
        templates = _load_json("template_registry.json")
        groups = _load_json("group_registry.json")
        grouping_overrides, grouping_sync = _grouping_context()

        # Build response list with enrichment from group registry
        result: List[Dict[str, Any]] = []
        for tid, tmpl in templates.items():
            gid = tmpl.get("group_id")
            known = _is_known(gid)
            group = groups.get(gid, {}) if gid and known else {}

            result.append({
                "template_id": tid,
                "template_text": tmpl.get("template_text", ""),
                "service": tmpl.get("service", "unknown"),
                "level": tmpl.get("level", "INFO"),
                "event_count": tmpl.get("event_count", 0),
                "first_seen": tmpl.get("first_seen", 0),
                "last_seen": tmpl.get("last_seen", 0),
                "group_id": gid,
                "status": "known" if known else "unknown",
                # Group enrichment
                "group_name": group.get("representative_template", ""),
                "documented": group.get("documented", False),
                "error_code": group.get("error_code", ""),
                **_template_grouping_fields(
                    tid, gid, grouping_overrides, grouping_sync
                ),
            })

        # ── Filters ──
        status = request.args.get("status", "all").lower()
        if status == "known":
            result = [t for t in result if t["status"] == "known"]
        elif status == "unknown":
            result = [t for t in result if t["status"] == "unknown"]

        service = request.args.get("service", "").strip()
        if service:
            result = [t for t in result
                      if service.lower() in t["service"].lower()]

        level = request.args.get("level", "").strip().upper()
        if level:
            result = [t for t in result if t["level"].upper() == level]

        search = request.args.get("search", "").strip()
        if search:
            q = search.lower()
            result = [t for t in result
                      if q in t["template_text"].lower()
                      or q in t["template_id"].lower()]

        # ── Sort ──
        sort_field = request.args.get("sort", "last_seen")
        sort_order = request.args.get("order", "desc")
        reverse = sort_order == "desc"
        result.sort(
            key=lambda x: (x.get(sort_field) is None, x.get(sort_field, "")),
            reverse=reverse,
        )

        # ── Stats (computed from full, unfiltered set) ──
        all_services = set()
        all_levels = set()
        known_count = 0
        unknown_count = 0
        for t in templates.values():
            all_services.add(t.get("service", "unknown"))
            all_levels.add((t.get("level", "INFO") or "INFO").upper())
            if _is_known(t.get("group_id")):
                known_count += 1
            else:
                unknown_count += 1

        # ── Pagination ──
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
        total = len(result)
        start = (page - 1) * per_page
        items = result[start:start + per_page]

        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
            "stats": {
                "total": known_count + unknown_count,
                "known": known_count,
                "unknown": unknown_count,
                "services": sorted(all_services),
                "levels": sorted(all_levels),
            },
            "grouping_revision": grouping_overrides.get("revision"),
            "grouping_synchronization": grouping_sync,
        })

    @app.route("/api/templates/<template_id>")
    def get_template(template_id: str):
        """Get detailed info for a single template."""
        templates = _load_json("template_registry.json")
        tmpl = templates.get(template_id)
        if not tmpl:
            return jsonify({"error": "Template not found"}), 404

        gid = tmpl.get("group_id")
        groups = _load_json("group_registry.json")
        group = groups.get(gid, {}) if gid and _is_known(gid) else {}
        grouping_overrides, grouping_sync = _grouping_context()

        return jsonify({
            **tmpl,
            "status": "known" if _is_known(gid) else "unknown",
            "group_info": group,
            **_template_grouping_fields(
                template_id, gid, grouping_overrides, grouping_sync
            ),
            "grouping_revision": grouping_overrides.get("revision"),
            "grouping_synchronization": grouping_sync,
        })

    @app.route("/api/grouping/status")
    def grouping_status():
        return jsonify(grouping.synchronization_status(
            stale_seconds=grouping_stale_seconds
        ))

    @app.route("/api/health")
    def health():
        grouping_sync = grouping.synchronization_status(
            stale_seconds=grouping_stale_seconds
        )
        documentation_sync = documentation.synchronization_status()
        if grouping_sync.get("state") == "engine_unavailable":
            state = "unavailable"
        elif grouping_sync.get("state") in {"failed", "partial"} or documentation_sync.get("state") == "failed":
            state = "degraded"
        else:
            state = "healthy"
        return jsonify({
            "state": state,
            "engine_alive": grouping_sync.get("state") != "engine_unavailable",
            "last_heartbeat_at": grouping_sync.get("last_heartbeat_at"),
            **grouping_sync.get("runtime", {}),
            "grouping": grouping_sync,
            "documentation": documentation_sync,
        }), (503 if state == "unavailable" else 200)

    @app.route("/api/templates/<template_id>/group", methods=["PUT"])
    def assign_template_group(template_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "invalid_request", "message": "JSON body is required"}), 400
        templates = _load_json("template_registry.json")
        source = templates.get(template_id)
        if source is None:
            return jsonify({"error": "not_found", "message": template_id}), 404
        groups = _load_json("group_registry.json")
        target_group_id = payload.get("target_group_id")
        create_new = payload.get("create_new") is True
        if create_new == bool(target_group_id):
            return jsonify({
                "error": "invalid_request",
                "message": "Provide exactly one of target_group_id or create_new=true",
            }), 400
        expected_revision = payload.get("expected_revision")
        if not isinstance(expected_revision, str) or not expected_revision:
            return jsonify({
                "error": "invalid_request", "message": "expected_revision is required"
            }), 400

        try:
            current_overrides = grouping.load_overrides()
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)
        if expected_revision != current_overrides["revision"]:
            return _mutation_error(
                GroupingRevisionConflict(current_overrides["revision"])
            )

        current_group_id = source.get("group_id")
        current_sync = grouping.synchronization_status(
            stale_seconds=grouping_stale_seconds
        )
        if (
            not create_new
            and target_group_id == current_group_id
            and current_sync.get("applied_revision") == current_overrides["revision"]
        ):
            return jsonify({
                "template_id": template_id,
                "requested_target": {
                    "kind": "effective_group",
                    "id": target_group_id,
                    "current_group_id": current_group_id,
                },
                "revision": current_sync.get("revision"),
                "state": "applied",
            })

        # The old manual group can be pruned immediately when this source is its
        # only effective member and no remaining assignment references it.
        effective_group_ids = set(groups)
        old_group = groups.get(current_group_id, {})
        if old_group.get("template_ids") == [template_id]:
            effective_group_ids.discard(current_group_id)
        try:
            if create_new:
                manual_group_id, snapshot = grouping.create_manual_group_assignment(
                    template_id,
                    expected_revision,
                    effective_group_ids=effective_group_ids,
                )
                requested = {
                    "kind": "manual_group",
                    "id": manual_group_id,
                    "current_group_id": current_group_id,
                }
            else:
                if not isinstance(target_group_id, str) or target_group_id == PENDING_GROUP_ID:
                    raise GroupingTargetError("target_group_id is invalid or pending")
                target_group = groups.get(target_group_id)
                if target_group is None:
                    return jsonify({"error": "not_found", "message": target_group_id}), 404
                if MANUAL_GROUP_RE.fullmatch(target_group_id):
                    if target_group_id not in current_overrides.get("manual_groups", {}):
                        raise GroupingTargetError(f"Unknown manual group: {target_group_id}")
                    target_kind = "manual_group"
                    target_id = target_group_id
                else:
                    candidates = [
                        tid for tid in target_group.get("template_ids", [])
                        if tid != template_id and tid in templates
                    ]
                    if not candidates:
                        raise GroupingTargetError(
                            "The target group has no eligible anchor template"
                        )
                    candidates.sort(
                        key=lambda tid: (-int(templates[tid].get("event_count", 0)), tid)
                    )
                    target_kind = "anchor"
                    target_id = candidates[0]
                snapshot = grouping.replace_assignment(
                    template_id,
                    target_kind,
                    target_id,
                    expected_revision,
                    effective_group_ids=effective_group_ids,
                )
                requested = {
                    "kind": target_kind,
                    "id": target_id,
                    "current_group_id": target_group_id,
                }
            return jsonify({
                "template_id": template_id,
                "requested_target": requested,
                "revision": snapshot["revision"],
                "state": "pending",
            }), 202
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)

    @app.route("/api/stats")
    def get_stats():
        """Summary statistics."""
        templates = _load_json("template_registry.json")
        services = set()
        levels = set()
        known = unknown = 0
        for t in templates.values():
            services.add(t.get("service", "unknown"))
            levels.add((t.get("level", "INFO") or "INFO").upper())
            if _is_known(t.get("group_id")):
                known += 1
            else:
                unknown += 1
        return jsonify({
            "total": known + unknown,
            "known": known,
            "unknown": unknown,
            "services": sorted(services),
            "levels": sorted(levels),
        })

    @app.route("/api/groups")
    def list_groups():
        """List every persisted semantic group from group_registry.json."""
        groups = _load_json("group_registry.json")
        grouping_overrides, grouping_sync = _grouping_context()
        service = request.args.get("service", "").strip().lower()
        documented = request.args.get("documented", "").strip().lower()
        search = request.args.get("search", "").strip().lower()
        items = []
        override_snapshot = documentation.load_overrides()
        overrides = override_snapshot["overrides"]
        cleared_groups = override_snapshot.get("cleared_groups", {})
        document_ids = {
            entry["id"] for entry in documentation.load_corpus()["entries"]
        }
        templates = _load_json("template_registry.json")
        for gid, group in groups.items():
            item = {"group_id": gid, **group}
            item["templates"] = []
            for template_id in item.get("template_ids", []):
                template = templates.get(template_id, {})
                if not isinstance(template, dict):
                    template = {}
                item["templates"].append({
                    "template_id": template_id,
                    "template_text": template.get("template_text", ""),
                    "service": template.get("service", "unknown"),
                    "level": template.get("level", DEFAULT_LEVEL),
                    "event_count": template.get("event_count", 0),
                    "first_seen": template.get("first_seen", 0),
                    "last_seen": template.get("last_seen", 0),
                })
            override = overrides.get(gid)
            texts = [
                templates.get(template_id, {}).get("template_text", "")
                for template_id in item.get("template_ids", [])
            ]
            current_fingerprint = group_fingerprint(item.get("template_ids", []), texts)
            item["manual_documentation_id"] = (
                override.get("documentation_id") if isinstance(override, dict) else None
            )
            item["documentation_clear_suppressed"] = gid in cleared_groups
            membership_changed = bool(
                isinstance(override, dict)
                and override.get("group_fingerprint") != current_fingerprint
            )
            item["membership_changed_since_assignment"] = membership_changed
            item["override_stale"] = bool(
                isinstance(override, dict)
                and override.get("documentation_id") not in document_ids
            )
            if service and service not in str(item.get("service", "")).lower():
                continue
            if documented in ("true", "false") and bool(item.get("documented", False)) != (documented == "true"):
                continue
            haystack = f"{gid} {item.get('representative_template', '')}".lower()
            if search and search not in haystack:
                continue
            items.append(item)
        items.sort(key=lambda item: (item.get("event_count", 0), item["group_id"]), reverse=True)
        return jsonify({
            "items": items,
            "total": len(items),
            "override_revision": override_snapshot["revision"],
            "grouping_revision": grouping_overrides.get("revision"),
            "grouping_synchronization": grouping_sync,
            "documentation_mutations_blocked": bool(
                grouping_overrides.get("assignments")
                and grouping_sync.get("applied_revision")
                != grouping_overrides.get("revision")
            ),
        })

    def _alert_items() -> tuple[List[Dict[str, Any]], Dict[str, int]]:
        """Every (service, group) window with its latest alert state and
        LLM analysis (requests not yet picked up are merged in)."""
        groups = _load_json("group_registry.json")
        templates = _load_json("template_registry.json")
        persisted = _load_json("anomaly_state.json")
        analyses = _analyses("window")

        def group_level(group: Dict[str, Any], service: str) -> str:
            candidates = [
                templates.get(template_id, {})
                for template_id in group.get("template_ids", [])
            ]
            service_candidates = [
                template for template in candidates
                if str(template.get("service", "")).lower() == service.lower()
            ]
            if service_candidates:
                candidates = service_candidates
            levels = [str(template.get("level") or DEFAULT_LEVEL).upper() for template in candidates]
            return max(levels, key=lambda level: LEVEL_RANK.get(level, LEVEL_RANK[DEFAULT_LEVEL]), default=DEFAULT_LEVEL)

        def identity(key: str, raw: Dict[str, Any]) -> tuple[str, str]:
            value = raw.get("group_id")
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                try:
                    decoded = json.loads(key)
                except (json.JSONDecodeError, TypeError):
                    decoded = None
                value = decoded if isinstance(decoded, list) and len(decoded) == 2 else value
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return str(value[0]), str(value[1])
            group_id = str(value or key)
            group = groups.get(group_id, {})
            return str(group.get("service") or "unknown"), group_id

        items: List[Dict[str, Any]] = []
        represented_groups = set()
        for key, raw in persisted.items():
            if not isinstance(raw, dict):
                continue
            service, group_id = identity(key, raw)
            if group_id not in groups:
                continue
            group = groups.get(group_id, {})
            represented_groups.add(group_id)
            items.append({
                "group_id": group_id,
                "service": service,
                "level": group_level(group, service),
                "alert_state": str(raw.get("alert_state") or "NORMAL").upper(),
                "anomaly_score": float(raw.get("anomaly_score") or 0.0),
                "anomaly": bool(raw.get("anomaly", False)),
                "consecutive_anomaly_count": int(raw.get("consecutive_anomaly_count") or 0),
                "timestamp": float(raw.get("timestamp") or 0.0),
                "representative_template": group.get("representative_template", ""),
                "error_code": group.get("error_code", ""),
                "documentation_id": group.get("documentation_id"),
                "key": group_id_key((service, group_id)),
                "analysis": analyses.get(group_id_key((service, group_id))),
            })

        for group_id, group in groups.items():
            if group_id in represented_groups:
                continue
            service = str(group.get("service") or "unknown")
            items.append({
                "group_id": group_id,
                "service": service,
                "level": group_level(group, service),
                "alert_state": "NORMAL",
                "anomaly_score": 0.0,
                "anomaly": False,
                "consecutive_anomaly_count": 0,
                "timestamp": 0.0,
                "representative_template": group.get("representative_template", ""),
                "error_code": group.get("error_code", ""),
                "documentation_id": group.get("documentation_id"),
                "key": group_id_key((service, group_id)),
                "analysis": analyses.get(group_id_key((service, group_id))),
            })

        state_priority = {"NORMAL": 0, "COOLING": 1, "WARMING": 2, "ALERTING": 3}
        items.sort(
            key=lambda item: (
                state_priority.get(item["alert_state"], -1),
                LEVEL_RANK.get(item["level"], LEVEL_RANK[DEFAULT_LEVEL]),
                item["anomaly_score"],
            ),
            reverse=True,
        )
        counts = {state: 0 for state in ("NORMAL", "WARMING", "ALERTING", "COOLING")}
        for item in items:
            counts[item["alert_state"]] = counts.get(item["alert_state"], 0) + 1
        return items, counts

    @app.route("/api/alerts")
    def list_alerts():
        """Return the latest persisted alert condition for every current group."""
        items, counts = _alert_items()
        return jsonify({
            "items": items, "total": len(items), "counts": counts, "llm": _llm_engine_state(),
        })

    def _llm_engine_state() -> Dict[str, Any]:
        """Whether the engine can run LLM analysis, and why not (never the key)."""
        status = grouping.synchronization_status(stale_seconds=grouping_stale_seconds)
        runtime = status.get("runtime") or {}
        if status.get("state") == "engine_unavailable":
            return {
                "alive": False, "llm_enabled": False,
                "llm_profile_id": runtime.get("llm_profile_id"), "status": "error",
                "reason": "The analysis engine is not running (no recent heartbeat)",
                "reason_since": status.get("last_heartbeat_at"),
            }
        enabled = runtime.get("llm_enabled") is True
        return {
            "alive": True, "llm_enabled": enabled,
            "llm_profile_id": runtime.get("llm_profile_id"),
            "status": runtime.get("llm_status") or ("ok" if enabled else "disabled"),
            "reason": runtime.get("llm_reason") or (
                "" if enabled else "LLM analysis is disabled on the engine"),
            "reason_since": runtime.get("llm_reason_since"),
        }

    def _known_services() -> List[str]:
        services = {
            str(t.get("service") or "unknown")
            for t in _load_json("template_registry.json").values() if isinstance(t, dict)
        }
        for key in _load_json("anomaly_state.json"):
            try:
                decoded = json.loads(key)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(decoded, list) and len(decoded) == 2:
                services.add(str(decoded[0]))
        return sorted(services)

    # ── AI Insights: on-demand LLM analyses ──

    def _read_records(path: Path) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                records = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return {}
        return records if isinstance(records, dict) else {}

    _record_files = {
        "window": incident_analysis_file,
        "service": service_analysis_file,
        "template": template_triage_file,
    }

    def _analyses(kind: str) -> Dict[str, Any]:
        """Engine records of one kind, keyed like the request ids, with web
        requests merged in: an analyze request not yet picked up shows as
        'requested' (or failed once stale); a pending delete hides the record."""
        records = _read_records(_record_files[kind])
        now = time.time()
        prefix = f"{kind}:"
        for key, (action, at, language) in load_requests(analysis_requests_file).items():
            if not key.startswith(prefix):
                continue
            target = key[len(prefix):]
            record = records.get(target)
            record_at = float(record.get("requested_at") or 0.0) if isinstance(record, dict) else 0.0
            if at <= record_at:
                continue
            if action == "delete":
                records.pop(target, None)
            elif now - at > STALE_REQUEST_SECONDS:
                records[target] = {
                    "status": "failed", "requested_at": at,
                    "error": "The engine did not pick up this request; try again",
                }
            else:
                records[target] = {"status": "requested", "requested_at": at, "language": language}
        return records

    def _known_services() -> List[str]:
        services = {
            str(t.get("service") or "unknown")
            for t in _load_json("template_registry.json").values() if isinstance(t, dict)
        }
        for key in _load_json("anomaly_state.json"):
            try:
                decoded = json.loads(key)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(decoded, list) and len(decoded) == 2:
                services.add(str(decoded[0]))
        return sorted(services)

    def _pending_templates() -> List[Dict[str, Any]]:
        templates = _load_json("template_registry.json")
        return [
            {
                "template_id": tid,
                "template_text": t.get("template_text", ""),
                "service": t.get("service", "unknown"),
                "level": t.get("level", DEFAULT_LEVEL),
                "event_count": t.get("event_count", 0),
                "first_seen": t.get("first_seen", 0),
                "last_seen": t.get("last_seen", 0),
            }
            for tid, t in templates.items()
            if isinstance(t, dict) and not _is_known(t.get("group_id"))
        ]

    def _target_exists(kind: str, target_id: str) -> bool:
        if kind == "window":
            try:
                identity = json.loads(target_id)
            except (TypeError, ValueError):
                return False
            return (
                isinstance(identity, list) and len(identity) == 2
                and identity[1] in _load_json("group_registry.json")
            )
        if kind == "service":
            return target_id in _known_services()
        if kind == "template":
            return target_id in _load_json("template_registry.json")
        return False

    @app.route("/api/insights", methods=["GET"])
    def list_insights():
        windows, counts = _alert_items()
        services = _analyses("service")
        triage = _analyses("template")
        templates = _pending_templates()
        for template in templates:
            template["analysis"] = triage.get(template["template_id"])
        templates.sort(key=lambda t: (-int(t["event_count"] or 0), t["template_id"]))
        try:
            grouping_revision = grouping.load_overrides()["revision"]
        except GroupingStoreError:
            grouping_revision = None
        return jsonify({
            "llm": _llm_engine_state(),
            "windows": windows,
            "window_counts": counts,
            "services": [{"service": name, "analysis": services.get(name)} for name in _known_services()],
            "templates": templates,
            # Current incident text for the recall panel: edits show on old analyses too.
            "incidents": {
                c["id"]: {key: c.get(key) for key in (
                    "service", "title", "root_cause", "resolution", "documentation_id",
                    "occurred_at", "updated_at")}
                for c in load_cases(incident_cases_file) if c.get("id")
            },
            "grouping_revision": grouping_revision,
        })

    def _llm_unavailable():
        llm_state = _llm_engine_state()
        if not llm_state["alive"]:
            return jsonify({"error": "engine_unavailable", "message": llm_state["reason"]}), 503
        if not llm_state["llm_enabled"]:
            return jsonify({"error": "llm_disabled", "message": llm_state["reason"]}), 409
        return None

    @app.route("/api/insights/analyze", methods=["POST"])
    def request_insight():
        payload = _json_body() or {}
        kind, target_id = payload.get("kind"), payload.get("id")
        if kind not in {"window", "service", "template", "templates_all"}:
            return jsonify({"error": "invalid_request", "message": "unknown kind"}), 400
        language = payload.get("language", DEFAULT_LANGUAGE)
        if language not in LANGUAGES:
            return jsonify({"error": "invalid_request", "message": "unknown language"}), 400
        blocked = _llm_unavailable()
        if blocked is not None:
            return blocked
        now = time.time()
        if kind == "templates_all":
            triage = _analyses("template")
            queued = 0
            for template in _pending_templates():
                if queued >= TRIAGE_ALL_LIMIT:
                    break
                current = triage.get(template["template_id"])
                if isinstance(current, dict) and current.get("status") in {"requested", "pending"}:
                    continue
                add_request(analysis_requests_file, "template", template["template_id"], now,
                            language=language)
                queued += 1
            return jsonify({"state": "requested", "queued": queued}), 202
        if not isinstance(target_id, str) or not _target_exists(kind, target_id):
            return jsonify({"error": "not_found", "message": str(target_id)}), 404
        current = _analyses(kind).get(target_id)
        if isinstance(current, dict) and current.get("status") in {"requested", "pending"}:
            return jsonify({
                "error": "analysis_pending", "message": "An analysis for this item is already running",
            }), 409
        add_request(analysis_requests_file, kind, target_id, now, language=language)
        return jsonify({"state": "requested", "requested_at": now}), 202

    @app.route("/api/insights/delete", methods=["POST"])
    def delete_insight():
        payload = _json_body() or {}
        kind, target_id = payload.get("kind"), payload.get("id")
        if kind not in _record_files or not isinstance(target_id, str):
            return jsonify({"error": "invalid_request", "message": "kind and id are required"}), 400
        if _analyses(kind).get(target_id) is None:
            return jsonify({"error": "not_found", "message": target_id}), 404
        add_request(analysis_requests_file, kind, target_id, time.time(), action="delete")
        return jsonify({"state": "deleting", "key": request_key(kind, target_id)}), 202

    # ── Incident history (human-confirmed analyses, fed back to the LLM) ──

    @app.route("/api/incident-cases", methods=["GET"])
    def list_incident_cases():
        return jsonify({"cases": load_cases(incident_cases_file)})

    def _curated_pattern(
        service: str, base: List[Any], keep_texts: Any, add_template_ids: Any,
    ) -> List[Dict[str, Any]]:
        """The incident's error pattern after a person's edits. `base` is what
        the engine recorded (analysis signature or the incident's pattern);
        the client only says which of those texts to keep and which template
        ids of the same service to add. Raises ValueError for bad input."""
        entries = [e if isinstance(e, dict) else {"text": e} for e in base
                   if isinstance(e, (dict, str))]
        if keep_texts is not None:
            if not isinstance(keep_texts, list) or not all(isinstance(t, str) for t in keep_texts):
                raise ValueError("keep_texts must be a list of template texts")
            keep = set(keep_texts)
            entries = [e for e in entries if e.get("text") in keep]
        if add_template_ids:
            if not isinstance(add_template_ids, list) or \
                    not all(isinstance(t, str) for t in add_template_ids):
                raise ValueError("add_template_ids must be a list of template ids")
            registry = _load_json("template_registry.json")
            present = {e.get("text") for e in entries}
            for template_id in add_template_ids:
                template = registry.get(template_id)
                if not isinstance(template, dict) or template.get("service") != service:
                    raise ValueError(f"Template {template_id} is not a template of {service}")
                if template.get("template_text") in present:
                    continue
                present.add(template.get("template_text"))
                entries.append({
                    "text": template.get("template_text"), "template_id": template_id,
                    "level": template.get("level"), "group_id": template.get("group_id"),
                    "reasons": ["manual"],
                })
        return entries

    def _incident_text(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {key: payload.get(key) for key in ("title", "root_cause", "resolution", "documentation_id")}

    @app.route("/api/incident-cases", methods=["POST"])
    def create_incident_case():
        """Save an incident of a whole service, either from its finished
        service analysis (the engine's recorded error pattern, optionally
        trimmed or extended) or written by hand from chosen templates."""
        payload = _json_body() or {}
        service = payload.get("service")
        if not isinstance(service, str) or not service:
            return jsonify({"error": "invalid_request", "message": "service is required"}), 400
        if payload.get("manual") is True:
            base: List[Any] = []
            fields: Dict[str, Any] = {"occurred_at": None, "group_ids": [], "totals": None,
                                      "language": None, "source": {"kind": "manual"}}
        else:
            record = _read_records(service_analysis_file).get(service)
            if not isinstance(record, dict) or record.get("status") != "done":
                return jsonify({"error": "not_found",
                                "message": "Analyze this service first; no finished analysis"}), 404
            signature = record.get("signature") or {}
            # Analyses made before numbers were recorded only have plain texts.
            base = signature.get("templates") or signature.get("template_texts") or []
            fields = {
                "occurred_at": signature.get("occurred_at"), "totals": signature.get("totals"),
                "group_ids": list(dict.fromkeys(
                    g for issue in record.get("issues") or [] for g in issue.get("group_ids") or []
                )),
                "language": record.get("language"),
                "source": {"kind": "service", "analyzed_at": record.get("analyzed_at")},
            }
        try:
            pattern = _curated_pattern(service, base, payload.get("keep_texts"),
                                       payload.get("add_template_ids"))
            if not pattern:
                raise ValueError("The error pattern is empty; keep or add at least one template "
                                 "(re-analyze while the service is having the problem)")
            case = add_case(incident_cases_file, {
                "service": service, "pattern": pattern, **fields, **_incident_text(payload),
            })
        except ValueError as exc:
            return jsonify({"error": "invalid_request", "message": str(exc)}), 400
        return jsonify(case), 201

    @app.route("/api/incident-cases/<case_id>", methods=["PUT"])
    def edit_incident_case(case_id: str):
        """Rewrite an incident from experience: its text and which templates
        define it (keep_texts of its pattern, add_template_ids)."""
        payload = _json_body() or {}
        case = next((c for c in load_cases(incident_cases_file) if c.get("id") == case_id), None)
        if case is None:
            return jsonify({"error": "not_found", "message": case_id}), 404
        try:
            pattern = _curated_pattern(
                case["service"], case.get("pattern") or case.get("template_texts") or [],
                payload.get("keep_texts"), payload.get("add_template_ids"),
            )
            if not pattern:
                raise ValueError("The error pattern is empty; keep or add at least one template")
            updated = update_case(incident_cases_file, case_id,
                                  {"pattern": pattern, **_incident_text(payload)})
        except KeyError:
            return jsonify({"error": "not_found", "message": case_id}), 404
        except ValueError as exc:
            return jsonify({"error": "invalid_request", "message": str(exc)}), 400
        return jsonify(updated)

    @app.route("/api/incident-cases/<case_id>", methods=["DELETE"])
    def delete_incident_case(case_id: str):
        if not delete_case(incident_cases_file, case_id):
            return jsonify({"error": "not_found", "message": case_id}), 404
        return jsonify({"deleted": case_id})

    # ── LLM profiles (keys go in, only hints come out) ──

    @app.errorhandler(ProfileStoreUnreadable)
    def _profiles_unreadable(exc: ProfileStoreUnreadable):
        logger.error("%s", exc)
        return jsonify({"error": "profiles_unreadable", "message": str(exc)}), 500

    def _profile_error(exc: Exception):
        if isinstance(exc, ProfileStoreUnreadable):
            return _profiles_unreadable(exc)
        if isinstance(exc, KeyError):
            return jsonify({"error": "not_found", "message": str(exc.args[0])}), 404
        return jsonify({"error": "invalid_request", "message": str(exc)}), 400

    @app.route("/api/llm-profiles", methods=["GET"])
    def list_llm_profiles():
        return jsonify({**llm_profiles.public_view(), "engine": _llm_engine_state()})

    @app.route("/api/llm-profiles", methods=["POST"])
    def create_llm_profile():
        payload = _json_body()
        if payload is None:
            return jsonify({"error": "invalid_request", "message": "JSON body is required"}), 400
        try:
            profile = llm_profiles.create(
                name=payload.get("name"), endpoint=payload.get("endpoint"),
                api_key=payload.get("api_key"), model=payload.get("model"),
                activate=payload.get("activate") is True,
            )
        except ProfileError as exc:
            return _profile_error(exc)
        return jsonify({"profile": profile}), 201

    @app.route("/api/llm-profiles/active", methods=["PUT"])
    def set_active_llm_profile():
        payload = _json_body()
        if payload is None or not (payload.get("profile_id") is None or isinstance(payload.get("profile_id"), str)):
            return jsonify({"error": "invalid_request", "message": "profile_id must be a string or null"}), 400
        try:
            llm_profiles.set_active(payload.get("profile_id"))
        except KeyError as exc:
            return _profile_error(exc)
        return jsonify({"active_profile_id": payload.get("profile_id")})

    @app.route("/api/llm-profiles/<profile_id>", methods=["PUT"])
    def update_llm_profile(profile_id: str):
        payload = _json_body()
        if payload is None:
            return jsonify({"error": "invalid_request", "message": "JSON body is required"}), 400
        fields = {k: payload[k] for k in ("name", "endpoint", "model", "api_key") if k in payload}
        try:
            profile = llm_profiles.update(profile_id, **fields)
        except (KeyError, ProfileError) as exc:
            return _profile_error(exc)
        return jsonify({"profile": profile})

    @app.route("/api/llm-profiles/<profile_id>", methods=["DELETE"])
    def delete_llm_profile(profile_id: str):
        try:
            llm_profiles.delete(profile_id)
        except (KeyError, ProfileStoreUnreadable) as exc:
            return _profile_error(exc)
        except ProfileError as exc:
            return jsonify({"error": "profile_active", "message": str(exc)}), 409
        return jsonify({"deleted_id": profile_id})

    @app.route("/api/documentation", methods=["GET"])
    def list_documentation():
        corpus = documentation.load_corpus()
        overrides = documentation.load_overrides()["overrides"]
        active_group_ids = set(_load_json("group_registry.json"))
        manual_usage: Dict[str, int] = {}
        for group_id, override in overrides.items():
            if group_id in active_group_ids and isinstance(override, dict) and override.get("documentation_id"):
                doc_id = str(override["documentation_id"])
                manual_usage[doc_id] = manual_usage.get(doc_id, 0) + 1
        group_usage: Dict[str, int] = {}
        for group in _load_json("group_registry.json").values():
            doc_id = group.get("documentation_id")
            if doc_id:
                key = str(doc_id)
                group_usage[key] = group_usage.get(key, 0) + 1
        items = [
            {
                **entry,
                "group_count": group_usage.get(entry["id"], 0),
                "manual_group_count": manual_usage.get(entry["id"], 0),
            }
            for entry in corpus["entries"]
        ]
        return jsonify({
            "items": items,
            "total": len(items),
            "revision": corpus["revision"],
            "synchronization": documentation.synchronization_status(),
        })

    def _assign_document(doc_id: str, group_ids: List[Any]) -> tuple[List[str], List[Dict[str, str]]]:
        """Make doc_id the manual documentation of each existing group."""
        assigned: List[str] = []
        skipped: List[Dict[str, str]] = []
        blocked, _ = _documentation_mutations_blocked()
        groups = _load_json("group_registry.json")
        templates = _load_json("template_registry.json")
        for group_id in dict.fromkeys(str(g) for g in group_ids):
            if blocked:
                skipped.append({"group_id": group_id, "reason": "grouping_pending"})
                continue
            group = groups.get(group_id)
            if not isinstance(group, dict):
                skipped.append({"group_id": group_id, "reason": "not_found"})
                continue
            texts = [templates.get(t, {}).get("template_text", "") for t in group.get("template_ids", [])]
            try:
                documentation.set_override(
                    group_id, doc_id, group_fingerprint(group.get("template_ids", []), texts),
                    documentation.load_overrides()["revision"],
                )
                assigned.append(group_id)
            except Exception as exc:  # noqa: BLE001 - the document itself was saved
                logger.warning("Unable to assign %s to %s: %s", doc_id, group_id, exc)
                skipped.append({"group_id": group_id, "reason": "assignment_failed"})
        return assigned, skipped

    @app.route("/api/documentation", methods=["POST"])
    def create_documentation():
        payload = request.get_json(silent=True) or {}
        group_ids = payload.pop("assign_group_ids", None) if isinstance(payload, dict) else None
        try:
            item, corpus = documentation.create_document(payload, payload.get("revision"))
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)
        body: Dict[str, Any] = {"item": item, "revision": corpus["revision"]}
        if isinstance(group_ids, list) and group_ids:
            body["assigned_group_ids"], body["assignment_skipped"] = _assign_document(item["id"], group_ids)
        return jsonify(body), 201

    @app.route("/api/documentation/<doc_id>", methods=["PUT"])
    def update_documentation(doc_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item, corpus = documentation.update_document(doc_id, payload, payload.get("revision"))
            return jsonify({"item": item, "revision": corpus["revision"]})
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)

    @app.route("/api/documentation/<doc_id>", methods=["DELETE"])
    def delete_documentation(doc_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            groups = _load_json("group_registry.json")
            active_group_ids = {
                group_id
                for group_id, group in groups.items()
                if isinstance(group, dict) and group.get("active", True)
            }
            assigned_group_ids = {
                group_id
                for group_id, group in groups.items()
                if isinstance(group, dict)
                and group_id in active_group_ids
                and group.get("documentation_id") == doc_id
            }
            override_snapshot = documentation.load_overrides()
            manual_group_ids = {
                group_id
                for group_id, value in override_snapshot.get("overrides", {}).items()
                if isinstance(value, dict)
                and value.get("documentation_id") == doc_id
                and group_id in active_group_ids
            }
            affected_group_ids = sorted(assigned_group_ids | manual_group_ids)
            force = payload.get("force") is True
            corpus = documentation.delete_document(
                doc_id,
                payload.get("revision"),
                active_group_ids=active_group_ids,
                assigned_group_ids=assigned_group_ids,
                force=force,
            )
            override_revision = documentation.load_overrides()["revision"]
            return jsonify({
                "deleted_id": doc_id,
                "revision": corpus["revision"],
                "override_revision": override_revision,
                "affected_group_ids": affected_group_ids,
                "state": "pending",
            })
        except DocumentInUse as exc:
            return jsonify({
                "error": "document_in_use",
                "message": str(exc),
                "group_ids": exc.group_ids,
                "groups": _document_group_details(groups, exc.group_ids),
                "requires_confirmation": True,
            }), 409
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)

    @app.route("/api/groups/<group_id>/documentation", methods=["PUT"])
    def assign_group_documentation(group_id: str):
        payload = request.get_json(silent=True) or {}
        blocked, grouping_sync = _documentation_mutations_blocked()
        if blocked:
            return _grouping_pending_response(grouping_sync)
        groups = _load_json("group_registry.json")
        group = groups.get(group_id)
        if group is None:
            return jsonify({"error": "not_found", "message": group_id}), 404
        templates = _load_json("template_registry.json")
        texts = [
            templates.get(template_id, {}).get("template_text", "")
            for template_id in group.get("template_ids", [])
        ]
        fingerprint = group_fingerprint(group.get("template_ids", []), texts)
        try:
            snapshot = documentation.set_override(
                group_id,
                str(payload.get("documentation_id") or ""),
                fingerprint,
                payload.get("override_revision"),
            )
            return jsonify({
                "group_id": group_id,
                "documentation_id": payload.get("documentation_id"),
                "override_revision": snapshot["revision"],
                "state": "pending",
            })
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)

    @app.route("/api/groups/<group_id>/documentation", methods=["DELETE"])
    def clear_group_documentation(group_id: str):
        payload = request.get_json(silent=True) or {}
        blocked, grouping_sync = _documentation_mutations_blocked()
        if blocked:
            return _grouping_pending_response(grouping_sync)
        groups = _load_json("group_registry.json")
        if group_id not in groups:
            return jsonify({"error": "not_found", "message": group_id}), 404
        if payload.get("force") is not True:
            group = groups[group_id]
            override = documentation.load_overrides().get("overrides", {}).get(group_id)
            documentation_id = group.get("documentation_id")
            if not documentation_id and isinstance(override, dict):
                documentation_id = override.get("documentation_id")
            documentation_source = group.get("documentation_source", "none")
            if isinstance(override, dict) and override.get("documentation_id"):
                documentation_source = "manual"
            return jsonify({
                "error": "confirmation_required",
                "message": "Confirm clearing documentation for this group",
                "group_id": group_id,
                "documentation_id": documentation_id,
                "documentation_source": documentation_source,
                "requires_confirmation": True,
            }), 409
        try:
            snapshot = documentation.clear_override(
                group_id, payload.get("override_revision")
            )
            return jsonify({
                "group_id": group_id,
                "override_revision": snapshot["revision"],
                "state": "pending",
                "clear_suppressed": True,
            })
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)

    # ── Serve the frontend ────────────────────────────────────────────

    # ── Data sources: which Elasticsearch indices the engine reads ──

    def _index_view() -> Dict[str, Any]:
        """The selection (or, before the first save, the engine's configured
        index) merged with what the engine reports for it."""
        selection = index_selection.load()
        status = read_json(index_status_file)
        alive = _llm_engine_state()["alive"]
        if selection is None:
            entries, revision, source = status.get("entries") or [], None, "configured"
            applied = status.get("mode") == "configured"
        else:
            entries, revision, source = selection["entries"], selection["revision"], "selected"
            applied = status.get("selection_revision") == revision
        if not alive:
            state = "engine_unavailable"
        elif not applied:
            state = "pending"
        elif source == "selected" and not entries:
            state = "no_index_selected"
        else:
            state = "applied"
        return {
            "state": state, "source": source, "revision": revision, "entries": entries,
            "resolved": status.get("resolved"), "active_indices": status.get("active_indices"),
            "indices": status.get("indices") or {}, "available": status.get("available"),
            "error": status.get("error"), "status_updated_at": status.get("updated_at"),
        }

    @app.route("/api/es-indices", methods=["GET"])
    def get_es_indices():
        return jsonify(_index_view())

    @app.route("/api/es-indices", methods=["PUT"])
    def set_es_indices():
        payload = _json_body()
        if payload is None or "patterns" not in payload:
            return jsonify({"error": "invalid_request", "message": "patterns is required"}), 400
        # Before the first save, keep the engine's start time for the
        # configured patterns so saving them unchanged never skips logs.
        known = {
            entry.get("pattern"): entry.get("added_at")
            for entry in read_json(index_status_file).get("entries") or []
            if isinstance(entry, dict) and isinstance(entry.get("added_at"), (int, float))
        } if index_selection.load() is None else {}
        try:
            saved = index_selection.replace(
                payload["patterns"], payload.get("revision"), known_added_at=known
            )
        except IndexSelectionConflict as exc:
            return jsonify({
                "error": "revision_conflict", "message": str(exc),
                "current_revision": exc.current_revision,
            }), 409
        except IndexSelectionError as exc:
            return jsonify({"error": "invalid_request", "message": str(exc)}), 400
        return jsonify({"state": "pending", **saved}), 202

    @app.route("/")
    def index():
        web_dir = Path(__file__).parent / "static"
        return send_from_directory(str(web_dir), "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename: str):
        web_dir = Path(__file__).parent / "static"
        return send_from_directory(str(web_dir), filename)

    return app

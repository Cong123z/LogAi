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
from logai.incident.profiles import LLMProfileStore, ProfileError
from logai.incident.requests import add_request, load_requests
from logai.models import DEFAULT_LEVEL, LEVEL_RANK
from logai.storage.documentation import (
    DocumentInUse,
    DocumentationCorpusStore,
    DocumentationStoreError,
    RevisionConflict,
    group_fingerprint,
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
    llm_profiles = LLMProfileStore(llm_profiles_path or base / "llm_profiles.json")
    analysis_requests_file = Path(analysis_requests_path or base / "analysis_requests.json")

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

    @app.route("/api/alerts")
    def list_alerts():
        """Return the latest persisted alert condition for every current group."""
        groups = _load_json("group_registry.json")
        templates = _load_json("template_registry.json")
        persisted = _load_json("anomaly_state.json")
        # Engine-owned LLM incident analysis, keyed like anomaly_state.json.
        analyses = _load_json("incident_analysis.json")

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
        return jsonify({"items": items, "total": len(items), "counts": counts})

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

    def _service_analyses() -> Dict[str, Any]:
        """Engine records, with requests not yet picked up shown as 'requested'."""
        try:
            with open(service_analysis_file, "r", encoding="utf-8") as stream:
                records = json.load(stream)
        except (OSError, json.JSONDecodeError):
            records = {}
        records = records if isinstance(records, dict) else {}
        for service, requested_at in load_requests(analysis_requests_file).items():
            record = records.get(service)
            if not isinstance(record, dict) or requested_at > float(record.get("requested_at") or 0.0):
                if time.time() - requested_at > STALE_REQUEST_SECONDS:
                    # Never picked up (engine down or LLM disabled): stop
                    # blocking the button so the user can retry.
                    records[service] = {
                        "status": "failed", "service": service, "requested_at": requested_at,
                        "error": "The engine did not pick up this request; try again",
                    }
                else:
                    records[service] = {
                        "status": "requested", "service": service, "requested_at": requested_at,
                    }
        return records

    @app.route("/api/service-analysis", methods=["GET"])
    def list_service_analysis():
        return jsonify({"services": _known_services(), "analyses": _service_analyses()})

    @app.route("/api/service-analysis", methods=["POST"])
    def request_service_analysis():
        payload = request.get_json(silent=True)
        service = payload.get("service") if isinstance(payload, dict) else None
        if not isinstance(service, str) or not service.strip():
            return jsonify({"error": "invalid_request", "message": "service is required"}), 400
        if service not in _known_services():
            return jsonify({"error": "not_found", "message": service}), 404
        status = grouping.synchronization_status(stale_seconds=grouping_stale_seconds)
        if status.get("state") == "engine_unavailable":
            return jsonify({
                "error": "engine_unavailable", "message": "The analysis engine is not running",
            }), 503
        if (status.get("runtime") or {}).get("llm_enabled") is not True:
            return jsonify({
                "error": "llm_disabled", "message": "LLM analysis is disabled on the engine",
            }), 409
        current = _service_analyses().get(service)
        if isinstance(current, dict) and current.get("status") in {"requested", "pending"}:
            return jsonify({
                "error": "analysis_pending", "message": "An analysis for this service is already running",
            }), 409
        requested_at = time.time()
        try:
            add_request(analysis_requests_file, service, requested_at)
        except OSError as exc:
            logger.exception("Unable to write analysis request")
            return jsonify({"error": "storage_error", "message": str(exc)}), 500
        return jsonify({"state": "requested", "requested_at": requested_at}), 202

    # ── LLM profiles (keys go in, only hints come out) ──

    def _profile_error(exc: Exception):
        if isinstance(exc, KeyError):
            return jsonify({"error": "not_found", "message": str(exc.args[0])}), 404
        return jsonify({"error": "invalid_request", "message": str(exc)}), 400

    def _json_body() -> Optional[Dict[str, Any]]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None

    @app.route("/api/llm-profiles", methods=["GET"])
    def list_llm_profiles():
        status = grouping.synchronization_status(stale_seconds=grouping_stale_seconds)
        runtime = status.get("runtime") or {}
        return jsonify({
            **llm_profiles.public_view(),
            "engine": {
                "alive": status.get("state") != "engine_unavailable",
                "llm_enabled": runtime.get("llm_enabled") is True,
                "llm_profile_id": runtime.get("llm_profile_id"),
            },
        })

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
        except KeyError as exc:
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

    @app.route("/api/documentation", methods=["POST"])
    def create_documentation():
        payload = request.get_json(silent=True) or {}
        try:
            item, corpus = documentation.create_document(payload, payload.get("revision"))
            return jsonify({"item": item, "revision": corpus["revision"]}), 201
        except Exception as exc:  # noqa: BLE001
            return _mutation_error(exc)

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

    @app.route("/")
    def index():
        web_dir = Path(__file__).parent / "static"
        return send_from_directory(str(web_dir), "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename: str):
        web_dir = Path(__file__).parent / "static"
        return send_from_directory(str(web_dir), filename)

    return app

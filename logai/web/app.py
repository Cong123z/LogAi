"""Template Explorer Web API.

Reads template_registry.json and group_registry.json (read-only)
to serve a browsable UI for discovered log templates.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory

logger = logging.getLogger("logai.web")

PENDING_GROUP_ID = "UNASSIGNED_PENDING"


def create_app(data_dir: str = "data") -> Flask:
    app = Flask(__name__, static_folder=None)
    base = Path(data_dir)

    def _load_json(filename: str) -> Dict[str, Any]:
        path = base / filename
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _is_known(group_id: Optional[str]) -> bool:
        return group_id is not None and group_id != PENDING_GROUP_ID

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

        return jsonify({
            **tmpl,
            "status": "known" if _is_known(gid) else "unknown",
            "group_info": group,
        })

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
        service = request.args.get("service", "").strip().lower()
        documented = request.args.get("documented", "").strip().lower()
        search = request.args.get("search", "").strip().lower()
        items = []
        for gid, group in groups.items():
            item = {"group_id": gid, **group}
            if service and service not in str(item.get("service", "")).lower():
                continue
            if documented in ("true", "false") and bool(item.get("documented", False)) != (documented == "true"):
                continue
            haystack = f"{gid} {item.get('representative_template', '')}".lower()
            if search and search not in haystack:
                continue
            items.append(item)
        items.sort(key=lambda item: (item.get("event_count", 0), item["group_id"]), reverse=True)
        return jsonify({"items": items, "total": len(items)})

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

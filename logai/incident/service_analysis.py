"""Evidence and reply validation for on-demand whole-service LLM analysis.

Pure functions: the realtime pipeline builds the evidence on the poll thread
from in-memory state, and the classifier worker validates the LLM reply.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.features.template_activity import is_active, recent
from logai.grouping import PENDING_GROUP_ID
from logai.incident.cases import MAX_TEMPLATE_TEXTS, similar_cases
from logai.incident.classifier import (
    MAX_PARAMETER_VALUE,
    MAX_UNKNOWN_TEMPLATES,
    NEW_TEMPLATE_SECONDS,
    TITLE_LIMIT,
    clean_suggestion,
    summarize_parameters,
)
from logai.models import DEFAULT_LEVEL, LEVEL_RANK
from logai.storage.registries import GroupRegistry, TemplateRegistry

MAX_GROUPS = 20
MAX_TEMPLATES_PER_GROUP = 3
CANDIDATES_PER_GROUP = 3
MAX_CANDIDATES = 15
MAX_CANDIDATE_TEXT = 1000
MAX_ISSUES = 10
MAX_PAST_INCIDENTS = 5
SUMMARY_LIMIT = REASONING_LIMIT = 4000
HEALTH_VALUES = {"healthy", "degraded", "critical"}
_ALERT_RANK = {"ALERTING": 3, "COOLING": 2, "WARMING": 1}

SERVICE_SYSTEM_PROMPT = (
    "You are an SRE reviewing the overall health of one service from its log "
    "groups. Assess its health, summarize it, and list the most important "
    "issues (at most 10), most severe first. Counts are recent only "
    "(count_15m / count_30m: events in the last 15 / 30 minutes); rate_per_min "
    "is the current rate, baseline_per_min a template's normal rate over the "
    "last 24 h and ratio how many times normal it runs now (null while unknown). "
    "unknown_templates are recent templates of this service that belong to no "
    "group yet. past_incidents are earlier incidents of this service, written "
    "and curated by people from experience, whose templates overlap the current "
    "ones; each has a then-vs-now comparison per template (now null: not "
    "abnormal now) and only_now templates that are new this time; judge "
    "severity mainly by ratio. When one fits, give its id as the top-level "
    "similar_case_id and prefer its root cause and resolution over new "
    "suggestions in the matching issues. For each issue name the related "
    "group_ids, choose the single candidate document that explains and "
    "resolves it or none, and if none fits propose a concise remediation. "
    'Reply with JSON only: {"health": "healthy" | "degraded" | "critical", '
    '"summary": "...", "issues": [{"title": "...", "group_ids": ["..."], '
    '"documentation_id": "<candidate id>" | null, "reasoning": "...", '
    '"suggestion": {"title": "...", "text": "...", "error_code": "..."} | null}], '
    '"similar_case_id": "<past incident id>" | null}'
)


def _level_rank(level: str) -> int:
    return LEVEL_RANK.get(str(level or DEFAULT_LEVEL).upper(), LEVEL_RANK[DEFAULT_LEVEL])


ELEVATED_RATIO = 3.0


def service_signature(
    service: str,
    templates: TemplateRegistry,
    alert_states: Dict[str, Tuple[str, float]],
    activity: Dict[str, Dict[str, Any]],
    now: float,
) -> Dict[str, Any]:
    """The service's current error pattern: {"templates": [...], "totals": {...}}.

    A template is in the pattern when it had events in the last 30 minutes AND
    is abnormal: running at least ELEVATED_RATIO times its own normal rate, in
    a non-NORMAL group, not in any group yet, or first seen within the last
    hour. While a template's normal rate is still unknown (fresh engine), WARN
    or worse stands in for "elevated". Chronic warnings at their usual rate are
    left out, so they never make unrelated incidents look alike. Each entry
    keeps its numbers, so an incident records how bad it was, not only what."""
    rows = []
    events_15m = error_events_15m = 0
    for state in templates.all_templates():
        if state.service != service:
            continue
        numbers = recent(activity, state.template_id)
        events_15m += numbers["count_15m"]
        if _level_rank(state.level) >= LEVEL_RANK["ERROR"]:
            error_events_15m += numbers["count_15m"]
        if not numbers["count_30m"]:
            continue
        group_state = alert_states.get(state.group_id or "", ("NORMAL", 0.0))[0]
        ratio = numbers["ratio"]
        elevated = (
            ratio >= ELEVATED_RATIO if ratio is not None
            else _level_rank(state.level) >= LEVEL_RANK["WARN"]
        )
        reasons = [
            reason for reason, applies in (
                ("elevated", elevated),
                ("alerting", group_state != "NORMAL"),
                ("unknown", not state.group_id or state.group_id == PENDING_GROUP_ID),
                ("new", now - state.first_seen < NEW_TEMPLATE_SECONDS),
            ) if applies
        ]
        if not reasons:
            continue
        rows.append({
            "text": state.template_text, "template_id": state.template_id,
            "level": state.level, "group_id": state.group_id, "group_state": group_state,
            **numbers,
            "age_minutes": round(max(0.0, now - state.first_seen) / 60.0),
            "reasons": reasons,
        })
    rows.sort(key=lambda row: (-row["count_30m"], row["template_id"]))
    return {
        "templates": rows[:MAX_TEMPLATE_TEXTS],
        "totals": {"events_15m": events_15m, "error_events_15m": error_events_15m},
    }


def build_service_evidence(
    service: str,
    groups: GroupRegistry,
    templates: TemplateRegistry,
    alert_states: Dict[str, Tuple[str, float]],
    recent_params: Dict[str, List[Tuple[str, List[str]]]],
    matcher: DocumentationMatcher,
    cases: Iterable[Dict[str, Any]] = (),
    activity: Optional[Dict[str, Dict[str, Any]]] = None,
    signature: Iterable[Dict[str, Any]] = (),
) -> Tuple[Dict[str, Any], Dict[str, str], Set[str]]:
    """Evidence for every group of `service`, plus candidate id -> title and
    the group ids sent (the only ones an issue may reference). `activity` holds
    this service's recent per-template counts; `signature` (the "templates"
    of service_signature) selects the past incidents."""
    activity = activity or {}

    def count(state, window: str = "count_30m") -> int:
        return recent(activity, state.template_id)[window]

    def describe(state) -> Dict[str, Any]:
        return {"id": state.template_id, "text": state.template_text, "level": state.level,
                **recent(activity, state.template_id)}

    rows = []
    for group in groups.all_groups():
        members = [
            state for template_id in group.template_ids
            if (state := templates.get(template_id)) is not None and state.service == service
        ]
        if not members and group.group_id not in alert_states:
            continue
        members.sort(key=lambda s: (-count(s), s.template_id))
        state, score = alert_states.get(group.group_id, ("NORMAL", 0.0))
        level = max((s.level for s in members), key=_level_rank, default=DEFAULT_LEVEL)
        recent_30m = sum(count(s) for s in members)
        rank = (_ALERT_RANK.get(state, 0), recent_30m > 0, _level_rank(level), recent_30m)
        rows.append((rank, group, members, state, score, level))
    rows.sort(key=lambda row: (row[0], row[1].group_id), reverse=True)
    rows = rows[:MAX_GROUPS]

    evidence_groups = []
    best: Dict[str, Tuple[float, Any]] = {}
    for _, group, members, state, score, level in rows:
        shown = members[:MAX_TEMPLATES_PER_GROUP]
        listed = {s.template_id for s in shown}
        evidence_groups.append({
            "group_id": group.group_id,
            "representative_template": group.representative_template,
            "count_15m": sum(count(s, "count_15m") for s in members),
            "count_30m": sum(count(s) for s in members),
            "level": level,
            "alert": {"state": state, "score": score},
            "templates": [describe(s) for s in shown],
            "parameters": [
                {
                    **item,
                    "top_values": [
                        [str(value)[:MAX_PARAMETER_VALUE], value_count]
                        for value, value_count in item["top_values"]
                    ],
                }
                for item in summarize_parameters(recent_params.get(group.group_id, ()))
                if item["template_id"] in listed
            ],
        })
        for entry, similarity in matcher.top_k(
            groups.get_centroid(group.group_id), CANDIDATES_PER_GROUP
        ):
            if entry.doc_id not in best or similarity > best[entry.doc_id][0]:
                best[entry.doc_id] = (similarity, entry)

    unknown = sorted(
        (
            s for s in templates.all_templates()
            if s.service == service and is_active(activity, s.template_id)
            and (not s.group_id or s.group_id == PENDING_GROUP_ID)
        ),
        key=lambda s: (-_level_rank(s.level), -count(s), s.template_id),
    )[:MAX_UNKNOWN_TEMPLATES]
    ranked = sorted(best.values(), key=lambda item: -item[0])[:MAX_CANDIDATES]
    evidence = {
        "service": service,
        "groups": evidence_groups,
        "unknown_templates": [describe(s) for s in unknown],
        "candidates": [
            {
                "id": entry.doc_id, "title": entry.title,
                "text": entry.text[:MAX_CANDIDATE_TEXT],
                "error_code": entry.error_code, "similarity": round(similarity, 4),
            }
            for similarity, entry in ranked
        ],
        "past_incidents": similar_cases(cases, service, signature, k=MAX_PAST_INCIDENTS),
    }
    return (
        evidence,
        {entry.doc_id: entry.title for _, entry in ranked},
        {group["group_id"] for group in evidence_groups},
    )


def validate_service_reply(
    reply: Dict[str, Any], candidates: Dict[str, str], sent_groups: Set[str],
) -> Dict[str, Any]:
    """Keep only what was offered: known group ids, candidate documents, and
    issues that carry a document or a valid suggestion."""
    summary = reply.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("LLM reply has no summary")
    health = reply.get("health")
    issues: List[Dict[str, Any]] = []
    raw_issues = reply.get("issues")
    for raw in raw_issues if isinstance(raw_issues, list) else []:
        if not isinstance(raw, dict):
            continue
        doc_id = raw.get("documentation_id")
        if not isinstance(doc_id, str) or doc_id not in candidates:
            doc_id = None
        suggestion = None if doc_id else clean_suggestion(raw.get("suggestion"))
        if doc_id is None and suggestion is None:
            continue
        group_ids = raw.get("group_ids")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            title = suggestion["title"] if suggestion else candidates[doc_id]
        issues.append({
            "title": title.strip()[:TITLE_LIMIT],
            "group_ids": [
                g for g in (group_ids if isinstance(group_ids, list) else [])
                if isinstance(g, str) and g in sent_groups
            ],
            "documentation_id": doc_id,
            "document_title": candidates.get(doc_id, "") if doc_id else "",
            "reasoning": str(raw.get("reasoning") or "")[:REASONING_LIMIT],
            "suggestion": suggestion,
        })
        if len(issues) == MAX_ISSUES:
            break
    return {
        "health": health if health in HEALTH_VALUES else "unknown",
        "summary": summary.strip()[:SUMMARY_LIMIT],
        "issues": issues,
    }

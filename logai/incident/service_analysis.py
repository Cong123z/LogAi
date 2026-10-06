"""Evidence and reply validation for on-demand whole-service LLM analysis.

Pure functions: the realtime pipeline builds the evidence on the poll thread
from in-memory state, and the classifier worker validates the LLM reply.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.incident.classifier import (
    MAX_PARAMETER_VALUE,
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
SUMMARY_LIMIT = REASONING_LIMIT = 4000
HEALTH_VALUES = {"healthy", "degraded", "critical"}
_ALERT_RANK = {"ALERTING": 3, "COOLING": 2, "WARMING": 1}

SERVICE_SYSTEM_PROMPT = (
    "You are an SRE reviewing the overall health of one service from its log "
    "groups. Assess its health, summarize it, and list the most important "
    "issues (at most 10), most severe first. For each issue name the related "
    "group_ids, choose the single candidate document that explains and "
    "resolves it or none, and if none fits propose a concise remediation. "
    'Reply with JSON only: {"health": "healthy" | "degraded" | "critical", '
    '"summary": "...", "issues": [{"title": "...", "group_ids": ["..."], '
    '"documentation_id": "<candidate id>" | null, "reasoning": "...", '
    '"suggestion": {"title": "...", "text": "...", "error_code": "..."} | null}]}'
)


def _level_rank(level: str) -> int:
    return LEVEL_RANK.get(str(level or DEFAULT_LEVEL).upper(), LEVEL_RANK[DEFAULT_LEVEL])


def build_service_evidence(
    service: str,
    groups: GroupRegistry,
    templates: TemplateRegistry,
    alert_states: Dict[str, Tuple[str, float]],
    recent_params: Dict[str, List[Tuple[str, List[str]]]],
    matcher: DocumentationMatcher,
) -> Tuple[Dict[str, Any], Dict[str, str], Set[str]]:
    """Evidence for every group of `service`, plus candidate id -> title and
    the group ids sent (the only ones an issue may reference)."""
    rows = []
    for group in groups.all_groups():
        members = [
            state for template_id in group.template_ids
            if (state := templates.get(template_id)) is not None and state.service == service
        ]
        if not members and group.group_id not in alert_states:
            continue
        members.sort(key=lambda s: (-s.event_count, s.template_id))
        state, score = alert_states.get(group.group_id, ("NORMAL", 0.0))
        level = max((s.level for s in members), key=_level_rank, default=DEFAULT_LEVEL)
        event_count = sum(s.event_count for s in members)
        rank = (_ALERT_RANK.get(state, 0), _level_rank(level), event_count)
        rows.append((rank, group, members[:MAX_TEMPLATES_PER_GROUP], state, score, level, event_count))
    rows.sort(key=lambda row: (row[0], row[1].group_id), reverse=True)
    rows = rows[:MAX_GROUPS]

    evidence_groups = []
    best: Dict[str, Tuple[float, Any]] = {}
    for _, group, members, state, score, level, event_count in rows:
        listed = {s.template_id for s in members}
        evidence_groups.append({
            "group_id": group.group_id,
            "representative_template": group.representative_template,
            "event_count": event_count,
            "level": level,
            "alert": {"state": state, "score": score},
            "templates": [
                {"id": s.template_id, "text": s.template_text, "level": s.level, "count": s.event_count}
                for s in members
            ],
            "parameters": [
                {
                    **item,
                    "top_values": [
                        [str(value)[:MAX_PARAMETER_VALUE], count]
                        for value, count in item["top_values"]
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

    ranked = sorted(best.values(), key=lambda item: -item[0])[:MAX_CANDIDATES]
    evidence = {
        "service": service,
        "groups": evidence_groups,
        "candidates": [
            {
                "id": entry.doc_id, "title": entry.title,
                "text": entry.text[:MAX_CANDIDATE_TEXT],
                "error_code": entry.error_code, "similarity": round(similarity, 4),
            }
            for similarity, entry in ranked
        ],
    }
    return (
        evidence,
        {entry.doc_id: entry.title for _, entry in ranked},
        {group["group_id"] for group in evidence_groups},
    )


def validate_service_reply(
    reply: Dict[str, Any], candidates: Dict[str, str], sent_groups: Set[str]
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

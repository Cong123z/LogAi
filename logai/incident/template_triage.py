"""LLM triage of unknown (pending) templates: is it suspicious, and which
existing group (if any) does it belong to? Pure functions; the realtime
pipeline builds the evidence and the classifier worker validates the reply.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np

from logai.incident.classifier import TEXT_LIMIT
from logai.storage.registries import GroupRegistry, TemplateRegistry

MAX_CANDIDATE_GROUPS = 5
VERDICTS = {"suspicious", "benign", "unsure"}
NEW_GROUP = "new"

TEMPLATE_SYSTEM_PROMPT = (
    "You triage a log template that the engine could not place in any known "
    "group. Decide whether it looks suspicious (errors, failures, security or "
    "unusual behaviour) or benign (normal operational information), and which "
    "candidate group it belongs to: the group whose logs describe the same "
    "operation or failure. If none fits, answer \"new\". Reply with JSON only: "
    '{"verdict": "suspicious" | "benign" | "unsure", "confidence": 0.0-1.0, '
    '"reasoning": "...", "suggested_group_id": "<candidate group_id>" | "new" | null}'
)


def build_template_evidence(
    template_id: str, templates: TemplateRegistry, groups: GroupRegistry
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Evidence for one template plus candidate group_id -> representative
    template (the only groups the LLM may suggest)."""
    state = templates.get(template_id)
    if state is None:
        raise LookupError(f"Unknown template {template_id!r}")
    embedding = templates.get_embedding(template_id)
    scored = []
    if embedding is not None:
        vector = np.asarray(embedding, dtype=float)
        for group_id, centroid in groups.all_centroids().items():
            centroid = np.asarray(centroid, dtype=float)
            if centroid.shape != vector.shape:
                continue
            scored.append((float(np.dot(vector, centroid)), group_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    candidate_groups = []
    for similarity, group_id in scored[:MAX_CANDIDATE_GROUPS]:
        group = groups.get(group_id)
        if group is None:
            continue
        candidate_groups.append({
            "group_id": group_id,
            "representative_template": group.representative_template,
            "service": group.service,
            "event_count": group.event_count,
            "similarity": round(similarity, 4),
        })
    evidence = {
        "template": {
            "id": state.template_id, "text": state.template_text, "service": state.service,
            "level": state.level, "count": state.event_count,
            "first_seen": state.first_seen, "last_seen": state.last_seen,
        },
        "candidate_groups": candidate_groups,
    }
    return evidence, {g["group_id"]: g["representative_template"] for g in candidate_groups}


def validate_template_reply(reply: Dict[str, Any], candidates: Dict[str, str]) -> Dict[str, Any]:
    reasoning = reply.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("LLM reply has no reasoning")
    verdict = reply.get("verdict")
    suggested = reply.get("suggested_group_id")
    if suggested != NEW_GROUP and not (isinstance(suggested, str) and suggested in candidates):
        suggested = None
    try:
        confidence = min(1.0, max(0.0, float(reply.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "verdict": verdict if verdict in VERDICTS else "unsure",
        "reasoning": reasoning.strip()[:TEXT_LIMIT],
        "confidence": confidence,
        "suggested_group_id": suggested,
        "suggested_group_rep": candidates.get(suggested, "") if suggested else "",
    }

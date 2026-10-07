"""Incident history: human-confirmed past incidents, fed back to the LLM.

Unlike the runbook corpus (few reusable "how to fix" documents matched by
embedding), a case records one confirmed incident of a whole service: what the
service was suffering, why, and what fixed it. Each case keeps the service's
error pattern at the time (service_analysis.service_signature) and is matched
by overlap with the current pattern, using template texts rather than ids, so
cases survive template-id renumbering after a Drain3 reset.

The web is the only writer of this file and the engine only reads it.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from logai.incident.requests import DEFAULT_LANGUAGE, LANGUAGES

SCHEMA_VERSION = 2  # 2 adds "pattern" (per-template numbers) and "totals"
PATTERN_TEXT_LIMIT = 1000
MAX_ONLY_LISTED = 10
# Numbers kept per pattern template; compared then vs now on recall.
PATTERN_NUMBERS = ("count_15m", "count_30m", "rate_per_min", "baseline_per_min", "ratio")
PATTERN_LABELS = ("template_id", "level", "group_id", "group_state")
TITLE_LIMIT = 200
TEXT_LIMIT = 4000
MAX_TEMPLATE_TEXTS = 30

# The web writes this file from several request threads.
_write_lock = threading.Lock()


def _read(path: str | Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {"next": 1, "cases": {}}
    cases = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(cases, dict):
        return {"next": 1, "cases": {}}
    next_number = raw.get("next")
    if not isinstance(next_number, int) or isinstance(next_number, bool) or next_number < 1:
        next_number = len(cases) + 1
    return {"next": next_number, "cases": {k: v for k, v in cases.items() if isinstance(v, dict)}}


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump({"schema_version": SCHEMA_VERSION, **data}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_cases(path: str | Path) -> List[Dict[str, Any]]:
    """Every case, newest first; a missing or corrupt file reads as empty."""
    cases = list(_read(path)["cases"].values())
    return sorted(cases, key=lambda c: (-float(c.get("created_at") or 0), str(c.get("id"))))


def _text(data: Dict[str, Any], key: str, limit: int, required: bool) -> str:
    value = data.get(key) or ""
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise ValueError(f"{key} must be a string of at most {limit} characters")
    if required and not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def clean_pattern(raw: Any) -> List[Dict[str, Any]]:
    """Keep only well-formed pattern entries and known fields (numbers finite
    or null), at most MAX_TEMPLATE_TEXTS, first occurrence of each text."""
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or text in seen:
            continue
        seen.add(text)
        entries.append({
            "text": text[:PATTERN_TEXT_LIMIT],
            **{key: str(item[key]) for key in PATTERN_LABELS if item.get(key) is not None},
            **{key: _number(item.get(key)) for key in PATTERN_NUMBERS},
            "age_minutes": _number(item.get("age_minutes")),
            "reasons": [r for r in item.get("reasons") or [] if isinstance(r, str)][:4],
        })
        if len(entries) == MAX_TEMPLATE_TEXTS:
            break
    return entries


def _pattern_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """`pattern` (template entries with their numbers) or, for analyses made
    before numbers were recorded, plain `template_texts`."""
    pattern = clean_pattern(data.get("pattern"))
    texts = [entry["text"] for entry in pattern] if pattern else data.get("template_texts")
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        raise ValueError("the error pattern must contain at least one template")
    return {"template_texts": list(dict.fromkeys(texts))[:MAX_TEMPLATE_TEXTS], "pattern": pattern}


def _text_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """The parts people write from experience."""
    return {
        "title": _text(data, "title", TITLE_LIMIT, True),
        "root_cause": _text(data, "root_cause", TEXT_LIMIT, True),
        "resolution": _text(data, "resolution", TEXT_LIMIT, False),
        "documentation_id": _text(data, "documentation_id", 100, False) or None,
    }


def add_case(path: str | Path, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and store one case under the next CASE-NNNN id."""
    service = _text(data, "service", TITLE_LIMIT, True)
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    group_ids = data.get("group_ids") or []
    if not isinstance(group_ids, list) or not all(isinstance(g, str) for g in group_ids):
        raise ValueError("group_ids must be a list of strings")
    case = {
        "created_at": time.time(),
        "updated_at": None,
        "occurred_at": data.get("occurred_at"),
        "service": service,
        "group_ids": group_ids,
        **_pattern_fields(data),
        "totals": {key: _number(totals.get(key)) for key in ("events_15m", "error_events_15m")},
        **_text_fields(data),
        "language": (
            data.get("language") if data.get("language") in LANGUAGES else DEFAULT_LANGUAGE
        ),
        "source": data.get("source") if isinstance(data.get("source"), dict) else {},
    }
    path = Path(path)
    with _write_lock:
        current = _read(path)
        case["id"] = f"CASE-{current['next']:04d}"
        current["cases"][case["id"]] = case
        _write(path, {"next": current["next"] + 1, "cases": current["cases"]})
    return case


def update_case(path: str | Path, case_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a case's text and error pattern with what people wrote from
    experience; service, times, source and totals stay. KeyError when gone."""
    changes = {**_text_fields(data), **_pattern_fields(data), "updated_at": time.time()}
    path = Path(path)
    with _write_lock:
        current = _read(path)
        case = current["cases"].get(case_id)
        if case is None:
            raise KeyError(case_id)
        case.update(changes)
        _write(path, current)
    return case


def delete_case(path: str | Path, case_id: str) -> bool:
    path = Path(path)
    with _write_lock:
        current = _read(path)
        if current["cases"].pop(case_id, None) is None:
            return False
        _write(path, current)
    return True


def recall_summary(past_incidents: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What an analysis record keeps of the incidents the engine recalled: the
    match itself, not the incident's text (the page reads the current text, so
    later edits show on old analyses too)."""
    keys = ("id", "title", "overlap", "occurred_at", "comparison", "only_now")
    return [{key: item.get(key) for key in keys} for item in past_incidents or []]


def _numbers(entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    entry = entry or {}
    return {key: entry.get(key) for key in ("rate_per_min", "ratio", "count_15m")}


def similar_cases(
    cases: Iterable[Dict[str, Any]], service: str,
    current: Iterable[Union[str, Dict[str, Any]]],
    k: int = 3, min_overlap: float = 0.5,
) -> List[Dict[str, Any]]:
    """Cases of `service` whose error pattern is mostly present now.

    `current` is the service's pattern now (service_signature entries, or
    plain template texts). Overlap is the share of the case's templates found
    in it; only template texts are compared, because rates differ every time
    an incident repeats. Each match carries a then-vs-now comparison of every
    case template (numbers are null for cases saved without them) and the
    templates present only now."""
    now_by_text: Dict[str, Optional[Dict[str, Any]]] = {}
    for item in current:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            now_by_text[item["text"]] = item
        elif isinstance(item, str):
            now_by_text[item] = None
    scored = []
    for case in cases:
        texts = list(dict.fromkeys(case.get("template_texts") or []))
        if case.get("service") != service or not texts:
            continue
        overlap = sum(1 for text in texts if text in now_by_text) / len(texts)
        if overlap >= min_overlap:
            scored.append((overlap, case, texts))
    scored.sort(key=lambda item: (-item[0], -float(item[1].get("created_at") or 0)))
    result = []
    for overlap, case, texts in scored[:k]:
        then_by_text = {
            entry["text"]: entry for entry in case.get("pattern") or []
            if isinstance(entry, dict) and isinstance(entry.get("text"), str)
        }
        result.append({
            "id": case.get("id"), "title": case.get("title"),
            "root_cause": case.get("root_cause"), "resolution": case.get("resolution"),
            "documentation_id": case.get("documentation_id"),
            "occurred_at": case.get("occurred_at"), "overlap": round(overlap, 2),
            "comparison": [
                {"text": text, "then": _numbers(then_by_text.get(text)),
                 "now": _numbers(now_by_text.get(text)) if text in now_by_text else None}
                for text in texts
            ],
            "only_now": [text for text in now_by_text if text not in set(texts)][:MAX_ONLY_LISTED],
        })
    return result

"""Every AI analysis, one JSON object per line (engine appends, web reads).

The per-kind stores hold only the latest record per target; this file keeps
every finished (done or failed) analysis so a user can look back at earlier
answers on the Incidents page. Append only: a re-analysis, delete or vanished
group never rewrites it, so an entry's line number is a stable id.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Statuses worth keeping: a result, or the reason there is none.
LOGGED_STATUSES = ("done", "failed")
SNIPPET_CHARS = 160
KIND_LABELS = {"window": "Alert", "service": "Service", "template": "Template"}


def append_history(
    path: str | Path, kind: str, target_id: str, record: Dict[str, Any],
    at: float | None = None,
) -> None:
    line = json.dumps(
        {"kind": kind, "id": target_id, "at": time.time() if at is None else at,
         "record": record},
        default=str,
    )
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def read_history(path: str | Path) -> List[Tuple[int, Dict[str, Any]]]:
    """(line number, entry) in file order. Unreadable lines (e.g. a torn last
    write) are skipped but still counted, so line numbers stay stable."""
    # ponytail: full scan per read; fine to tens of thousands of lines,
    # trim old entries at engine start if it grows past that.
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for number, line in enumerate(stream):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("record"), dict):
                    entries.append((number, entry))
    except OSError:
        return []
    return entries


def history_entry(path: str | Path, number: int) -> Optional[Dict[str, Any]]:
    return next((entry for n, entry in read_history(path) if n == number), None)


def backfill_history(path: str | Path, records_by_kind: Dict[str, Dict[str, Any]]) -> int:
    """Append every finished record that is not in the history yet (matched
    by kind, id and analyzed_at), so results made before this file existed
    survive their next re-analysis. Safe to run on every engine start."""
    logged = {
        (entry.get("kind"), entry.get("id"), entry["record"].get("analyzed_at"))
        for _, entry in read_history(path)
    }
    added = 0
    for kind, records in records_by_kind.items():
        for target_id, record in sorted(records.items()):
            if not isinstance(record, dict) or record.get("status") not in LOGGED_STATUSES:
                continue
            if (kind, target_id, record.get("analyzed_at")) in logged:
                continue
            append_history(path, kind, target_id, record, at=record.get("analyzed_at"))
            added += 1
    return added


def _snippet(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 1] + "…"


def history_row(number: int, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Compact list row: who was analyzed, when, and the gist of the answer.
    `service` is None for older template entries (the caller resolves it)."""
    kind, target_id, record = entry.get("kind"), str(entry.get("id")), entry["record"]
    service, target = record.get("service"), target_id
    if kind == "window":
        try:
            service, group_id = json.loads(target_id)
            target = f"{group_id} · {service}"
        except (TypeError, ValueError):
            pass
    elif kind == "service":
        service = target_id
    if record.get("status") == "failed":
        headline, snippet = "Failed", record.get("error")
    elif kind == "window":
        suggestion = record.get("suggestion") or {}
        headline = (
            f"Matched {record['documentation_id']} {record.get('document_title') or ''}".strip()
            if record.get("documentation_id")
            else f"Suggestion: {suggestion.get('title') or '—'}"
        )
        snippet = record.get("reasoning")
    elif kind == "service":
        issues = record.get("issues") or []
        headline = f"{(record.get('health') or 'unknown').capitalize()} · {len(issues)} issue(s)"
        snippet = record.get("summary")
    else:
        group = record.get("suggested_group_id")
        headline = f"{(record.get('verdict') or 'unsure').capitalize()}" + (
            " · new group" if group == "new" else f" · → {group}" if group else "")
        snippet = record.get("reasoning")
    return {
        "n": number, "kind": kind, "kind_label": KIND_LABELS.get(kind, str(kind)),
        "id": target_id, "target": target, "service": service,
        "at": entry.get("at"), "status": record.get("status"),
        "headline": _snippet(headline), "snippet": _snippet(snippet),
        "model": record.get("model"), "language": record.get("language"),
    }

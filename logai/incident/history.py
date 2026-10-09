"""Past AI analyses, one JSON object per line (engine appends, web reads).

The per-kind stores hold only the latest record per target; this file keeps
every finished analysis so a user can look back at earlier answers. Append
only: a re-analysis, delete or vanished group never rewrites it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List


def append_history(path: str | Path, kind: str, target_id: str, record: Dict[str, Any]) -> None:
    line = json.dumps(
        {"kind": kind, "id": target_id, "at": time.time(), "record": record}, default=str
    )
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def load_history(path: str | Path, kind: str, target_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Newest first. Unreadable lines (e.g. a torn last write) are skipped."""
    # ponytail: full scan per read; fine to tens of thousands of lines,
    # trim to the last N per target at engine start if it grows past that.
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("kind") == kind and entry.get("id") == target_id:
                    entries.append(entry)
    except OSError:
        return []
    return entries[::-1][:limit]

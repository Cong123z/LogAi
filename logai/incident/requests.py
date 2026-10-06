"""On-demand service analysis requests.

The web is the only writer of this file and the engine only reads it, so
each process keeps its own file (results go to service_analysis.json).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

SCHEMA_VERSION = 1
REQUEST_TTL_SECONDS = 86_400


def load_requests(path: str | Path) -> Dict[str, float]:
    """service -> requested_at; a missing or corrupt file reads as empty."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {}
    requests = raw.get("requests") if isinstance(raw, dict) else None
    if not isinstance(requests, dict):
        return {}
    return {
        str(service): float(requested_at)
        for service, requested_at in requests.items()
        if isinstance(requested_at, (int, float)) and not isinstance(requested_at, bool)
    }


def add_request(
    path: str | Path, service: str, requested_at: float, *, now: Optional[float] = None
) -> None:
    """Record a request atomically and drop requests older than a day."""
    path = Path(path)
    cutoff = (time.time() if now is None else now) - REQUEST_TTL_SECONDS
    requests = {s: t for s, t in load_requests(path).items() if t >= cutoff}
    requests[service] = requested_at
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": SCHEMA_VERSION, "requests": requests}, stream,
                  ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)

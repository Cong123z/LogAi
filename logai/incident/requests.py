"""On-demand LLM requests from the web: analyze or delete an analysis.

The web is the only writer of this file and the engine only reads it; the
engine owns the result files. Keys are "<kind>:<id>" with kind one of
window (id = json.dumps([service, group_id])), service (id = name) or
template (id = template_id). Only the latest action per key is kept.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

SCHEMA_VERSION = 2
REQUEST_TTL_SECONDS = 86_400
KINDS = {"window", "service", "template"}
ACTIONS = {"analyze", "delete"}
# Output language of LLM free text, chosen per request; anything else reads as "en".
LANGUAGES = {"en": "English", "vi": "Vietnamese (Tiếng Việt)"}
DEFAULT_LANGUAGE = "en"

# The web writes this file from several request threads.
_write_lock = threading.Lock()


def request_key(kind: str, target_id: str) -> str:
    return f"{kind}:{target_id}"


def parse_key(key: str) -> Optional[Tuple[str, str]]:
    kind, sep, target_id = str(key).partition(":")
    if not sep or kind not in KINDS or not target_id:
        return None
    return kind, target_id


def load_requests(path: str | Path) -> Dict[str, Tuple[str, float, str]]:
    """key -> (action, requested_at, language); a missing or corrupt file reads
    as empty. Version-1 files ({service: ts}) read as service analyze requests."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {}
    requests = raw.get("requests") if isinstance(raw, dict) else None
    if not isinstance(requests, dict):
        return {}
    result: Dict[str, Tuple[str, float, str]] = {}
    for key, value in requests.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[request_key("service", str(key))] = ("analyze", float(value), DEFAULT_LANGUAGE)
            continue
        if not isinstance(value, dict) or parse_key(key) is None:
            continue
        action, at = value.get("action"), value.get("at")
        language = value.get("language")
        if language not in LANGUAGES:
            language = DEFAULT_LANGUAGE
        if action in ACTIONS and isinstance(at, (int, float)) and not isinstance(at, bool):
            result[key] = (action, float(at), language)
    return result


def add_request(
    path: str | Path, kind: str, target_id: str, requested_at: float,
    *, action: str = "analyze", now: Optional[float] = None,
    language: str = DEFAULT_LANGUAGE,
) -> None:
    """Record a request atomically and drop requests older than a day."""
    if kind not in KINDS or action not in ACTIONS or language not in LANGUAGES:
        raise ValueError(f"invalid request {kind}/{action}/{language}")
    path = Path(path)
    cutoff = (time.time() if now is None else now) - REQUEST_TTL_SECONDS
    with _write_lock:
        requests = {
            key: {"action": a, "at": at, "language": lang}
            for key, (a, at, lang) in load_requests(path).items() if at >= cutoff
        }
        requests[request_key(kind, target_id)] = {
            "action": action, "at": requested_at, "language": language,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": SCHEMA_VERSION, "requests": requests}, stream,
                          ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

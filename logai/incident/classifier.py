"""LLM incident classification for alerting (service, group) windows.

When a window enters ALERTING the realtime pipeline submits it here. A daemon
worker sends the incident evidence (templates, alert state, extracted template
parameters) plus the closest corpus documents to an OpenAI-compatible
/chat/completions endpoint and asks it to pick one document or suggest a fix.
The result is persisted to incident_analysis.json (engine-owned; the web only
reads it). Nothing here ever raises into the poll loop.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from logai.alert.alert_state_machine import _parse_group_id_key, group_id_key
from logai.config import LLMConfig
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.parsing.preprocessor import PLACEHOLDER
from logai.storage.base import JSONStore
from logai.storage.registries import GroupRegistry, TemplateRegistry

logger = logging.getLogger("logai.incident")

SYSTEM_PROMPT = (
    "You are an SRE incident classifier. Choose the single candidate document "
    "that explains and resolves this incident, or none. If none fits, propose a "
    "concise remediation. Reply with JSON only: "
    '{"documentation_id": "<candidate id>" | null, "confidence": 0.0-1.0, '
    '"reasoning": "...", "suggestion": {"title": "...", "text": "...", '
    '"error_code": "..."} | null}'
)
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_TEMPLATES = 10
MAX_CANDIDATE_TEXT = 2000
TITLE_LIMIT, TEXT_LIMIT, ERROR_CODE_LIMIT, ERROR_LIMIT = 200, 20_000, 100, 500
QUEUE_SIZE = 100

WindowKey = Tuple[str, str]


def summarize_parameters(
    entries: Iterable[Tuple[str, List[str]]], top_n: int = 5
) -> List[Dict[str, Any]]:
    """Top values per (template_id, slot) from recent events; masked '<*>'
    values carry nothing and are dropped."""
    counts: Dict[Tuple[str, int], Counter] = defaultdict(Counter)
    for template_id, params in entries:
        for slot, value in enumerate(params):
            if value != PLACEHOLDER:
                counts[(template_id, slot)][value] += 1
    return [
        {
            "template_id": template_id,
            "slot": slot,
            "top_values": [
                [value, count]
                for value, count in sorted(
                    counts[(template_id, slot)].items(), key=lambda kv: (-kv[1], kv[0])
                )[:top_n]
            ],
        }
        for template_id, slot in sorted(counts)
    ]


class IncidentClassifier:
    def __init__(
        self,
        config: LLMConfig,
        matcher: DocumentationMatcher,
        groups: GroupRegistry,
        templates: TemplateRegistry,
        store: JSONStore,
        session: Optional[requests.Session] = None,
        on_result: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.matcher = matcher
        self.groups = groups
        self.templates = templates
        self.store = store
        self._session = session or requests.Session()
        self._on_result = on_result or (lambda result: None)
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_SIZE)
        self._active: set[WindowKey] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- engine-facing API ---------------------------------------------------

    def submit(
        self, window_key: WindowKey, alert: Dict[str, Any],
        recent_params: List[Tuple[str, List[str]]],
    ) -> bool:
        """Queue one analysis without blocking. False when the key is already
        queued/in flight or the queue is full."""
        with self._lock:
            if window_key in self._active:
                return False
            self._active.add(window_key)
        service, group_id = window_key
        # Pending goes first so the worker's final record can never be
        # overwritten by it.
        self.store.set(group_id_key(window_key), {
            "status": "pending", "service": service, "group_id": group_id,
            "queued_at": time.time(),
        })
        try:
            self._queue.put_nowait((window_key, alert, summarize_parameters(recent_params)))
        except queue.Full:
            logger.warning("Incident analysis queue full; dropping %s", window_key)
            with self._lock:
                self._active.discard(window_key)
            self.store.set(group_id_key(window_key), self._failed(
                window_key, "Analysis queue is full; incident was not analyzed"
            ))
            self._on_result("dropped")
            return False
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="incident-classifier", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def drop_group(self, group_id: str) -> int:
        """Remove every service's analysis for a deleted semantic group."""
        keys = [
            key for key in self.store.all()
            if isinstance(identity := _parse_group_id_key(key), tuple)
            and len(identity) == 2 and identity[1] == group_id
        ]
        for key in keys:
            self.store.delete(key, flush=False)
        if keys:
            self.store.flush()
        return len(keys)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                window_key, alert, parameters = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process(window_key, alert, parameters)
            finally:
                with self._lock:
                    self._active.discard(window_key)

    # -- one analysis ----------------------------------------------------------

    def process(
        self, window_key: WindowKey, alert: Dict[str, Any], parameters: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Evidence -> LLM -> validation -> persist. Never raises."""
        try:
            evidence, candidates = self._evidence(window_key, alert, parameters)
            record = self._validate(window_key, self._parse(self._call(evidence)), candidates)
            result = "matched" if record["documentation_id"] else "suggested"
        except Exception as exc:  # noqa: BLE001 - analysis is advisory enrichment
            logger.warning("Incident analysis failed for %s: %s", window_key, exc)
            record = self._failed(window_key, str(exc))
            result = "failed"
        try:
            self.store.set(group_id_key(window_key), record)
        except Exception:  # noqa: BLE001
            logger.exception("Unable to persist incident analysis for %s", window_key)
        self._on_result(result)
        return record

    def _evidence(
        self, window_key: WindowKey, alert: Dict[str, Any], parameters: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        service, group_id = window_key
        group = self.groups.get(group_id)
        members = [
            state for template_id in (group.template_ids if group else [])
            if (state := self.templates.get(template_id)) is not None
        ]
        same_service = [state for state in members if state.service == service]
        members = sorted(same_service or members, key=lambda s: (-s.event_count, s.template_id))
        hits = self.matcher.top_k(self.groups.get_centroid(group_id), self.config.max_candidates)
        evidence = {
            "service": service,
            "group_id": group_id,
            "alert": alert,
            "group": {
                "representative_template": group.representative_template if group else "",
                "event_count": group.event_count if group else 0,
            },
            "templates": [
                {"id": s.template_id, "text": s.template_text, "level": s.level, "count": s.event_count}
                for s in members[:MAX_TEMPLATES]
            ],
            "parameters": parameters,
            "candidates": [
                {
                    "id": entry.doc_id, "title": entry.title,
                    "text": entry.text[:MAX_CANDIDATE_TEXT],
                    "error_code": entry.error_code, "similarity": round(similarity, 4),
                }
                for entry, similarity in hits
            ],
        }
        return evidence, {entry.doc_id: entry.title for entry, _ in hits}

    def _call(self, evidence: Dict[str, Any]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": self.config.max_tokens,
        }
        attempts = self.config.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._session.post(
                    self.config.endpoint, headers=headers, json=body,
                    timeout=self.config.timeout_seconds,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                error: Exception = exc
            else:
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    if response.status_code >= 400:
                        raise RuntimeError(f"LLM returned HTTP {response.status_code}")
                    return str(response.json()["choices"][0]["message"]["content"])
                error = RuntimeError(f"LLM returned HTTP {response.status_code}")
            if attempt + 1 < attempts:
                time.sleep(self.config.retry_backoff_seconds * (2 ** attempt))
        raise error

    @staticmethod
    def _parse(content: str) -> Dict[str, Any]:
        """Tolerates code fences and prose around the JSON object."""
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM reply contains no JSON object")
        reply = json.loads(content[start:end + 1])
        if not isinstance(reply, dict):
            raise ValueError("LLM reply is not a JSON object")
        return reply

    def _validate(
        self, window_key: WindowKey, reply: Dict[str, Any], candidates: Dict[str, str]
    ) -> Dict[str, Any]:
        doc_id = reply.get("documentation_id")
        # Hallucination guard: only a document we offered may be chosen.
        if doc_id not in candidates:
            doc_id = None
        suggestion = None
        raw = reply.get("suggestion")
        if doc_id is None:
            if not (
                isinstance(raw, dict)
                and isinstance(raw.get("title"), str) and raw["title"].strip()
                and isinstance(raw.get("text"), str) and raw["text"].strip()
                and isinstance(raw.get("error_code", ""), str)
            ):
                raise ValueError("LLM chose no candidate document and gave no valid suggestion")
            suggestion = {
                "title": raw["title"].strip()[:TITLE_LIMIT],
                "text": raw["text"].strip()[:TEXT_LIMIT],
                "error_code": (raw.get("error_code") or "").strip()[:ERROR_CODE_LIMIT],
            }
        try:
            confidence = min(1.0, max(0.0, float(reply.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        service, group_id = window_key
        return {
            "status": "done",
            "service": service,
            "group_id": group_id,
            "analyzed_at": time.time(),
            "model": self.config.model,
            "documentation_id": doc_id,
            "document_title": candidates.get(doc_id, "") if doc_id else "",
            "confidence": confidence,
            "reasoning": str(reply.get("reasoning") or "")[:TEXT_LIMIT],
            "suggestion": suggestion,
            "error": None,
        }

    def _failed(self, window_key: WindowKey, error: str) -> Dict[str, Any]:
        service, group_id = window_key
        return {
            "status": "failed", "service": service, "group_id": group_id,
            "analyzed_at": time.time(), "model": self.config.model,
            "documentation_id": None, "document_title": "", "confidence": 0.0,
            "reasoning": "", "suggestion": None, "error": error[:ERROR_LIMIT],
        }

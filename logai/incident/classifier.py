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
MAX_PARAMETER_VALUE = 200
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


def clean_suggestion(raw: Any) -> Optional[Dict[str, str]]:
    """A trimmed {title, text, error_code} suggestion, or None when invalid
    (title/text must be non-empty strings; error_code may be null)."""
    if not (
        isinstance(raw, dict)
        and isinstance(raw.get("title"), str) and raw["title"].strip()
        and isinstance(raw.get("text"), str) and raw["text"].strip()
        and isinstance(raw.get("error_code") or "", str)
    ):
        return None
    return {
        "title": raw["title"].strip()[:TITLE_LIMIT],
        "text": raw["text"].strip()[:TEXT_LIMIT],
        "error_code": (raw.get("error_code") or "").strip()[:ERROR_CODE_LIMIT],
    }


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
        service_store: Optional[JSONStore] = None,
        template_store: Optional[JSONStore] = None,
    ):
        self.config = config
        self.service_store = service_store
        self.template_store = template_store
        self.matcher = matcher
        self.groups = groups
        self.templates = templates
        self.store = store
        self._session = session or requests.Session()
        self._on_result = on_result or (lambda result: None)
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_SIZE)
        # Window tuples and ("service"|"template", id) keys share one set; the
        # tag keeps a service named like a window from ever colliding with it.
        self._active: set[tuple] = set()
        # Active jobs whose record was deleted meanwhile: their result is dropped.
        self._cancelled: set[tuple] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Outcome of the most recent LLM job, reported by the engine heartbeat
        # so the web can say why analysis is not working.
        self.last_error: Optional[str] = None
        self.last_error_at: float = 0.0
        self.last_ok_at: float = 0.0
        # The queue lives in memory: pending records from a previous process
        # will never complete, and their episode is not resubmitted.
        for key, record in self.store.all().items():
            if isinstance(record, dict) and record.get("status") == "pending":
                identity = _parse_group_id_key(key)
                if isinstance(identity, tuple) and len(identity) == 2:
                    failed = self._failed(
                        identity, "Interrupted by engine restart before analysis finished"
                    )
                    failed["requested_at"] = record.get("requested_at")
                    self.store.set(key, failed, flush=False)
        self.store.flush()
        if self.service_store is not None:
            for service, record in self.service_store.all().items():
                if isinstance(record, dict) and record.get("status") == "pending":
                    self.service_store.set(service, self._failed_service(
                        service, record.get("requested_at"),
                        "Interrupted by engine restart before analysis finished",
                    ), flush=False)
            self.service_store.flush()
        if self.template_store is not None:
            for template_id, record in self.template_store.all().items():
                if isinstance(record, dict) and record.get("status") == "pending":
                    self.template_store.set(template_id, self._failed_template(
                        template_id, record.get("requested_at"),
                        "Interrupted by engine restart before analysis finished",
                    ), flush=False)
            self.template_store.flush()

    @property
    def enabled(self) -> bool:
        """False until an endpoint is configured (env or active web profile)."""
        return bool(self.config.endpoint)

    # -- engine-facing API ---------------------------------------------------

    def _enqueue(
        self, active_key: tuple, store: Optional[JSONStore], store_key: str,
        pending: Dict[str, Any], failed: Callable[[str], Dict[str, Any]], job: tuple,
    ) -> bool:
        """Write the pending record, then queue the job without blocking.
        False when there is no store, the key is already queued/in flight, the
        pending record cannot be written, or the queue is full."""
        if store is None:
            return False
        with self._lock:
            if active_key in self._active:
                return False
            self._active.add(active_key)
            self._cancelled.discard(active_key)
        # Pending goes first so the worker's final record can never be
        # overwritten by it.
        try:
            store.set(store_key, {**pending, "status": "pending", "queued_at": time.time()})
        except Exception:  # noqa: BLE001 - never raise into the caller's loop
            logger.exception("Unable to persist pending analysis for %s", active_key)
            with self._lock:
                self._active.discard(active_key)
            return False
        try:
            # ponytail: one worker serves every job kind, so a slow job delays
            # those queued behind it; add workers if latency matters.
            self._queue.put_nowait((active_key, job))
        except queue.Full:
            logger.warning("Analysis queue full; dropping %s", active_key)
            with self._lock:
                self._active.discard(active_key)
            store.set(store_key, failed("Analysis queue is full; nothing was analyzed"))
            self._on_result("dropped")
            return False
        return True

    def submit(
        self, window_key: WindowKey, alert: Dict[str, Any],
        recent_params: List[Tuple[str, List[str]]], requested_at: Optional[float] = None,
    ) -> bool:
        """Queue one (service, group) window analysis."""
        service, group_id = window_key

        def failed(error: str) -> Dict[str, Any]:
            return {**self._failed(window_key, error), "requested_at": requested_at}

        return self._enqueue(
            window_key, self.store, group_id_key(window_key),
            {"service": service, "group_id": group_id, "requested_at": requested_at},
            failed,
            (self.process, window_key, alert, summarize_parameters(recent_params), requested_at),
        )

    def submit_service(
        self, service: str, requested_at: float, evidence: Dict[str, Any],
        candidates: Dict[str, str], sent_groups: set[str],
    ) -> bool:
        """Queue one whole-service analysis."""
        return self._enqueue(
            ("service", service), self.service_store, service,
            {"service": service, "requested_at": requested_at},
            lambda error: self._failed_service(service, requested_at, error),
            (self.process_service, service, requested_at, evidence, candidates, sent_groups),
        )

    def submit_template(
        self, template_id: str, requested_at: float, evidence: Dict[str, Any],
        candidates: Dict[str, str],
    ) -> bool:
        """Queue one unknown-template triage."""
        return self._enqueue(
            ("template", template_id), self.template_store, template_id,
            {"template_id": template_id, "requested_at": requested_at},
            lambda error: self._failed_template(template_id, requested_at, error),
            (self.process_template, template_id, requested_at, evidence, candidates),
        )

    def delete_record(self, kind: str, target_id: str) -> bool:
        """Remove one analysis; an in-flight job for it will not write it back."""
        if kind == "window":
            try:
                identity = tuple(json.loads(target_id))
            except (TypeError, ValueError):
                return False
            active_key, store, store_key = identity, self.store, group_id_key(identity)
        elif kind == "service":
            active_key, store, store_key = ("service", target_id), self.service_store, target_id
        elif kind == "template":
            active_key, store, store_key = ("template", target_id), self.template_store, target_id
        else:
            return False
        with self._lock:
            if active_key in self._active:
                self._cancelled.add(active_key)
        if store is None or store.get(store_key) is None:
            return False
        store.delete(store_key)
        return True

    def _persist(self, active_key: tuple, store: Optional[JSONStore], store_key: str,
                 record: Dict[str, Any]) -> None:
        with self._lock:
            if active_key in self._cancelled:
                self._cancelled.discard(active_key)
                return  # deleted while running: do not resurrect it
        try:
            if store is not None:
                store.set(store_key, record)
        except Exception:  # noqa: BLE001
            logger.exception("Unable to persist analysis for %s", active_key)

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
                active_key, (fn, *args) = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fn(*args)
            finally:
                with self._lock:
                    self._active.discard(active_key)
                    self._cancelled.discard(active_key)

    # -- one analysis ----------------------------------------------------------

    def process(
        self, window_key: WindowKey, alert: Dict[str, Any], parameters: List[Dict[str, Any]],
        requested_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Evidence -> LLM -> validation -> persist. Never raises."""
        cfg = self.config  # snapshot: a profile switch mid-job must not mix configs
        try:
            evidence, candidates = self._evidence(window_key, alert, parameters)
            record = self._validate(
                window_key, self._parse(self._call(evidence, cfg=cfg)), candidates
            )
            result = "matched" if record["documentation_id"] else "suggested"
        except Exception as exc:  # noqa: BLE001 - analysis is advisory enrichment
            logger.warning("Incident analysis failed for %s: %s", window_key, exc)
            record = self._failed(window_key, str(exc))
            result = "failed"
        self._note_outcome(record)
        record["model"] = cfg.model
        record["requested_at"] = requested_at
        self._persist(window_key, self.store, group_id_key(window_key), record)
        self._on_result(result)
        return record

    def process_service(
        self, service: str, requested_at: float, evidence: Dict[str, Any],
        candidates: Dict[str, str], sent_groups: set[str],
    ) -> Dict[str, Any]:
        """Whole-service analysis: LLM -> validation -> persist. Never raises."""
        from logai.incident.service_analysis import (
            SERVICE_SYSTEM_PROMPT,
            validate_service_reply,
        )

        cfg = self.config  # snapshot: a profile switch mid-job must not mix configs
        try:
            content = self._call(
                evidence, SERVICE_SYSTEM_PROMPT, max(cfg.max_tokens, 2000), cfg=cfg
            )
            record = {
                "status": "done", "service": service, "requested_at": requested_at,
                "analyzed_at": time.time(), "model": self.config.model,
                **validate_service_reply(self._parse(content), candidates, sent_groups),
                "error": None,
            }
            result = "service_done"
        except Exception as exc:  # noqa: BLE001 - analysis is advisory enrichment
            logger.warning("Service analysis failed for %s: %s", service, exc)
            record = self._failed_service(service, requested_at, str(exc))
            result = "service_failed"
        self._note_outcome(record)
        record["model"] = cfg.model
        self._persist(("service", service), self.service_store, service, record)
        self._on_result(result)
        return record

    def process_template(
        self, template_id: str, requested_at: float, evidence: Dict[str, Any],
        candidates: Dict[str, str],
    ) -> Dict[str, Any]:
        """Unknown-template triage: LLM -> validation -> persist. Never raises."""
        from logai.incident.template_triage import (
            TEMPLATE_SYSTEM_PROMPT,
            validate_template_reply,
        )

        cfg = self.config  # snapshot: a profile switch mid-job must not mix configs
        try:
            content = self._call(evidence, TEMPLATE_SYSTEM_PROMPT, cfg=cfg)
            record = {
                "status": "done", "template_id": template_id, "requested_at": requested_at,
                "analyzed_at": time.time(),
                **validate_template_reply(self._parse(content), candidates),
                "error": None,
            }
            result = "template_done"
        except Exception as exc:  # noqa: BLE001 - analysis is advisory enrichment
            logger.warning("Template triage failed for %s: %s", template_id, exc)
            record = self._failed_template(template_id, requested_at, str(exc))
            result = "template_failed"
        self._note_outcome(record)
        record["model"] = cfg.model
        self._persist(("template", template_id), self.template_store, template_id, record)
        self._on_result(result)
        return record

    def _failed_template(
        self, template_id: str, requested_at: Optional[float], error: str
    ) -> Dict[str, Any]:
        return {
            "status": "failed", "template_id": template_id, "requested_at": requested_at,
            "analyzed_at": time.time(), "model": self.config.model, "verdict": None,
            "reasoning": "", "confidence": 0.0, "suggested_group_id": None,
            "suggested_group_rep": "", "error": error[:ERROR_LIMIT],
        }

    def _note_outcome(self, record: Dict[str, Any]) -> None:
        if record.get("status") == "failed":
            self.last_error = str(record.get("error") or "unknown error")[:300]
            self.last_error_at = time.time()
        else:
            self.last_error = None
            self.last_ok_at = time.time()

    def _failed_service(
        self, service: str, requested_at: Optional[float], error: str
    ) -> Dict[str, Any]:
        return {
            "status": "failed", "service": service, "requested_at": requested_at,
            "analyzed_at": time.time(), "model": self.config.model,
            "health": None, "summary": "", "issues": [], "error": error[:ERROR_LIMIT],
        }

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
            "parameters": [
                {
                    **item,
                    "top_values": [
                        [str(value)[:MAX_PARAMETER_VALUE], count]
                        for value, count in item["top_values"]
                    ],
                }
                for item in parameters
                if item["template_id"] in {s.template_id for s in members[:MAX_TEMPLATES]}
            ],
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

    def _call(
        self, evidence: Dict[str, Any], system_prompt: str = SYSTEM_PROMPT,
        max_tokens: Optional[int] = None, cfg: Optional[LLMConfig] = None,
    ) -> str:
        # One config for the whole request, retries included: a profile switch
        # meanwhile must never send this key to the other profile's host.
        cfg = cfg or self.config
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        body = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": max_tokens or cfg.max_tokens,
        }
        attempts = cfg.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._session.post(
                    cfg.endpoint, headers=headers, json=body,
                    timeout=cfg.timeout_seconds,
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
                time.sleep(cfg.retry_backoff_seconds * (2 ** attempt))
        raise error

    @staticmethod
    def _parse(content: str) -> Dict[str, Any]:
        """Tolerates code fences and prose around the JSON object."""
        decoder = json.JSONDecoder()
        start = content.find("{")
        while start >= 0:
            try:
                reply, _ = decoder.raw_decode(content, start)
            except json.JSONDecodeError:
                reply = None
            if isinstance(reply, dict):
                return reply
            start = content.find("{", start + 1)
        raise ValueError("LLM reply contains no JSON object")

    def _validate(
        self, window_key: WindowKey, reply: Dict[str, Any], candidates: Dict[str, str]
    ) -> Dict[str, Any]:
        doc_id = reply.get("documentation_id")
        # Hallucination guard: only a document we offered may be chosen.
        if doc_id not in candidates:
            doc_id = None
        suggestion = None
        if doc_id is None:
            suggestion = clean_suggestion(reply.get("suggestion"))
            if suggestion is None:
                raise ValueError("LLM chose no candidate document and gave no valid suggestion")
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

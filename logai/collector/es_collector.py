"""Elasticsearch collector (plan section 4.1).

Uses `search_after` pagination sorted by (@timestamp, _id) so it can resume
exactly where it left off via a persisted checkpoint, with retry+backoff on
transient errors. Also exposes a bounded historical range query used by the
training pipeline (plan section 3.1).
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from elasticsearch import Elasticsearch

try:
    from elasticsearch import (
        AuthenticationException,
        AuthorizationException,
        BadRequestError,
        NotFoundError,
    )
    # Filter out mock objects when elasticsearch is mocked in tests
    _ES_NON_RETRYABLE = tuple(
        cls for cls in (
            AuthenticationException, AuthorizationException,
            BadRequestError, NotFoundError,
        )
        if isinstance(cls, type) and issubclass(cls, BaseException)
    )
except (ImportError, AttributeError):
    _ES_NON_RETRYABLE = ()

from logai.config import ElasticsearchConfig
from logai.models import RawLog
from logai.parsing.preprocessor import find_level
from logai.storage.checkpoint import CheckpointStore

logger = logging.getLogger("logai.collector")


def _build_client(config: ElasticsearchConfig) -> Elasticsearch:
    kwargs: Dict[str, Any] = {
        "hosts": config.hosts,
        "request_timeout": config.request_timeout_seconds,
    }
    if config.username and config.password:
        kwargs["basic_auth"] = (config.username, config.password)
    return Elasticsearch(**kwargs)


def _extract_service(src: Dict[str, Any]) -> str:
    """Resolve the RawLog.service string from an ES `_source` dict.

    Preference order (each optional, falls through on absence):
      1. `service` - either a flat string, or an ECS-nested object
         (`{"service": {"name": "..."}}`, used by Filebeat/APM integrations).
      2. `service_code2` - the flat service-name string used by this
         deployment's Logstash pipeline (its sibling `service_code` holds a
         host IP, NOT a service name, and must never be used here).
      3. "unknown".
    """
    service = src.get("service")
    if isinstance(service, str) and service:
        return service
    if isinstance(service, dict):
        name = service.get("name")
        if isinstance(name, str) and name:
            return name

    service_code2 = src.get("service_code2")
    if isinstance(service_code2, str) and service_code2:
        return service_code2

    return "unknown"


def _extract_level_from_message(message: str) -> Optional[str]:
    """Level from the message prefix: searched within the first 5 tokens,
    preceded only by date/time/pid/[thread]-like tokens (shared with the
    Drain3 preprocessor so both agree on what the prefix is)."""
    found = find_level((message or "").strip())
    return found[0] if found else None


def _extract_level(src: Dict[str, Any]) -> str:
    """Read the log level, preferring the ECS nested path `log.level`.

    Elastic Agent / Filebeat integrations (the current Elastic Integrations
    format) write level under `log.level`, not a top-level `level` field.
    Falls back to a legacy flat `level` field for older/custom shippers, then
    to the level found in the first 5 tokens of `message` for shippers that
    index neither.
    """
    log_obj = src.get("log")
    if isinstance(log_obj, dict):
        level = log_obj.get("level")
        if isinstance(level, str) and level:
            return level

    flat_level = src.get("level")
    if isinstance(flat_level, str) and flat_level:
        return flat_level

    from_message = _extract_level_from_message(src.get("message", ""))
    if from_message:
        return from_message

    return "INFO"


# Fields that are shipper/pipeline plumbing (Filebeat/Logstash/ECS envelope)
# with no log-analysis value, plus fields already consumed into service/level
# above - excluded from RawLog.metadata so it only carries business fields
# (e.g. moduleCode, groupModule) that aren't yet used elsewhere in the schema.
_NOISE_KEYS = (
    "@timestamp", "service", "level", "message",
    "ecs", "agent", "@version", "input", "logstash_instance",
    "kafka_cluster", "log", "host", "service_code", "service_code2",
)


def _hit_to_rawlog(hit: Dict[str, Any], index: str) -> RawLog:
    """Convert a single ES hit dict to RawLog.

    Raises ValueError with a descriptive message when the hit is missing
    required fields (_source, _id) or _source is not a dict.  Callers
    should catch exceptions per-hit so one bad document never crashes an
    entire batch.
    """
    src = hit.get("_source")
    if not isinstance(src, dict):
        raise ValueError(
            f"Hit missing or invalid _source (got {type(src).__name__}): "
            f"_id={hit.get('_id', '<no_id>')}"
        )
    hit_id = hit.get("_id")
    if not hit_id:
        raise ValueError(
            f"Hit missing _id, _source keys: {list(src.keys())[:5]}"
        )
    ts_raw = src.get("@timestamp")
    ts = _parse_timestamp(ts_raw)
    return RawLog(
        timestamp=ts,
        service=_extract_service(src),
        level=_extract_level(src),
        message=src.get("message", ""),
        metadata={k: v for k, v in src.items() if k not in _NOISE_KEYS},
        event_id=hit_id,
        es_index=index,
        es_doc_id=hit_id,
    )


def _safe_hits_to_rawlogs(
    hits: List[Dict[str, Any]],
    index: str,
    malformed_counter: Optional[Any] = None,
) -> List[RawLog]:
    """Convert a list of ES hits to RawLog, skipping malformed ones.

    Each hit is processed independently so a single bad document never
    crashes the entire batch.  Malformed hits are logged at WARNING level.
    If a malformed_counter metric is provided, its .inc() method is called.
    """
    raw_logs: List[RawLog] = []
    for hit in hits:
        try:
            raw_logs.append(_hit_to_rawlog(hit, index))
        except Exception as exc:  # noqa: BLE001
            hit_id = "<unknown>"
            try:
                hit_id = (
                    hit.get("_id", "<no_id>")
                    if isinstance(hit, dict)
                    else repr(hit)[:80]
                )
            except Exception:  # noqa: BLE001
                pass
            logger.warning("Skipping malformed ES hit %s: %s", hit_id, exc)
            if malformed_counter is not None:
                malformed_counter.inc()
    return raw_logs


def _parse_timestamp(value: Any) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    # ISO8601 string, e.g. 2026-09-01T10:00:01Z
    from datetime import datetime, timezone
    try:
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return time.time()


class ElasticsearchCollector:
    _malformed_counter: Optional[Any] = None
    _on_retry_hook: Optional[Callable[[int, BaseException], None]] = None

    def __init__(self, config: ElasticsearchConfig, checkpoint: CheckpointStore):
        self.config = config
        self.checkpoint = checkpoint
        self.client = _build_client(config)
        # Injected by the pipeline for Prometheus metrics (see RealtimePipeline).
        self._malformed_counter = None
        self._on_retry_hook = None

    def _search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Search with exponential backoff, classifying ES errors.

        Non-retryable client errors (400/401/403/404) fail fast instead of
        wasting the full retry budget.  Retryable errors (connection/timeout,
        429, 5xx) back off.  An instance-level ``_on_retry_hook`` is invoked
        before each sleep so the pipeline can increment ``logai_retry_total``.
        """
        attempt = 0
        while True:
            try:
                return self.client.search(index=self.config.index, body=body)
            except _ES_NON_RETRYABLE:
                raise  # fail-fast: no point retrying a 400/401/403/404
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt > 5:
                    logger.error(
                        "_search failed after %d attempts: %s", attempt - 1, exc
                    )
                    raise
                delay = min(1.0 * (2 ** (attempt - 1)), 60.0)
                delay += random.uniform(0, delay * 0.1)  # jitter
                logger.warning(
                    "_search attempt %d/5 failed (%s), retrying in %.2fs",
                    attempt, exc, delay,
                )
                if self._on_retry_hook is not None:
                    self._on_retry_hook(attempt, exc)
                time.sleep(delay)

    def poll_batch(self) -> Tuple[List[RawLog], Optional[List[Any]]]:
        """Fetch a batch and return its cursor without advancing checkpoint.

        The realtime orchestrator commits the cursor only after processing the
        complete batch and flushing durable state.
        """
        search_after = self.checkpoint.get_search_after()
        if search_after and len(search_after) >= 2 and isinstance(search_after[1], str):
            # Migration safety: old checkpoint used string _id, which fails with _doc sort.
            search_after = [search_after[0], 0]

        body: Dict[str, Any] = {
            "size": self.config.batch_size,
            "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}],
        }
        if search_after:
            body["query"] = {"match_all": {}}
            body["search_after"] = search_after
        else:
            # No cursor yet: this is the very first poll ever (or right after
            # a checkpoint reset). Floor the query at the moment the realtime
            # pipeline first started instead of match_all, so it never rewinds
            # into the historical window already consumed by training. The
            # floor is persisted so repeated empty polls (no new logs yet)
            # don't keep pushing it forward and silently drop events.
            start_ts = self.checkpoint.get_start_ts()
            if start_ts is None:
                start_ts = time.time()
                self.checkpoint.set_start_ts(start_ts)
            from datetime import datetime, timezone
            gte_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
            body["query"] = {"range": {"@timestamp": {"gte": gte_iso}}}

        response = self._search(body)
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return [], None

        counter = getattr(self, "_malformed_counter", None)
        raw_logs = _safe_hits_to_rawlogs(hits, self.config.index, counter)

        # Cursor must be extracted from the last hit regardless of whether
        # that hit parsed successfully — otherwise the batch would be
        # refetched indefinitely.
        last_sort = hits[-1].get("sort")
        if last_sort is None:
            logger.error(
                "Last hit in batch missing 'sort' field; cannot advance "
                "cursor.  Batch had %d hits, %d parsed successfully.",
                len(hits), len(raw_logs),
            )

        return raw_logs, last_sort

    def run_forever(self) -> Iterator[Tuple[List[RawLog], Optional[List[Any]]]]:
        """Generator that polls indefinitely, sleeping `poll_interval_seconds`
        when there's nothing new. Caller drives processing per batch."""
        while True:
            batch, cursor = self.poll_batch()
            if batch:
                yield batch, cursor
            else:
                time.sleep(self.config.poll_interval_seconds)

    def stream_historical_batches(
        self,
        start_ts: float,
        end_ts: Optional[float] = None,
        max_docs: int = 200_000,
        batch_size: Optional[int] = None,
        initial_search_after: Optional[List[Any]] = None,
    ) -> Iterator[Tuple[List[RawLog], Optional[List[Any]]]]:
        """Stream historical logs in batches without accumulating all logs into RAM.

        Yields (batch, last_sort_value) tuples for each page fetched.
        Does NOT touch or modify self.checkpoint (protects realtime pipeline).
        """
        from datetime import datetime, timezone

        range_filter: Dict[str, Any] = {
            "gte": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
        }
        if end_ts:
            range_filter["lte"] = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()

        chunk_size = batch_size or self.config.batch_size
        search_after = initial_search_after
        total_fetched = 0

        while total_fetched < max_docs:
            if search_after and len(search_after) >= 2 and isinstance(search_after[1], str):
                search_after = [search_after[0], 0]

            current_limit = min(chunk_size, max_docs - total_fetched)
            body: Dict[str, Any] = {
                "size": current_limit,
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}],
                "query": {"range": {"@timestamp": range_filter}},
            }
            if search_after:
                body["search_after"] = search_after

            response = self._search(body)
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            counter = getattr(self, "_malformed_counter", None)
            batch = _safe_hits_to_rawlogs(hits, self.config.index, counter)
            total_fetched += len(batch)

            last_sort = hits[-1].get("sort")
            if last_sort is None:
                logger.error(
                    "Historical hit missing 'sort' field; stopping stream."
                )
                break
            search_after = last_sort

            yield batch, search_after

            if len(hits) < current_limit:
                break
        else:
            if max_docs > 0:
                logger.warning(
                    "Historical stream stopped at max_docs=%d; logs after %s were NOT "
                    "used for training (time range truncated)",
                    max_docs,
                    search_after[0] if search_after else "the range start",
                )

    def fetch_historical_range(
        self, start_ts: float, end_ts: Optional[float] = None, max_docs: int = 200_000
    ) -> List[RawLog]:
        """Used by the training pipeline to pull a bounded historical window
        for building templates/groups/models (plan section 3.1).

        Preserved for backward compatibility, delegates to stream_historical_batches.
        """
        results: List[RawLog] = []
        for batch, _ in self.stream_historical_batches(start_ts, end_ts, max_docs=max_docs):
            results.extend(batch)
        return results

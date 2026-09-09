"""Elasticsearch collector (plan section 4.1).

Uses `search_after` pagination sorted by (@timestamp, _id) so it can resume
exactly where it left off via a persisted checkpoint, with retry+backoff on
transient errors. Also exposes a bounded historical range query used by the
training pipeline (plan section 3.1).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, List, Optional

from elasticsearch import Elasticsearch

from logai.config import ElasticsearchConfig
from logai.models import RawLog
from logai.reliability.retry import retry_with_backoff
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


def _hit_to_rawlog(hit: Dict[str, Any], index: str) -> RawLog:
    src = hit["_source"]
    ts_raw = src.get("@timestamp")
    ts = _parse_timestamp(ts_raw)
    return RawLog(
        timestamp=ts,
        service=src.get("service", "unknown"),
        level=src.get("level", "INFO"),
        message=src.get("message", ""),
        metadata={k: v for k, v in src.items() if k not in ("@timestamp", "service", "level", "message")},
        event_id=hit["_id"],
        es_index=index,
        es_doc_id=hit["_id"],
    )


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
    def __init__(self, config: ElasticsearchConfig, checkpoint: CheckpointStore):
        self.config = config
        self.checkpoint = checkpoint
        self.client = _build_client(config)

    @retry_with_backoff(exceptions=(Exception,))
    def _search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.search(index=self.config.index, body=body)

    def poll_batch(self) -> List[RawLog]:
        """Fetch up to `batch_size` new documents since the last checkpoint,
        ordered by (@timestamp, _id), and advance the checkpoint."""
        search_after = self.checkpoint.get_search_after()
        body: Dict[str, Any] = {
            "size": self.config.batch_size,
            "sort": [{"@timestamp": "asc"}, {"_id": "asc"}],
            "query": {"match_all": {}},
        }
        if search_after:
            body["search_after"] = search_after

        response = self._search(body)
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return []

        raw_logs = [_hit_to_rawlog(h, self.config.index) for h in hits]
        last_hit = hits[-1]
        self.checkpoint.set_search_after(last_hit["sort"])
        self.checkpoint.set_last_timestamp(raw_logs[-1].timestamp)
        return raw_logs

    def run_forever(self) -> Iterator[List[RawLog]]:
        """Generator that polls indefinitely, sleeping `poll_interval_seconds`
        when there's nothing new. Caller drives processing per batch."""
        while True:
            batch = self.poll_batch()
            if batch:
                yield batch
            else:
                time.sleep(self.config.poll_interval_seconds)

    def fetch_historical_range(
        self, start_ts: float, end_ts: Optional[float] = None, max_docs: int = 200_000
    ) -> List[RawLog]:
        """Used by the training pipeline to pull a bounded historical window
        for building templates/groups/models (plan section 3.1)."""
        from datetime import datetime, timezone

        range_filter: Dict[str, Any] = {
            "gte": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
        }
        if end_ts:
            range_filter["lte"] = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()

        results: List[RawLog] = []
        search_after = None
        while len(results) < max_docs:
            body: Dict[str, Any] = {
                "size": min(self.config.batch_size, max_docs - len(results)),
                "sort": [{"@timestamp": "asc"}, {"_id": "asc"}],
                "query": {"range": {"@timestamp": range_filter}},
            }
            if search_after:
                body["search_after"] = search_after
            response = self._search(body)
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break
            results.extend(_hit_to_rawlog(h, self.config.index) for h in hits)
            search_after = hits[-1]["sort"]
            if len(hits) < body["size"]:
                break
        return results

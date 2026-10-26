"""Elasticsearch source (copied from logai-engine logai/collector/es_collector.py).

The only code that talks to Elasticsearch. `page` returns one page of raw
hits of a search target (pattern) sorted by (@timestamp, _doc) from a millisecond position;
the reader keeps the position and drops re-read docs by _id. One attempt per
search: the reader's failure count and the next poll are the backoff.
`hit_to_line` turns a hit into a `Line`.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional

from elasticsearch import Elasticsearch

from logai2.config import ElasticsearchConfig, ReaderConfig
from logai2.reader.normalize import find_level


_REQUEST_TIMEOUT_SECONDS = 30.0
# The @timestamp sort value is always epoch milliseconds (also for a
# date_nanos field), so a hit's first sort value can start a range query.
_SORT = [{"@timestamp": {"order": "asc", "numeric_type": "date", "unmapped_type": "date"}}, {"_doc": "asc"}]
# Page size used to read one crowded millisecond whole: ES's default
# index.max_result_window, the largest page a plain search may return.
_WHOLE_MILLISECOND = 10_000
_FILTER_PATH = "hits.hits._id,hits.hits._index,hits.hits._source,hits.hits.sort"


class Line(NamedTuple):
    ts: float
    service: str
    level: str
    message: str
    module: str
    doc_id: str


def _build_client(config: ElasticsearchConfig) -> Elasticsearch:
    kwargs: Dict[str, Any] = {"hosts": config.hosts, "request_timeout": _REQUEST_TIMEOUT_SECONDS}
    if config.username and config.password:
        kwargs["basic_auth"] = (config.username, config.password)
    if not config.verify_certs:
        kwargs.update(verify_certs=False, ssl_show_warn=False)
    return Elasticsearch(**kwargs)


def _extract_service(src: Dict[str, Any]) -> str:
    """`service` (flat string or ECS `{"name": ...}`), else `service_code2`
    (this deployment's Logstash field; its sibling `service_code` holds a host
    IP, never a service name), else "unknown"."""
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


def _extract_level_from_message(message: Any) -> Optional[str]:
    """Level from the message prefix: searched within the first 5 tokens,
    preceded only by date/time/pid/[thread]-like tokens (shared with the
    normalizer so both agree on what the prefix is)."""
    if not isinstance(message, str):
        return None
    found = find_level(message.strip())
    return found[0] if found else None


def _extract_level(src: Dict[str, Any]) -> str:
    """ECS `log.level`, else a flat `level`, else the message prefix, else INFO."""
    log_obj = src.get("log")
    if isinstance(log_obj, dict):
        level = log_obj.get("level")
        if isinstance(level, str) and level.strip():
            return level.strip().upper()
    flat_level = src.get("level")
    if isinstance(flat_level, str) and flat_level.strip():
        return flat_level.strip().upper()
    return _extract_level_from_message(src.get("message", "")) or "INFO"


def _coerce_message(value: Any) -> str:
    """A field as a string: missing/null -> "", any other type -> str()."""
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _parse_timestamp(value: Any) -> float:
    """`@timestamp` as epoch seconds; falls back to now when unparseable."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # ES `date` fields stored as a long are epoch millis.
            return float(value) / 1000.0 if value > 1e11 else float(value)
        if isinstance(value, str) and value.strip():
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return time.time()


def _sort_seconds(hit: Dict[str, Any]) -> Optional[float]:
    try:
        return int((hit.get("sort") or [None])[0]) / 1000.0
    except (TypeError, ValueError):
        return None


def hit_to_line(hit: Dict[str, Any], module_field: str, max_line_bytes: int) -> Optional[Line]:
    """One ES hit as a Line; None when the hit has no `_id` or no dict
    `_source` (the caller counts it as malformed). The time is the hit's sort
    value (epoch ms, what the reader's position uses), else `@timestamp`."""
    if not isinstance(hit, dict):
        return None
    src = hit.get("_source")
    doc_id = hit.get("_id")
    if not isinstance(src, dict) or not doc_id:
        return None
    # Byte cut can split a multibyte char; "ignore" drops the partial tail.
    ts = _sort_seconds(hit)
    message = _coerce_message(src.get("message")).encode("utf-8")[:max_line_bytes].decode("utf-8", "ignore")
    nul = lambda s: s.replace("\x00", "")   # Postgres text refuses NUL; every stored field comes from here
    return Line(
        ts=ts if ts is not None else _parse_timestamp(src.get("@timestamp")),
        service=nul(_extract_service(src)),
        level=nul(_extract_level({**src, "message": message})),
        message=nul(message),
        module=nul(_coerce_message(src.get(module_field))),
        doc_id=str(doc_id),
    )


class EsSource:
    def __init__(self, cfg: ElasticsearchConfig, reader_cfg: ReaderConfig):
        self.cfg = cfg
        self.reader_cfg = reader_cfg
        self.client = _build_client(cfg)
        self._source_fields = ["@timestamp", "service", "service_code2", "level",
                               "log.level", "message", reader_cfg.module_field]

    def reconnect(self, cfg: ElasticsearchConfig) -> None:
        """Use new connection settings (from the web's Data sources page)."""
        self.cfg = cfg
        self.client = _build_client(cfg)

    def _search(self, body: Dict[str, Any], index: str, timeout: int | None = None) -> Dict[str, Any]:
        """One attempt, no retry: a failing index must not stall the poll of
        the others (spec section 6); the reader counts its failures."""
        # A deleted index must not fail the poll of every other one.
        extra = {} if timeout is None else {"request_timeout": timeout}
        return self.client.search(index=index, body=body, ignore_unavailable=True, allow_no_indices=True,
                                  filter_path=_FILTER_PATH, **extra)

    def count(self, target: str, from_ms: int, to_ms: int, timeout: int | None = None) -> int:
        """Docs of `target` with from_ms <= @timestamp <= to_ms (the re-read's progress target)."""
        body = {"query": {"bool": {"filter": [{"range": {"@timestamp": {
            "gte": from_ms, "lte": to_ms, "format": "epoch_millis"}}}]}}}
        extra = {} if timeout is None else {"request_timeout": timeout}
        response = self.client.count(index=target, body=body, ignore_unavailable=True, allow_no_indices=True, **extra)
        return int((response or {}).get("count", 0))

    def page(self, target: str, from_ms: int, upper_ms: int, timeout: int | None = None) -> List[Dict[str, Any]]:
        """Raw hits of `target` (a search expression: a pattern, optionally
        followed by `,-<pattern>` exclusions) with from_ms <= @timestamp <= upper_ms, sorted
        by (@timestamp, _doc).

        Starting at from_ms (inclusive) re-reads the docs sharing the previous
        page's last millisecond: search_after on _doc would skip some of them
        (_doc is per shard and changes on merges), so the reader drops the
        re-read ones by _id. When that millisecond alone fills a page,
        re-reading it would never move forward, so it is read whole with a
        larger page.
        """
        size = self.cfg.batch_size
        body: Dict[str, Any] = {
            "size": size,
            "sort": _SORT,
            "_source": self._source_fields,
            "query": {"bool": {"filter": [{"range": {"@timestamp": {
                "gte": from_ms, "lte": upper_ms, "format": "epoch_millis"}}}]}},
        }

        def hits_of(response: Dict[str, Any]) -> List[Dict[str, Any]]:
            return (response or {}).get("hits", {}).get("hits", [])

        def stuck(hits: List[Dict[str, Any]], page: int) -> bool:
            return len(hits) >= page and (hits[-1].get("sort") or [None])[0] == from_ms

        hits = hits_of(self._search(body, target, timeout))
        if stuck(hits, size) and size < _WHOLE_MILLISECOND:
            # The millisecond fills the page: read all of it in one request.
            body = {**body, "size": _WHOLE_MILLISECOND}
            hits = hits_of(self._search(body, target, timeout))
        if stuck(hits, body["size"]):
            # ponytail: one millisecond holding more than 10k docs is continued
            # by one search_after page, where _doc can still skip a doc and a
            # >20k millisecond still stalls; use PIT + _shard_doc if a source
            # ever logs that much in one millisecond.
            hits = hits + hits_of(self._search({**body, "search_after": hits[-1]["sort"]}, target, timeout))
        return hits

    def oldest_ms(self, target: str) -> int | None:
        """Epoch ms of the oldest @timestamp under `target` (same expression as page), None when empty."""
        body = {"size": 0, "aggs": {"oldest": {"min": {"field": "@timestamp"}}}}
        response = self.client.search(index=target, body=body, ignore_unavailable=True, allow_no_indices=True,
                                      filter_path="aggregations.oldest.value")
        value = ((response or {}).get("aggregations") or {}).get("oldest", {}).get("value")
        return None if value is None else int(value)

    def resolve(self, patterns: List[str]) -> Dict[str, List[str]]:
        """pattern -> sorted concrete open indices (data-stream backing indices
        included, hidden ones not). A pattern matching nothing maps to [].
        One attempt, no backoff: the caller retries on its own timer."""
        result: Dict[str, List[str]] = {}
        for pattern in patterns:
            found = self.client.indices.get(
                index=pattern, expand_wildcards="open", ignore_unavailable=True,
                allow_no_indices=True,
            )
            result[pattern] = sorted(dict(found))
        return result

    def list_indices(self) -> List[Dict[str, Any]]:
        """Open, non-hidden indices for the web picker (one attempt)."""
        response = self.client.cat.indices(format="json", h="index,docs.count,health,status")
        # elasticsearch-py 8 wraps the list in a ListApiResponse (not a list).
        rows = getattr(response, "body", response)
        indices = []
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("index") or "")
            if not name or name.startswith("."):
                continue
            count = row.get("docs.count")
            indices.append({
                "index": name,
                "docs_count": int(count) if str(count or "").isdigit() else None,
                "health": row.get("health"),
                "status": row.get("status"),
            })
        return sorted(indices, key=lambda item: item["index"])

"""Elasticsearch source: hit -> Line, malformed hits, paging and retry.

Ported from logai-engine tests/test_es_malformed_hits.py,
test_es_retry_classification.py and test_es_index_selection.py. EsSource is
always driven through a fake client (or a fake _search); no real ES.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from logai2.config import ElasticsearchConfig, ReaderConfig
from logai2.reader import es_source
from logai2.reader.es_source import EsSource, hit_to_line
from tests.fakes import hit


def line_of(**src):
    return hit_to_line({"_id": "d1", "_source": src}, "moduleCode", 8192)


# -- hit -> Line -------------------------------------------------------------

def test_hit_to_line_fields():
    line = hit_to_line(hit("a", 1_760_000_000_000, service_code2="lixi",
                           message="2026-10-18 ERROR boom", moduleCode="M1"), "moduleCode", 8192)
    assert (line.service, line.level, line.module, line.doc_id) == ("lixi", "ERROR", "M1", "a")
    assert line.ts == 1_760_000_000.0


def test_missing_module_and_service():
    line = hit_to_line(hit("a", 1_760_000_000_000, message="x"), "moduleCode", 8192)
    assert (line.service, line.module, line.level) == ("unknown", "", "INFO")


def test_multibyte_truncation_is_safe():
    msg = "Lỗi kết nối " * 2000
    line = hit_to_line(hit("a", 1_760_000_000_000, message=msg), "moduleCode", 8192)
    assert len(line.message.encode("utf-8")) <= 8192
    assert msg.startswith(line.message)


def test_module_field_is_configurable():
    line = hit_to_line(hit("a", 1, groupModule="G", moduleCode="M"), "groupModule", 8192)
    assert line.module == "G"


# -- malformed hits (from test_es_malformed_hits.py) ------------------------

def test_malformed_hit_is_none():
    assert hit_to_line({"_source": {}}, "moduleCode", 8192) is None


@pytest.mark.parametrize("bad", [
    {"_id": "x"},                               # missing _source
    {"_id": "x", "_source": "a string"},        # _source not a dict
    {"_id": "x", "_source": ["a", "b"]},        # _source is a list
    {"_source": {"message": "m"}},              # missing _id
    {"_id": "", "_source": {"message": "m"}},   # empty _id
    "not a dict",                               # hit itself is junk
])
def test_malformed_hits_are_none(bad):
    assert hit_to_line(bad, "moduleCode", 8192) is None


def test_missing_timestamp_uses_current_time():
    before = time.time()
    assert line_of(message="no ts").ts >= before


def test_missing_optional_fields_use_defaults():
    line = line_of(**{"@timestamp": "2026-09-10T12:00:00Z"})
    assert (line.service, line.level, line.message, line.module) == ("unknown", "INFO", "", "")


def test_service_falls_back_to_service_code2():
    # service_code (no "2") holds a host IP and must never be the service.
    line = line_of(service_code2="vtn_cntt_vas_066", service_code={"host_ip": "10.240.175.121"})
    assert line.service == "vtn_cntt_vas_066"


def test_service_prefers_flat_service_then_ecs_name():
    assert line_of(service="payment", service_code2="other").service == "payment"
    assert line_of(service={"name": "apm"}).service == "apm"
    assert line_of(service={"name": None}, service_code2="svc").service == "svc"
    assert line_of(service_code2={"x": 1}).service == "unknown"
    assert line_of(service_code2=["a"]).service == "unknown"


@pytest.mark.parametrize("message,expected", [
    ("30/09/2026 14:00:31 DEBUG [gossip-handlers-321] jgroups:name=X responded", "DEBUG"),
    ("27/07/2026 00:00:00.024 INFO [TransactionManager] DBAdapter: error_code=10700", "INFO"),
    ("2026-09-30T07:00:32.036Z ERROR [main] Payment failed", "ERROR"),
    ("2026-09-30 14:00:31,123 [main] WARN com.x.Y - slow request", "WARN"),
    ("Connection ERROR while calling gateway", "INFO"),
    ("just a plain message with no structured prefix at all", "INFO"),
])
def test_level_from_message_prefix(message, expected):
    assert line_of(message=message).level == expected


def test_level_field_is_normalised():
    assert line_of(log={"level": "debug"}).level == "DEBUG"
    assert line_of(level=" warn ").level == "WARN"
    assert line_of(level="  ", message="x ERROR y").level == "INFO"


def test_message_not_a_string():
    assert line_of(message=123).message == "123"
    assert line_of(message=None).message == ""
    assert line_of(message={"a": 1}).level == "INFO"


def test_timestamps():
    assert line_of(**{"@timestamp": 1790751632036}).ts == pytest.approx(1790751632.036)
    assert line_of(**{"@timestamp": 1790751632}).ts == 1790751632.0
    assert line_of(**{"@timestamp": "2026-09-30T07:00:32.036Z"}).ts == pytest.approx(1790751632.036)
    for bad in ({"a": 1}, [1], "garbage", "", True):
        before = time.time()
        assert line_of(**{"@timestamp": bad}).ts >= before


# -- paging (from test_es_index_selection.py) --------------------------------

def make_source(responses, batch_size=10_000):
    source = EsSource(ElasticsearchConfig(batch_size=batch_size), ReaderConfig())
    source.client = MagicMock()
    source.client.search.side_effect = responses
    return source


def bodies(source):
    return [c.kwargs["body"] for c in source.client.search.call_args_list]


def test_page_query_shape():
    page = [hit("a", 5, message="m")]
    source = make_source([{"hits": {"hits": page}}])
    assert source.page("idx-a", 5, 9) == page
    call = source.client.search.call_args
    assert call.kwargs["index"] == "idx-a" and call.kwargs["ignore_unavailable"] is True
    assert call.kwargs["filter_path"] == "hits.hits._id,hits.hits._source,hits.hits.sort"
    body = call.kwargs["body"]
    assert body["size"] == 10_000
    assert body["sort"] == [{"@timestamp": {"order": "asc", "numeric_type": "date"}}, {"_doc": "asc"}]
    assert body["query"] == {"bool": {"filter": [
        {"range": {"@timestamp": {"gte": 5, "lte": 9, "format": "epoch_millis"}}}]}}
    assert body["_source"] == ["@timestamp", "service", "service_code2", "level",
                               "log.level", "message", "moduleCode"]
    assert "search_after" not in body


def test_empty_response_is_empty_list():
    assert make_source([{}]).page("idx", 1, 2) == []


def test_page_reads_whole_millisecond():
    # A page whose last hit sits on from_ms would never move forward, so
    # that millisecond is re-requested whole with size 10,000.
    same_ms = [hit(f"d{i}", 9, message="m") for i in range(5)]
    source = make_source([{"hits": {"hits": same_ms[:3]}}, {"hits": {"hits": same_ms}}], batch_size=3)
    assert source.page("idx-a", 9, 100) == same_ms
    first, second = bodies(source)
    assert first["size"] == 3 and second["size"] == 10_000
    assert "search_after" not in second


def test_full_page_past_from_ms_is_not_reread():
    hits = [hit("a", 9), hit("b", 10), hit("c", 11)]
    source = make_source([{"hits": {"hits": hits}}], batch_size=3)
    assert source.page("idx", 9, 100) == hits
    assert source.client.search.call_count == 1


def test_millisecond_beyond_largest_page_falls_back_to_search_after(monkeypatch):
    monkeypatch.setattr(es_source, "_WHOLE_MILLISECOND", 3)
    full = [hit(f"d{i}", 9) for i in range(3)]
    after = [hit("d3", 9), hit("e", 10)]
    source = make_source([{"hits": {"hits": full}}, {"hits": {"hits": after}}], batch_size=3)
    assert source.page("idx", 9, 100) == full + after
    assert bodies(source)[-1]["search_after"] == full[-1]["sort"]


# -- resolve / list_indices --------------------------------------------------

def test_resolve_and_list_indices():
    source = EsSource(ElasticsearchConfig(), ReaderConfig())
    source.client = MagicMock()
    source.client.indices.get.side_effect = lambda index, **_: (
        {"logs-2": {}, "logs-1": {}} if index == "logs-*" else {})
    # Shaped like elasticsearch-py's ListApiResponse: the list is in .body.
    source.client.cat.indices.return_value = MagicMock(body=[
        {"index": ".kibana", "docs.count": "3"},
        {"index": "logs-1", "docs.count": "10", "health": "green", "status": "open"},
    ])
    assert source.resolve(["logs-*", "none"]) == {"logs-*": ["logs-1", "logs-2"], "none": []}
    assert source.list_indices() == [
        {"index": "logs-1", "docs_count": 10, "health": "green", "status": "open"}]


def test_reconnect_builds_new_client():
    source = EsSource(ElasticsearchConfig(), ReaderConfig())
    old = source.client
    cfg = ElasticsearchConfig(hosts=["http://other:9200"])
    source.reconnect(cfg)
    assert source.cfg is cfg and source.client is not old


# -- retry classification (from test_es_retry_classification.py) ------------

class BadRequestError(Exception): pass          # 400
class AuthenticationException(Exception): pass  # 401
class AuthorizationException(Exception): pass   # 403
class NotFoundError(Exception): pass            # 404
class Transient(Exception): pass                # connection / timeout / 5xx


@pytest.fixture
def retry(monkeypatch):
    monkeypatch.setattr(es_source, "_ES_NON_RETRYABLE", (
        BadRequestError, AuthenticationException, AuthorizationException, NotFoundError))
    sleep = MagicMock()
    monkeypatch.setattr(es_source.time, "sleep", sleep)
    source = EsSource(ElasticsearchConfig(), ReaderConfig())
    source.client = MagicMock()
    return source, sleep


@pytest.mark.parametrize("exc", [
    BadRequestError, AuthenticationException, AuthorizationException, NotFoundError])
def test_non_retryable_fails_fast(retry, exc):
    source, sleep = retry
    source.client.search.side_effect = exc("boom")
    hook = MagicMock()
    source._on_retry_hook = hook
    with pytest.raises(exc):
        source._search({"query": {}}, "idx")
    assert source.client.search.call_count == 1 and sleep.call_count == 0
    assert hook.call_count == 0


def test_retryable_retries_five_times_then_raises(retry):
    source, sleep = retry
    source.client.search.side_effect = Transient("down")
    with pytest.raises(Transient):
        source._search({"query": {}}, "idx")
    assert source.client.search.call_count == 6 and sleep.call_count == 5


def test_recovers_after_transient_failures_and_hook_fires(retry):
    source, sleep = retry
    source.client.search.side_effect = [Transient("x"), Transient("x"), Transient("x"), {"ok": True}]
    hook = MagicMock()
    source._on_retry_hook = hook
    assert source._search({"query": {}}, "idx") == {"ok": True}
    assert sleep.call_count == 3
    assert [c.args[0] for c in hook.call_args_list] == [1, 2, 3]


def test_abort_retry_stops_early(retry):
    source, sleep = retry
    source.client.search.side_effect = Transient("down")
    source._abort_retry = lambda: True
    with pytest.raises(Transient):
        source._search({"query": {}}, "idx")
    assert source.client.search.call_count == 1 and sleep.call_count == 0

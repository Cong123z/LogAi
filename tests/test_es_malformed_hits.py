"""Tests for malformed ES hit isolation in ElasticsearchCollector.

Verifies that:
- A single malformed hit does not crash the entire batch
- Valid hits in the same batch are still returned
- Missing _source, missing _id, non-dict _source are all handled
- Missing 'sort' on last hit returns cursor=None
- All hits malformed returns ([], cursor) so the pipeline is not stuck
- Input/output contract of poll_batch() and stream_historical_batches()
  is unchanged for valid data
"""
from __future__ import annotations

import logging
import sys
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

# Lightweight mock for elasticsearch driver
if "elasticsearch" not in sys.modules:
    sys.modules["elasticsearch"] = MagicMock()

from logai.collector.es_collector import (
    ElasticsearchCollector,
    _hit_to_rawlog,
    _safe_hits_to_rawlogs,
)
from logai.config import ElasticsearchConfig
from logai.models import RawLog
from logai.storage.checkpoint import CheckpointStore


def _make_hit(
    doc_id: str = "doc_1",
    source: Any = None,
    sort: Optional[List[Any]] = None,
    *,
    include_source: bool = True,
    include_id: bool = True,
    include_sort: bool = True,
) -> Dict[str, Any]:
    """Build a synthetic ES hit dict with fine-grained control."""
    hit: Dict[str, Any] = {}
    if include_id:
        hit["_id"] = doc_id
    if include_source:
        hit["_source"] = source if source is not None else {
            "@timestamp": "2026-09-10T10:00:00Z",
            "service": "payment",
            "level": "INFO",
            "message": f"Event {doc_id}",
        }
    if include_sort:
        hit["sort"] = sort if sort is not None else [1725958800, doc_id]
    return hit


def _make_valid_hits(n: int, base_id: int = 0) -> List[Dict[str, Any]]:
    return [_make_hit(doc_id=f"doc_{base_id + i}") for i in range(n)]


# ── Unit tests for _hit_to_rawlog ────────────────────────────────────────

class TestHitToRawlog(unittest.TestCase):
    """Verify _hit_to_rawlog raises clear errors on malformed hits and
    produces correct RawLog on valid hits (input/output parity)."""

    def test_valid_hit_returns_rawlog(self):
        hit = _make_hit(doc_id="abc123")
        result = _hit_to_rawlog(hit, "app-logs-*")
        self.assertIsInstance(result, RawLog)
        self.assertEqual(result.event_id, "abc123")
        self.assertEqual(result.es_doc_id, "abc123")
        self.assertEqual(result.service, "payment")
        self.assertEqual(result.level, "INFO")
        self.assertIn("Event abc123", result.message)
        self.assertEqual(result.es_index, "app-logs-*")

    def test_missing_source_raises_valueerror(self):
        hit = _make_hit(include_source=False)
        with self.assertRaises(ValueError) as ctx:
            _hit_to_rawlog(hit, "app-logs-*")
        self.assertIn("_source", str(ctx.exception))
        self.assertIn("NoneType", str(ctx.exception))

    def test_source_not_dict_raises_valueerror(self):
        hit = _make_hit(source="this is a string, not a dict")
        with self.assertRaises(ValueError) as ctx:
            _hit_to_rawlog(hit, "app-logs-*")
        self.assertIn("_source", str(ctx.exception))
        self.assertIn("str", str(ctx.exception))

    def test_source_is_list_raises_valueerror(self):
        hit = _make_hit(source=["a", "b"])
        with self.assertRaises(ValueError) as ctx:
            _hit_to_rawlog(hit, "app-logs-*")
        self.assertIn("list", str(ctx.exception))

    def test_missing_id_raises_valueerror(self):
        hit = _make_hit(include_id=False)
        with self.assertRaises(ValueError) as ctx:
            _hit_to_rawlog(hit, "app-logs-*")
        self.assertIn("_id", str(ctx.exception))

    def test_empty_string_id_raises_valueerror(self):
        hit = _make_hit()
        hit["_id"] = ""
        with self.assertRaises(ValueError) as ctx:
            _hit_to_rawlog(hit, "app-logs-*")
        self.assertIn("_id", str(ctx.exception))

    def test_missing_timestamp_uses_current_time(self):
        """Missing @timestamp should fallback gracefully (existing behavior)."""
        source = {"service": "auth", "level": "WARN", "message": "no ts"}
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertIsInstance(result.timestamp, float)
        self.assertGreater(result.timestamp, 0)

    def test_missing_optional_fields_use_defaults(self):
        """Missing service/level/message should use defaults (existing behavior)."""
        source = {"@timestamp": "2026-09-10T12:00:00Z"}
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.service, "unknown")
        self.assertEqual(result.level, "INFO")
        self.assertEqual(result.message, "")

    def test_extra_fields_go_to_metadata(self):
        """Extra fields in _source should end up in metadata (existing behavior)."""
        source = {
            "@timestamp": "2026-09-10T12:00:00Z",
            "service": "api",
            "level": "DEBUG",
            "message": "hello",
            "trace_id": "abc",
            "region": "ap-southeast-1",
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.metadata["trace_id"], "abc")
        self.assertEqual(result.metadata["region"], "ap-southeast-1")
        self.assertNotIn("@timestamp", result.metadata)

    def test_service_falls_back_to_service_code2(self):
        """No `service` field at all (production schema): service_code2 is the
        real service string; service_code (no "2") is a trap holding host_ip,
        never the service name, and must not leak into service or metadata."""
        source = {
            "@timestamp": "2026-09-30T07:00:32.036Z",
            "service_code2": "vtn_cntt_vas_066",
            "service_code": {"host_ip": "10.240.175.121"},
            "message": "some message",
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.service, "vtn_cntt_vas_066")
        self.assertNotIn("service_code", result.metadata)
        self.assertNotIn("service_code2", result.metadata)

    def test_service_prefers_flat_service_over_service_code2(self):
        """Backward compatibility: a real `service` field always wins over
        service_code2 when both are present."""
        source = {
            "@timestamp": "2026-09-30T07:00:32.036Z",
            "service": "payment",
            "service_code2": "other",
            "message": "some message",
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.service, "payment")

    def test_level_parsed_from_message_third_token(self):
        """No log.level/level field: level is read from the message prefix
        (here the 3rd token, after date and time)."""
        source = {
            "@timestamp": "2026-09-30T07:00:32.036Z",
            "message": (
                "30/09/2026 14:00:31 DEBUG [gossip-handlers-321] "
                "jgroups:name=NewGossipRouter responded to GOSSIP_GET with []"
            ),
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.level, "DEBUG")

    def test_level_parsed_from_message_transaction_sample(self):
        """Real transaction-log sample: level must be read as INFO, never
        confused with `error_code=10700` deep inside the message body - only
        the first 5 tokens are searched."""
        source = {
            "@timestamp": "2026-07-27T00:00:00.024Z",
            "message": (
                "27/07/2026 00:00:00.024 INFO [TransactionManager] DBAdapter: "
                "Log success request his TransactionInfo{reqID=1384223044, "
                "error_code=10700, description=Receive incorrect}"
            ),
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.level, "INFO")

    def test_level_parsed_from_message_second_and_fourth_token(self):
        """The level is not always the 3rd token: it is searched within the
        first 5 tokens, behind date/time/pid/[thread]-like tokens only."""
        for message, expected in (
            ("2026-09-30T07:00:32.036Z ERROR [main] Payment failed", "ERROR"),
            ("2026-09-30 14:00:31,123 [main] WARN com.x.Y - slow request", "WARN"),
        ):
            hit = _make_hit(source={"@timestamp": "2026-09-30T07:00:32.036Z", "message": message})
            self.assertEqual(_hit_to_rawlog(hit, "idx").level, expected, message)

    def test_level_word_inside_sentence_is_not_a_level(self):
        """A level word preceded by a plain word is message text, not the
        prefix, so the INFO default applies."""
        hit = _make_hit(source={
            "@timestamp": "2026-09-30T07:00:32.036Z",
            "message": "Connection ERROR while calling gateway",
        })
        self.assertEqual(_hit_to_rawlog(hit, "idx").level, "INFO")

    def test_level_regex_no_match_when_third_token_not_a_level(self):
        """A message that doesn't follow the date/time/LEVEL convention falls
        back to the INFO default rather than guessing from elsewhere in the
        text."""
        source = {
            "@timestamp": "2026-09-30T07:00:32.036Z",
            "message": "just a plain message with no structured prefix at all",
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        self.assertEqual(result.level, "INFO")

    def test_noise_fields_excluded_business_fields_kept(self):
        """Full production-shaped _source: Filebeat/Logstash/ECS envelope
        fields must never reach metadata, but app-specific business fields
        (moduleCode, groupModule) must be preserved."""
        source = {
            "@timestamp": "2026-09-30T07:00:32.036Z",
            "ecs": {"version": "1.11.0"},
            "agent": {"version": "7.15.2", "hostname": "VAS-APP121", "type": "filebeat"},
            "service_code2": "vtn_cntt_vas_066",
            "host": {"name": "VAS-APP121"},
            "log": {"offset": 38903723, "file": {"path": "/u01/data_2g/gossip/gossip.log"}},
            "logstash_instance": "ls-152-67",
            "@version": "1",
            "message": "30/09/2026 14:00:31 DEBUG [gossip-handlers-321] jgroups:...",
            "groupModule": "GOSSIP",
            "moduleCode": "VTN_CNTT_VAS_066_301",
            "input": {"type": "log"},
            "service_code": {"host_ip": "10.240.175.121"},
            "kafka_cluster": "cluster07",
        }
        hit = _make_hit(source=source)
        result = _hit_to_rawlog(hit, "idx")
        for noise_key in (
            "ecs", "agent", "@version", "input", "logstash_instance",
            "kafka_cluster", "log", "host", "service_code", "service_code2",
        ):
            self.assertNotIn(noise_key, result.metadata)
        self.assertEqual(result.metadata["groupModule"], "GOSSIP")
        self.assertEqual(result.metadata["moduleCode"], "VTN_CNTT_VAS_066_301")


# ── Unit tests for _safe_hits_to_rawlogs ─────────────────────────────────

GOSSIP_MESSAGE = (
    "30/09/2026 14:00:31 DEBUG [gossip-handlers-321] "
    "jgroups:name=NewGossipRouterWARNING_EXTEND_DATA_MI_HT2: "
    "ConnectionHandler[peer: /10.240.175.140, logical_addrs: "
    "warning_extend_data_mi_ht2_node2] responded to GOSSIP_GET with []"
)

# Real `_source` from index udcntt-vtn_cntt_vas_066-2026.09.30.
GOSSIP_SOURCE: Dict[str, Any] = {
    "ecs": {"version": "1.11.0"},
    "agent": {"version": "7.15.2", "hostname": "VAS-APP121", "type": "filebeat"},
    "service_code2": "vtn_cntt_vas_066",
    "host": {"name": "VAS-APP121"},
    "log": {
        "offset": 38903723,
        "file": {"path": "/u01/data_2g/gossip/new_gossip_extend/gossip_ex_ht2/log/full/gossip.log"},
    },
    "logstash_instance": "ls-152-67",
    "@timestamp": "2026-09-30T07:00:32.036Z",
    "@version": "1",
    "message": GOSSIP_MESSAGE,
    "groupModule": "GOSSIP",
    "moduleCode": "VTN_CNTT_VAS_066_301",
    "input": {"type": "log"},
    "service_code": {"host_ip": "10.240.175.121"},
    "kafka_cluster": "cluster07",
}


class TestProductionGossipHit(unittest.TestCase):
    def test_fields_extracted(self):
        raw = _hit_to_rawlog(
            {"_id": "U5cd8aABgTaDf7peb8en", "_source": GOSSIP_SOURCE},
            "udcntt-vtn_cntt_vas_066-2026.09.30",
        )
        self.assertEqual(raw.service, "vtn_cntt_vas_066")
        self.assertEqual(raw.level, "DEBUG")
        self.assertAlmostEqual(raw.timestamp, 1790751632.036, places=3)
        self.assertEqual(raw.message, GOSSIP_MESSAGE)
        self.assertEqual(
            raw.metadata, {"groupModule": "GOSSIP", "moduleCode": "VTN_CNTT_VAS_066_301"}
        )


class TestOddFieldsAreIgnored(unittest.TestCase):
    """A missing or oddly typed field falls back; the hit is never dropped."""

    def _raw(self, **source: Any) -> RawLog:
        rows = _safe_hits_to_rawlogs([{"_id": "d1", "_source": source}], "idx")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_message_not_a_string(self):
        self.assertEqual(self._raw(message=123).message, "123")
        self.assertEqual(self._raw(message=None).message, "")
        self.assertEqual(self._raw().message, "")
        self.assertEqual(self._raw(message={"a": 1}).level, "INFO")

    def test_bad_timestamp_falls_back_to_now(self):
        for value in ({"a": 1}, [1], "garbage", "", True):
            before = time.time()
            self.assertGreaterEqual(self._raw(**{"@timestamp": value}).timestamp, before)

    def test_epoch_millis_timestamp(self):
        self.assertAlmostEqual(
            self._raw(**{"@timestamp": 1790751632036}).timestamp, 1790751632.036, places=3
        )
        self.assertEqual(self._raw(**{"@timestamp": 1790751632}).timestamp, 1790751632.0)

    def test_level_field_is_normalised(self):
        self.assertEqual(self._raw(log={"level": "debug"}).level, "DEBUG")
        self.assertEqual(self._raw(level=" warn ").level, "WARN")
        self.assertEqual(self._raw(level="  ", message="x ERROR y").level, "INFO")

    def test_odd_service_fields(self):
        self.assertEqual(self._raw(service_code2={"x": 1}).service, "unknown")
        self.assertEqual(self._raw(service_code2=["a"]).service, "unknown")
        self.assertEqual(self._raw(service={"name": None}, service_code2="svc").service, "svc")


class TestSafeHitsToRawlogs(unittest.TestCase):
    """Verify batch-level malformed hit isolation."""

    def test_all_valid_hits(self):
        hits = _make_valid_hits(5)
        result = _safe_hits_to_rawlogs(hits, "app-logs-*")
        self.assertEqual(len(result), 5)
        for r in result:
            self.assertIsInstance(r, RawLog)

    def test_malformed_hit_mid_batch_skipped(self):
        """One bad hit in the middle: 9 good hits returned, 1 skipped."""
        hits = _make_valid_hits(10)
        # Corrupt hit #5: remove _source
        del hits[5]["_source"]
        result = _safe_hits_to_rawlogs(hits, "app-logs-*")
        self.assertEqual(len(result), 9)
        ids = {r.event_id for r in result}
        self.assertNotIn("doc_5", ids)
        for i in range(10):
            if i != 5:
                self.assertIn(f"doc_{i}", ids)

    def test_missing_id_hit_skipped(self):
        hits = _make_valid_hits(3)
        del hits[1]["_id"]
        result = _safe_hits_to_rawlogs(hits, "idx")
        self.assertEqual(len(result), 2)
        ids = {r.event_id for r in result}
        self.assertIn("doc_0", ids)
        self.assertIn("doc_2", ids)

    def test_source_not_dict_skipped(self):
        hits = _make_valid_hits(3)
        hits[0]["_source"] = "not a dict"
        result = _safe_hits_to_rawlogs(hits, "idx")
        self.assertEqual(len(result), 2)

    def test_all_hits_malformed_returns_empty(self):
        hits = [
            {"_id": "a"},                        # missing _source
            {"_source": "string"},               # _source not dict
            {"_source": {"msg": "x"}},           # missing _id
        ]
        for h in hits:
            h["sort"] = [1, "x"]
        result = _safe_hits_to_rawlogs(hits, "idx")
        self.assertEqual(len(result), 0)

    def test_non_dict_hit_element_skipped(self):
        """A hit that is not a dict at all (extreme edge case)."""
        hits = _make_valid_hits(2)
        hits.insert(1, "this is not a dict")  # type: ignore
        result = _safe_hits_to_rawlogs(hits, "idx")
        # The string element should be skipped, 2 valid hits remain
        self.assertEqual(len(result), 2)

    def test_logs_warning_for_each_skipped_hit(self):
        hits = _make_valid_hits(3)
        del hits[0]["_source"]
        del hits[2]["_id"]
        with self.assertLogs("logai.collector", level="WARNING") as cm:
            result = _safe_hits_to_rawlogs(hits, "idx")
        self.assertEqual(len(result), 1)
        self.assertEqual(len(cm.output), 2)
        self.assertIn("Skipping malformed", cm.output[0])
        self.assertIn("Skipping malformed", cm.output[1])

    def test_malformed_counter_incremented(self):
        counter = MagicMock()
        hits = _make_valid_hits(4)
        del hits[1]["_source"]
        del hits[3]["_id"]
        result = _safe_hits_to_rawlogs(hits, "idx", malformed_counter=counter)
        self.assertEqual(len(result), 2)
        self.assertEqual(counter.inc.call_count, 2)


# ── Integration tests for poll_batch ─────────────────────────────────────

class TestPollBatchMalformedHits(unittest.TestCase):
    """Verify poll_batch() contract is preserved with malformed hit handling."""

    def setUp(self):
        self.config = ElasticsearchConfig()
        self.config.batch_size = 10

        # Use a fake checkpoint with in-memory state
        self.checkpoint = MagicMock(spec=CheckpointStore)
        self.checkpoint.get_search_after.return_value = None

        self.collector = ElasticsearchCollector(self.config, self.checkpoint)

    def _mock_search_response(self, hits: List[Dict[str, Any]]) -> Dict:
        return {"hits": {"hits": hits}}

    def test_valid_batch_returns_all_logs_and_cursor(self):
        """Contract parity: valid batch returns (List[RawLog], sort)."""
        hits = _make_valid_hits(5)
        self.collector._search = MagicMock(
            return_value=self._mock_search_response(hits)
        )
        raw_logs, cursor = self.collector.poll_batch()
        self.assertEqual(len(raw_logs), 5)
        self.assertEqual(cursor, hits[-1]["sort"])
        for r in raw_logs:
            self.assertIsInstance(r, RawLog)

    def test_empty_response_returns_empty_and_none(self):
        """Contract parity: no hits returns ([], None)."""
        self.collector._search = MagicMock(
            return_value={"hits": {"hits": []}}
        )
        raw_logs, cursor = self.collector.poll_batch()
        self.assertEqual(raw_logs, [])
        self.assertIsNone(cursor)

    def test_malformed_hit_mid_batch_others_survive(self):
        """1 bad hit in 10 → 9 RawLog, cursor from last hit."""
        hits = _make_valid_hits(10)
        del hits[4]["_source"]  # corrupt hit #4
        self.collector._search = MagicMock(
            return_value=self._mock_search_response(hits)
        )
        raw_logs, cursor = self.collector.poll_batch()
        self.assertEqual(len(raw_logs), 9)
        self.assertEqual(cursor, hits[-1]["sort"])

    def test_last_hit_malformed_still_returns_cursor(self):
        """Even if the last hit has bad _source, its sort must still be used."""
        hits = _make_valid_hits(5)
        del hits[-1]["_source"]  # last hit corrupt, but has sort
        self.collector._search = MagicMock(
            return_value=self._mock_search_response(hits)
        )
        raw_logs, cursor = self.collector.poll_batch()
        self.assertEqual(len(raw_logs), 4)
        # Cursor still comes from last hit's sort
        self.assertEqual(cursor, hits[-1]["sort"])

    def test_last_hit_missing_sort_returns_none_cursor(self):
        """If last hit has no 'sort' field, cursor is None."""
        hits = _make_valid_hits(3)
        del hits[-1]["sort"]
        self.collector._search = MagicMock(
            return_value=self._mock_search_response(hits)
        )
        raw_logs, cursor = self.collector.poll_batch()
        self.assertEqual(len(raw_logs), 3)  # all have valid _source/_id
        self.assertIsNone(cursor)

    def test_all_hits_malformed_returns_empty_with_cursor(self):
        """All bad hits → empty list, cursor from last hit's sort."""
        hits = [
            {"_id": "a", "sort": [1, "a"]},                    # no _source
            {"_source": "str", "_id": "b", "sort": [2, "b"]},  # bad _source
            {"_source": {}, "sort": [3, "c"]},                  # no _id
        ]
        self.collector._search = MagicMock(
            return_value=self._mock_search_response(hits)
        )
        raw_logs, cursor = self.collector.poll_batch()
        self.assertEqual(len(raw_logs), 0)
        self.assertEqual(cursor, [3, "c"])  # still advances


# ── Integration tests for stream_historical_batches ──────────────────────

class TestStreamHistoricalMalformedHits(unittest.TestCase):
    """Verify stream_historical_batches() contract with malformed hits."""

    def setUp(self):
        self.config = ElasticsearchConfig()
        self.config.batch_size = 5
        self.checkpoint = MagicMock(spec=CheckpointStore)
        self.checkpoint.get_search_after.return_value = None
        self.collector = ElasticsearchCollector(self.config, self.checkpoint)

    def test_valid_stream_returns_all(self):
        hits = _make_valid_hits(5)
        self.collector._search = MagicMock(
            side_effect=[
                {"hits": {"hits": hits}},
                {"hits": {"hits": []}},  # second page: empty → stop
            ]
        )
        batches = list(
            self.collector.stream_historical_batches(start_ts=0.0, max_docs=10)
        )
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0][0]), 5)
        self.assertEqual(batches[0][1], hits[-1]["sort"])

    def test_malformed_hit_skipped_in_stream(self):
        hits = _make_valid_hits(5)
        del hits[2]["_source"]
        self.collector._search = MagicMock(
            side_effect=[
                {"hits": {"hits": hits}},
                {"hits": {"hits": []}},  # second page: empty → stop
            ]
        )
        batches = list(
            self.collector.stream_historical_batches(start_ts=0.0, max_docs=10)
        )
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0][0]), 4)

    def test_missing_sort_stops_stream(self):
        """If last hit has no sort, stream should stop (cannot paginate)."""
        hits = _make_valid_hits(3)
        del hits[-1]["sort"]
        self.collector._search = MagicMock(
            return_value={"hits": {"hits": hits}}
        )
        batches = list(
            self.collector.stream_historical_batches(start_ts=0.0, max_docs=100)
        )
        # Stream yields nothing because it breaks before yield
        self.assertEqual(len(batches), 0)


if __name__ == "__main__":
    unittest.main()

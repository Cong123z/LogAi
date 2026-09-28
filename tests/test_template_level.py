"""TemplateState.level: the most severe log level ever recorded per template.

Covers the three contracts that matter:
  1. Monotonic promotion - a low-severity event never downgrades an ERROR
     template - in BOTH realtime branches (known-grouped and known-pending) and
     in the training aggregation.
  2. Free-form level normalisation (casing / WARN vs WARNING synonyms / empty).
  3. Backward compatibility: pre-existing template_registry.json files that
     predate the `level` field must still load, and the field must survive a real
     disk round-trip through JSONStore.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

for mod in [
    "numpy",
    "sklearn",
    "sklearn.ensemble",
    "hdbscan",
    "elasticsearch",
    "drain3",
    "drain3.template_miner",
    "drain3.file_persistence",
    "drain3.template_miner_config",
    "drain3.masking",
    "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from logai.config import AppConfig
from logai.models import (
    DEFAULT_LEVEL,
    LEVEL_RANK,
    GroupState,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.realtime.realtime_pipeline import PENDING_GROUP_ID, RealtimePipeline
from logai.storage.registries import TemplateRegistry


def _norm(raw) -> str:
    """Mirror the inline normalisation used by both pipelines (no helper exists
    in production code on purpose; this is the test-side copy)."""
    return str(raw or DEFAULT_LEVEL).strip().upper()


def _promote(current: str, incoming: str) -> str:
    """Return the level that must be stored: the max by rank, monotonic."""
    inc = _norm(incoming)
    if LEVEL_RANK.get(inc, LEVEL_RANK[DEFAULT_LEVEL]) > LEVEL_RANK.get(
        current, LEVEL_RANK[DEFAULT_LEVEL]
    ):
        return inc
    return current


class TestLevelRanking(unittest.TestCase):
    def test_case_and_synonyms_normalise(self):
        self.assertEqual(_norm("error"), "ERROR")
        self.assertEqual(_norm("Error"), "ERROR")
        self.assertEqual(_norm("  warn "), "WARN")
        # WARN and WARNING share a rank so either spelling ranks correctly.
        self.assertEqual(LEVEL_RANK["WARN"], LEVEL_RANK["WARNING"])
        self.assertEqual(LEVEL_RANK["ERROR"], LEVEL_RANK["error".upper()])

    def test_empty_level_is_inert_default(self):
        self.assertEqual(_norm(None), DEFAULT_LEVEL)
        self.assertEqual(_norm(""), DEFAULT_LEVEL)

    def test_unknown_level_ranks_as_info_and_never_poisons(self):
        # A level the engine has never seen must NOT outrank ERROR: an app that
        # logs "SEVERE" once should not lock a template above a real ERROR...
        self.assertLessEqual(LEVEL_RANK.get("SEVERE", LEVEL_RANK[DEFAULT_LEVEL]),
                             LEVEL_RANK["ERROR"])
        # ...it is treated at the INFO rank, so a later ERROR still wins.
        self.assertEqual(_promote("SEVERE", "ERROR"), "ERROR")

    def test_max_never_downgrades(self):
        self.assertEqual(_promote("ERROR", "WARN"), "ERROR")
        self.assertEqual(_promote("ERROR", "INFO"), "ERROR")
        self.assertEqual(_promote("INFO", "ERROR"), "ERROR")
        self.assertEqual(_promote("WARN", "ERROR"), "ERROR")
        # equal rank keeps the incumbent (no pointless rewrite)
        self.assertEqual(_promote("WARNING", "WARN"), "WARNING")


class TestLegacyRegistryCompat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = AppConfig()
        self.config.storage.base_dir = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registry_without_level_field_still_loads(self):
        """A template_registry.json written before `level` existed must not crash
        TemplateState(**raw); it falls back to the INFO default."""
        legacy = {
            "T00001": {
                "template_id": "T00001",
                "template_text": "old template",
                "service": "hdfs",
                "module": "",
                "first_seen": 1.0,
                "last_seen": 2.0,
                "event_count": 5,
                "group_id": "G0001",
            }
        }
        path = f"{self.tmp}/{self.config.storage.template_registry_file}"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy, f)

        reg = TemplateRegistry(self.config.storage)
        state = reg.get("T00001")
        self.assertIsNotNone(state)
        self.assertEqual(state.level, DEFAULT_LEVEL)
        self.assertEqual(state.event_count, 5)  # other fields intact

    def test_level_survives_disk_round_trip(self):
        reg = TemplateRegistry(self.config.storage)
        reg.upsert(
            TemplateState(
                template_id="T00002",
                template_text="t",
                service="hdfs",
                level="ERROR",
            ),
            flush=False,
        )
        reg.flush()

        raw = json.load(open(f"{self.tmp}/{self.config.storage.template_registry_file}"))
        self.assertIn("level", raw["T00002"])
        self.assertEqual(raw["T00002"]["level"], "ERROR")

        # Reload from disk into a fresh registry.
        self.assertEqual(TemplateRegistry(self.config.storage).get("T00002").level, "ERROR")


class TestRealtimePromotion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = AppConfig()
        self.config.storage.base_dir = self.tmp
        self.config.storage.model_dir = f"{self.tmp}/models"
        self.config.drain3.persistence_path = f"{self.tmp}/drain3.bin"
        self.pipeline = RealtimePipeline(self.config)
        # The embedding value is never used numerically here (the clusterer is
        # mocked), so a sentinel is enough and keeps this test independent of
        # whichever numpy mock a sibling test installed in sys.modules.
        self.pipeline.embedder.embed_one = MagicMock(return_value=[0.0])
        self.pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=(None, 0.5))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _event(self, template_id: str, timestamp: float, level: str) -> ParsedEvent:
        return ParsedEvent(
            raw=RawLog(
                event_id=f"evt_{template_id}_{timestamp}",
                timestamp=timestamp,
                service="hdfs",
                level=level,
                message="msg",
            ),
            template_id=template_id,
            template="t",
            parameters=[],
            is_new_template=False,
        )

    def _seed(self, template_id: str, group_id, level: str) -> None:
        if group_id:
            self.pipeline.group_registry.upsert(GroupState(group_id=group_id))
        self.pipeline.template_registry.upsert(
            TemplateState(
                template_id=template_id,
                template_text="t",
                service="hdfs",
                level=level,
                first_seen=1.0,
                last_seen=1.0,
                event_count=1,
                group_id=group_id,
            )
        )

    def test_known_grouped_promotes_and_never_downgrades(self):
        self._seed("T_G", "G0001", "WARN")
        self.pipeline._assign_group(self._event("T_G", 100.0, "ERROR"))
        self.assertEqual(self.pipeline.template_registry.get("T_G").level, "ERROR")
        # a later INFO must not downgrade
        self.pipeline._assign_group(self._event("T_G", 200.0, "INFO"))
        self.assertEqual(self.pipeline.template_registry.get("T_G").level, "ERROR")
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 0)

    def test_known_pending_promotes_on_the_fast_path(self):
        """The pending fast-path previously skipped re-embedding; it must still
        promote level, otherwise a pending template stuck on ERROR stays INFO."""
        self._seed("T_P", None, "INFO")
        # first pass heals group_id to PENDING
        self.pipeline._assign_group(self._event("T_P", 50.0, "INFO"))
        self.assertEqual(
            self.pipeline.template_registry.get("T_P").group_id, PENDING_GROUP_ID
        )
        # subsequent pending events promote without embedding
        self.pipeline._assign_group(self._event("T_P", 60.0, "error"))
        self.assertEqual(self.pipeline.template_registry.get("T_P").level, "ERROR")
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 0)

    def test_unknown_template_records_first_level(self):
        self.pipeline._assign_group(self._event("T_NEW", 10.0, "FATAL"))
        state = self.pipeline.template_registry.get("T_NEW")
        self.assertEqual(state.level, "FATAL")
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 1)

    def test_level_change_does_not_add_upsert_or_embed_io(self):
        """Regression guard for the commit-3f0d3f9 I/O contract: promoting the
        level stays in-RAM; the branch still performs exactly one upsert and no
        embedding for each event."""
        self._seed("T_IO", "G0002", "INFO")
        upserts = MagicMock(wraps=self.pipeline.template_registry.upsert)
        self.pipeline.template_registry.upsert = upserts

        for lvl in ("WARN", "ERROR", "INFO"):
            self.pipeline._assign_group(self._event("T_IO", 300.0, lvl))

        self.assertEqual(upserts.call_count, 3)          # one per event, no extras
        self.assertEqual(self.pipeline.embedder.embed_one.call_count, 0)


class TestTrainingLevel(unittest.TestCase):
    """Training derives the template level from the durable event index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_event_index_records_level(self):
        from logai.models import ParsedEvent, RawLog
        from logai.storage.training_event_index import TrainingEventIndex

        idx = TrainingEventIndex(f"{self.tmp}/events.jsonl")
        idx.append_batch([
            ParsedEvent(
                raw=RawLog(
                    timestamp=1.0, service="hdfs", level="ERROR",
                    message="m", event_id="e1",
                ),
                template_id="T00001", template="t", parameters=[],
                is_new_template=True,
            )
        ])
        rec = next(iter(idx.records()))
        self.assertEqual(rec["level"], "ERROR")

    def test_rebuild_keeps_most_severe_and_handles_legacy_records(self):
        from logai.models import ParsedEvent, RawLog
        from logai.storage.training_event_index import TrainingEventIndex
        from logai.training.train_pipeline import TrainingPipeline

        cfg = AppConfig()
        cfg.storage.base_dir = self.tmp
        cfg.storage.model_dir = f"{self.tmp}/models"
        cfg.drain3.persistence_path = f"{self.tmp}/drain3.bin"
        pipeline = TrainingPipeline.__new__(TrainingPipeline)  # skip heavy __init__
        pipeline.config = cfg
        pipeline.template_registry = TemplateRegistry(cfg.storage)
        # A stub miner: id_to_cluster empty -> _generalized_template_text -> None,
        # so the recorded template_text is used.
        pipeline.parser = MagicMock()
        pipeline.parser.miner.drain.id_to_cluster = {}
        pipeline.event_index = TrainingEventIndex(f"{self.tmp}/events.jsonl")

        def ev(tid, level, ts, eid):
            return ParsedEvent(
                raw=RawLog(timestamp=ts, service="hdfs", level=level,
                           message="m", event_id=eid),
                template_id=tid, template="t", parameters=[], is_new_template=False,
            )

        # Same template sees INFO then ERROR then INFO -> must land on ERROR.
        pipeline.event_index.append_batch([
            ev("T00001", "INFO", 1.0, "a"),
            ev("T00001", "error", 2.0, "b"),
            ev("T00001", "INFO", 3.0, "c"),
        ])
        pipeline._rebuild_template_registry()
        self.assertEqual(
            pipeline.template_registry.get("T00001").level, "ERROR"
        )

    def test_rebuild_tolerates_records_without_level(self):
        """A training_event_index.jsonl written by the previous schema (mid-resume)
        has no `level` key; those templates default to INFO without crashing."""
        from logai.storage.training_event_index import TrainingEventIndex
        from logai.training.train_pipeline import TrainingPipeline

        cfg = AppConfig()
        cfg.storage.base_dir = self.tmp
        cfg.storage.model_dir = f"{self.tmp}/models"
        cfg.drain3.persistence_path = f"{self.tmp}/drain3.bin"
        pipeline = TrainingPipeline.__new__(TrainingPipeline)
        pipeline.config = cfg
        pipeline.template_registry = TemplateRegistry(cfg.storage)
        pipeline.parser = MagicMock()
        pipeline.parser.miner.drain.id_to_cluster = {}

        legacy_index = f"{self.tmp}/legacy.jsonl"
        with open(legacy_index, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "event_id": "x", "template_id": "T00007", "template_text": "t",
                "service": "hdfs", "timestamp": 1.0,   # NOTE: no "level"
            }) + "\n")
        pipeline.event_index = TrainingEventIndex(legacy_index)
        pipeline._rebuild_template_registry()
        self.assertEqual(
            pipeline.template_registry.get("T00007").level, DEFAULT_LEVEL
        )


if __name__ == "__main__":
    unittest.main()

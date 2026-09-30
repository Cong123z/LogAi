"""Training must not re-serialise the whole Drain3 tree on every template
change (that made Phase 1 run for hours with a large state); it persists
once per batch through an atomic write instead."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from logai.config import Drain3Config
from logai.models import RawLog
from logai.parsing.drain3_parser import Drain3Parser


def _raw(i: int, message: str) -> RawLog:
    return RawLog(timestamp=1000.0 + i, service="svc", level="INFO", message=message)


class TestDrain3DeferredSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Drain3Config()
        self.config.persistence_path = os.path.join(self.tmp, "drain3_state.bin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse_distinct_templates(self, parser: Drain3Parser, count: int) -> None:
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
        for i in range(count):
            parser.parse(_raw(i, f"{words[i % len(words)]} event kind{chr(97 + i)} happened"))

    def test_autosave_disabled_does_not_write_on_template_change(self):
        parser = Drain3Parser(self.config, autosave=False)
        self._parse_distinct_templates(parser, 5)
        self.assertGreater(parser.cluster_count(), 1)
        self.assertFalse(os.path.exists(self.config.persistence_path))

    def test_explicit_save_is_atomic_and_restorable(self):
        parser = Drain3Parser(self.config, autosave=False)
        self._parse_distinct_templates(parser, 5)
        size = parser.save_state("test batch")

        self.assertGreater(size, 0)
        self.assertEqual(os.path.getsize(self.config.persistence_path), size)
        self.assertFalse(os.path.exists(self.config.persistence_path + ".tmp"))
        # Saving must not re-enable per-change autosave.
        self.assertIsNone(parser.miner.persistence_handler)

        restored = Drain3Parser(self.config, autosave=False)
        self.assertEqual(restored.cluster_count(), parser.cluster_count())
        self.assertEqual(restored.message_count(), 5)

    def test_default_autosave_still_persists_on_change(self):
        parser = Drain3Parser(self.config)
        self._parse_distinct_templates(parser, 2)
        self.assertTrue(os.path.exists(self.config.persistence_path))


if __name__ == "__main__":
    unittest.main()

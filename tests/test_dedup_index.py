"""Unit tests for DedupIndex (Issue 2)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from logai.config import StorageConfig
from logai.storage.dedup import DedupIndex


class TestDedupIndex(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage = StorageConfig(
            base_dir=self.temp_dir,
            dedup_index_file="dedup_index.json",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_bounded_size_eviction(self):
        """When capacity is exceeded, oldest items should be evicted in O(1)."""
        dedup = DedupIndex(self.storage, max_size=5)

        for i in range(5):
            dedup.mark(f"event_{i}")

        self.assertEqual(len(dedup), 5)
        self.assertTrue(dedup.seen("event_0"))
        self.assertTrue(dedup.seen("event_4"))

        # Add 6th item -> event_0 should be evicted
        dedup.mark("event_5")
        self.assertEqual(len(dedup), 5)
        self.assertFalse(dedup.seen("event_0"))
        self.assertTrue(dedup.seen("event_1"))
        self.assertTrue(dedup.seen("event_5"))

    def test_lru_move_to_end(self):
        """Accessing/re-marking an existing key should refresh its LRU order."""
        dedup = DedupIndex(self.storage, max_size=3)

        dedup.mark("e1")
        dedup.mark("e2")
        dedup.mark("e3")

        # Refresh e1 -> now e2 is the oldest
        dedup.mark("e1")

        # Add e4 -> e2 should be evicted, e1 and e3 must remain
        dedup.mark("e4")
        self.assertFalse(dedup.seen("e2"))
        self.assertTrue(dedup.seen("e1"))
        self.assertTrue(dedup.seen("e3"))
        self.assertTrue(dedup.seen("e4"))

    def test_persistence_flush_and_reload(self):
        """Snapshot should be persisted to disk and reloadable upon restart."""
        dedup1 = DedupIndex(self.storage, max_size=10)
        dedup1.mark("ev_alpha")
        dedup1.mark("ev_beta")
        dedup1.flush()

        # Restart simulation: new instance loads file
        dedup2 = DedupIndex(self.storage, max_size=10)
        self.assertEqual(len(dedup2), 2)
        self.assertTrue(dedup2.seen("ev_alpha"))
        self.assertTrue(dedup2.seen("ev_beta"))
        self.assertFalse(dedup2.seen("ev_gamma"))

    def test_legacy_format_compatibility(self):
        """Must seamlessly load legacy JSON dict format {"event_id": timestamp}."""
        legacy_path = Path(self.storage.base_dir) / self.storage.dedup_index_file
        legacy_data = {
            "legacy_1": 1788858000.0,
            "legacy_2": 1788858001.0,
        }
        with open(legacy_path, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        dedup = DedupIndex(self.storage, max_size=10)
        self.assertEqual(len(dedup), 2)
        self.assertTrue(dedup.seen("legacy_1"))
        self.assertTrue(dedup.seen("legacy_2"))

    def test_gc_flushes_and_returns_zero(self):
        """gc() should flush dirty changes and return 0 (no O(D) stall)."""
        dedup = DedupIndex(self.storage, max_size=5)
        dedup.mark("e_dirty")
        self.assertTrue(dedup._dirty)

        removed = dedup.gc()
        self.assertEqual(removed, 0)
        self.assertFalse(dedup._dirty)
        self.assertTrue((Path(self.storage.base_dir) / self.storage.dedup_index_file).exists())


if __name__ == "__main__":
    unittest.main()

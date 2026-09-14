"""Batch persistence tests for AlertStateMachine."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logai.alert.alert_state_machine import AlertStateMachine
from logai.config import AlertConfig
from logai.models import AlertStateEnum, AnomalyResult
from logai.storage.base import JSONStore


class CountingJSONStore(JSONStore):
    def __init__(self, path: Path):
        self.bulk_set_calls = 0
        super().__init__(path)

    def bulk_set(self, mapping):
        self.bulk_set_calls += 1
        super().bulk_set(mapping)


def _result(group_id: str, timestamp: float, score: float) -> AnomalyResult:
    return AnomalyResult(
        group_id=group_id,
        timestamp=timestamp,
        anomaly_score=score,
        anomaly=score >= 0.6,
        count_1m=10,
    )


class TestAlertStateBatch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = AlertConfig()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _store(self, name: str) -> CountingJSONStore:
        return CountingJSONStore(Path(self.temp_dir.name) / name)

    def test_batch_matches_ordered_single_transitions(self):
        results = [
            _result("G1", 1.0, 0.8),
            _result("G2", 2.0, 0.2),
            _result("G1", 3.0, 0.8),
            _result("G1", 4.0, 0.8),
            _result("G1", 5.0, 0.2),
            _result("G1", 6.0, 0.2),
            _result("G1", 7.0, 0.2),
        ]
        single = AlertStateMachine(self.config, self._store("single.json"))
        expected = [single.transition(result) for result in results]

        batch_store = self._store("batch.json")
        batch = AlertStateMachine(self.config, batch_store)
        actual = batch.transition_batch(results)

        self.assertEqual(actual, expected)
        self.assertEqual(batch_store.bulk_set_calls, 1)
        self.assertEqual(
            batch_store.get("G1"),
            actual[-1].__dict__,
        )
        self.assertEqual(actual[3].alert_state, AlertStateEnum.ALERTING.value)
        self.assertEqual(actual[-1].alert_state, AlertStateEnum.NORMAL.value)

    def test_only_final_state_per_group_is_persisted(self):
        store = self._store("final.json")
        machine = AlertStateMachine(self.config, store)
        states = machine.transition_batch([
            _result("G1", 1.0, 0.8),
            _result("G1", 2.0, 0.8),
            _result("G2", 3.0, 0.8),
            _result("G1", 4.0, 0.8),
        ])

        self.assertEqual(store.bulk_set_calls, 1)
        self.assertEqual(set(store.all()), {"G1", "G2"})
        self.assertEqual(store.get("G1"), states[-1].__dict__)
        self.assertEqual(store.get("G2"), states[2].__dict__)
        self.assertIn("G1", machine.groups_not_normal())
        self.assertNotIn("G2", machine.groups_not_normal())

    def test_empty_batch_does_not_write(self):
        store = self._store("empty.json")
        machine = AlertStateMachine(self.config, store)

        self.assertEqual(machine.transition_batch([]), [])
        self.assertEqual(store.bulk_set_calls, 0)


if __name__ == "__main__":
    unittest.main()

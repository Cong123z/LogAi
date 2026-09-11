"""Fix #6: log_alerts_total counts each phase transition INTO ALERTING exactly
once, not once per event while a group stays alerting.

Follows the suite convention of stubbing heavy optional drivers. prometheus_client
is stubbed too, so the real Counter is swapped for a tiny counting fake; the code
under test (`MetricsExporter.set_alert_state`) and its `_last_alert_state`
bookkeeping are exercised for real.
"""
from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from unittest.mock import MagicMock

for mod in ["sentence_transformers", "prometheus_client"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from logai.config import MetricsConfig
from logai.metrics.prometheus_exporter import MetricsExporter
from logai.models import AlertStateEnum, AnomalyState


class _FakeCounter:
    """Minimal prometheus-Counter stand-in that tracks inc() per group_id."""

    def __init__(self):
        self.counts = defaultdict(float)

    def labels(self, group_id):
        parent = self

        class _Child:
            def inc(self, amount: float = 1.0):
                parent.counts[group_id] += amount

        return _Child()


def _state(group_id: str, alert_state: AlertStateEnum) -> AnomalyState:
    return AnomalyState(group_id=group_id, timestamp=0.0, alert_state=alert_state.value)


class TestLogAlertsTotal(unittest.TestCase):
    def setUp(self):
        self.exporter = MetricsExporter(MetricsConfig())
        # Swap the mocked Counter for a real-counting fake; keep the rest mocked.
        self.fake = _FakeCounter()
        self.exporter.log_alerts_total = self.fake

    def _count(self, group_id: str) -> float:
        return self.fake.counts[group_id]

    def test_increments_once_per_escalation(self):
        seq = [
            AlertStateEnum.NORMAL,
            AlertStateEnum.WARMING,
            AlertStateEnum.ALERTING,
            AlertStateEnum.ALERTING,  # staying alerting must NOT re-count
            AlertStateEnum.ALERTING,
        ]
        for st in seq:
            self.exporter.set_alert_state(_state("G0001", st))
        self.assertEqual(self._count("G0001"), 1.0)

    def test_reincrements_after_recovery(self):
        cycle = [
            AlertStateEnum.ALERTING,   # +1
            AlertStateEnum.COOLING,
            AlertStateEnum.NORMAL,
            AlertStateEnum.WARMING,
            AlertStateEnum.ALERTING,   # +1 (fresh escalation)
        ]
        for st in cycle:
            self.exporter.set_alert_state(_state("G0002", st))
        self.assertEqual(self._count("G0002"), 2.0)

    def test_cooling_back_to_alerting_counts(self):
        # ALERTING -> COOLING -> ALERTING (score climbs again) is a new alert.
        for st in [AlertStateEnum.ALERTING, AlertStateEnum.COOLING, AlertStateEnum.ALERTING]:
            self.exporter.set_alert_state(_state("G0003", st))
        self.assertEqual(self._count("G0003"), 2.0)

    def test_counter_is_per_group(self):
        self.exporter.set_alert_state(_state("A", AlertStateEnum.ALERTING))
        self.exporter.set_alert_state(_state("B", AlertStateEnum.ALERTING))
        self.exporter.set_alert_state(_state("A", AlertStateEnum.ALERTING))
        self.assertEqual(self._count("A"), 1.0)
        self.assertEqual(self._count("B"), 1.0)

    def test_never_alerting_stays_zero(self):
        for st in [AlertStateEnum.NORMAL, AlertStateEnum.WARMING, AlertStateEnum.NORMAL]:
            self.exporter.set_alert_state(_state("G0005", st))
        self.assertEqual(self._count("G0005"), 0.0)


if __name__ == "__main__":
    unittest.main()

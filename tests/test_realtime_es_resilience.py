"""Tests for graceful ES-unavailable handling in RealtimePipeline.run_forever
(Fix 3 + Fix 4b).

Verifies that when collector.poll_batch() raises (ES down / non-retryable
error surfaced after the collector exhausts its own retries):
- run_forever does NOT crash — it logs, sleeps with backoff, and retries
- backoff grows exponentially from poll_interval and caps at 300s
- a successful poll resets the consecutive-failure counter
- logai_es_poll_errors_total is incremented per failure
- the checkpoint is never advanced on a poll failure

The pipeline is built via __new__ so we don't construct the heavy real
collaborators (embedder, clusterer, models). Only what run_forever touches is
attached. The otherwise-infinite loop is broken by making time.sleep raise a
sentinel once enough iterations have been observed.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

if "elasticsearch" not in sys.modules:
    sys.modules["elasticsearch"] = MagicMock()

# Stub heavy optional deps that realtime_pipeline pulls in transitively but
# which run_forever does not exercise (metrics exporter and embedder are
# replaced with mocks on the __new__-built pipeline below).
for _mod in ("prometheus_client", "sentence_transformers"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from logai.realtime import realtime_pipeline
from logai.realtime.realtime_pipeline import RealtimePipeline


class _StopLoop(Exception):
    """Sentinel raised from a patched time.sleep to exit run_forever."""


class RunForeverResilienceTest(unittest.TestCase):
    def _make_pipeline(self, poll_interval: float = 5.0) -> RealtimePipeline:
        p = RealtimePipeline.__new__(RealtimePipeline)

        config = MagicMock()
        config.elasticsearch.poll_interval_seconds = poll_interval
        config.alert.idle_eval_seconds = 5.0
        p.config = config

        p.collector = MagicMock()
        p.metrics = MagicMock()
        p.template_registry = MagicMock()
        p.group_registry = MagicMock()
        p.dedup = MagicMock()
        p.checkpoint = MagicMock()
        p._process_one = MagicMock(return_value=True)
        # Idle-tick state. Anchoring the event-clock wall to "now" keeps
        # _now_event_time() ~0, so the throttle keeps the idle tick dormant and
        # this ES-resilience test stays focused on the poll/backoff path.
        p._event_clock = 0.0
        p._event_clock_wall = realtime_pipeline.time.monotonic()
        p._last_idle_tick = 0.0
        # Micro-batch predict buffer state (TODO #7). run_forever's flush
        # decision (Phase 4) reads these every cycle; without them the loop
        # AttributeErrors before it can reach the poll/backoff path under test.
        # They stay empty here because poll_batch never yields a batch, so the
        # flush branch is never taken and this test stays focused on backoff.
        p._pending_predictions = []
        p._pending_cursor = None
        p._pending_last_ts = None
        p._buffer_started_at = None
        # start_metrics_server just needs to be a no-op here.
        p.start_metrics_server = MagicMock()
        return p

    def _run_capturing_sleeps(self, pipeline, stop_after: int):
        """Run run_forever, capturing sleep durations; stop after N sleeps."""
        sleeps: list[float] = []

        def fake_sleep(duration):
            sleeps.append(duration)
            if len(sleeps) >= stop_after:
                raise _StopLoop
        with patch.object(realtime_pipeline.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(_StopLoop):
                pipeline.run_forever()
        return sleeps

    # ── #14: ES unavailable does not crash ───────────────────────────────
    def test_run_forever_es_unavailable_no_crash(self):
        p = self._make_pipeline()
        p.collector.poll_batch.side_effect = ConnectionError("ES down")
        sleeps = self._run_capturing_sleeps(p, stop_after=1)
        # It caught the error, slept, and would have retried (no propagation
        # of ConnectionError — only our sentinel escapes).
        self.assertEqual(len(sleeps), 1)
        p.metrics.logai_es_poll_errors_total.inc.assert_called()

    # ── #15: backoff increases exponentially ─────────────────────────────
    def test_run_forever_backoff_increases(self):
        p = self._make_pipeline(poll_interval=5.0)
        p.collector.poll_batch.side_effect = ConnectionError("down")
        sleeps = self._run_capturing_sleeps(p, stop_after=3)
        # 5*2^1, 5*2^2, 5*2^3
        self.assertEqual(sleeps, [10.0, 20.0, 40.0])

    # ── #16: recovery resets backoff ─────────────────────────────────────
    def test_run_forever_recovery_resets_backoff(self):
        p = self._make_pipeline(poll_interval=5.0)
        # fail x3, then an empty successful poll, then fail again.
        p.collector.poll_batch.side_effect = [
            ConnectionError("down"),
            ConnectionError("down"),
            ConnectionError("down"),
            ([], None),          # success -> consecutive resets to 0
            ConnectionError("down"),
        ]
        sleeps = self._run_capturing_sleeps(p, stop_after=5)
        # 10,20,40 (fails), 5 (empty-batch poll_interval), 10 (reset -> 5*2^1)
        self.assertEqual(sleeps, [10.0, 20.0, 40.0, 5.0, 10.0])

    # ── #17: backoff caps at 300s ────────────────────────────────────────
    def test_run_forever_backoff_caps_at_max(self):
        p = self._make_pipeline(poll_interval=5.0)
        p.collector.poll_batch.side_effect = ConnectionError("down")
        sleeps = self._run_capturing_sleeps(p, stop_after=8)
        # 10,20,40,80,160, then capped at 300.
        self.assertEqual(sleeps, [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0])
        self.assertTrue(all(s <= 300.0 for s in sleeps))

    # ── #18: poll error metric incremented per failure ───────────────────
    def test_poll_error_metric_incremented(self):
        p = self._make_pipeline()
        p.collector.poll_batch.side_effect = ConnectionError("down")
        self._run_capturing_sleeps(p, stop_after=2)
        self.assertEqual(p.metrics.logai_es_poll_errors_total.inc.call_count, 2)

    # ── #19: checkpoint not advanced on poll failure ─────────────────────
    def test_checkpoint_not_advanced_on_poll_failure(self):
        p = self._make_pipeline()
        p.collector.poll_batch.side_effect = ConnectionError("down")
        self._run_capturing_sleeps(p, stop_after=3)
        p.checkpoint.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

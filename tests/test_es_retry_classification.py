"""Tests for ES error classification in ElasticsearchCollector._search (Fix 2).

Verifies that:
- Non-retryable client errors (400/401/403/404) fail fast with no retry/sleep
- Retryable errors (connection/timeout, generic transport) retry up to 5 times
- The instance-level _on_retry_hook fires once per retry (feeds logai_retry_total)

`_search` reads the module-global `_ES_NON_RETRYABLE` at call time, so tests
patch that tuple with local sentinel exception classes rather than depending on
the real elasticsearch driver (which is mocked out at import).
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

# Lightweight mock for elasticsearch driver (matches test_es_malformed_hits).
if "elasticsearch" not in sys.modules:
    sys.modules["elasticsearch"] = MagicMock()

from logai.collector import es_collector
from logai.collector.es_collector import ElasticsearchCollector
from logai.config import ElasticsearchConfig
from logai.storage.checkpoint import CheckpointStore


# ── Sentinel exception classes standing in for the ES driver hierarchy ────

class BadRequestError(Exception):        # 400
    pass


class AuthenticationException(Exception):  # 401
    pass


class AuthorizationException(Exception):   # 403
    pass


class NotFoundError(Exception):            # 404
    pass


class ConnectionError_(Exception):         # transient transport error
    pass


class ConnectionTimeout(Exception):        # transient timeout
    pass


_TEST_NON_RETRYABLE = (
    BadRequestError,
    AuthenticationException,
    AuthorizationException,
    NotFoundError,
)


class _RetryTestBase(unittest.TestCase):
    def setUp(self):
        self.config = ElasticsearchConfig()
        self.checkpoint = MagicMock(spec=CheckpointStore)
        self.collector = ElasticsearchCollector(self.config, self.checkpoint)

        # Patch the module-global classification tuple and neutralise real
        # sleeping so retry tests run instantly.
        self._nr_patch = patch.object(
            es_collector, "_ES_NON_RETRYABLE", _TEST_NON_RETRYABLE
        )
        self._sleep_patch = patch.object(es_collector.time, "sleep")
        self._nr_patch.start()
        self.mock_sleep = self._sleep_patch.start()
        self.addCleanup(self._nr_patch.stop)
        self.addCleanup(self._sleep_patch.stop)

    def _client_raising(self, exc: Exception) -> MagicMock:
        client = MagicMock()
        client.search.side_effect = exc
        self.collector.client = client
        return client


# ── Non-retryable: fail fast, no sleep ───────────────────────────────────

class TestNonRetryable(_RetryTestBase):
    def _assert_fail_fast(self, exc_cls):
        client = self._client_raising(exc_cls("boom"))
        with self.assertRaises(exc_cls):
            self.collector._search({"query": {}})
        # Exactly one attempt, zero sleeps.
        self.assertEqual(client.search.call_count, 1)
        self.assertEqual(self.mock_sleep.call_count, 0)

    def test_400_bad_request_no_retry(self):
        self._assert_fail_fast(BadRequestError)

    def test_401_auth_no_retry(self):
        self._assert_fail_fast(AuthenticationException)

    def test_403_forbidden_no_retry(self):
        self._assert_fail_fast(AuthorizationException)

    def test_404_not_found_no_retry(self):
        self._assert_fail_fast(NotFoundError)


# ── Retryable: retry up to 5 times then re-raise ─────────────────────────

class TestRetryable(_RetryTestBase):
    def _assert_retries_then_raises(self, exc_cls):
        client = self._client_raising(exc_cls("transient"))
        with self.assertRaises(exc_cls):
            self.collector._search({"query": {}})
        # 1 initial + 5 retries = 6 attempts, 5 sleeps.
        self.assertEqual(client.search.call_count, 6)
        self.assertEqual(self.mock_sleep.call_count, 5)

    def test_connection_error_retries(self):
        self._assert_retries_then_raises(ConnectionError_)

    def test_timeout_retries(self):
        self._assert_retries_then_raises(ConnectionTimeout)

    def test_recovers_after_transient_failures(self):
        """A retryable error that clears before the budget is exhausted
        returns the successful response."""
        client = MagicMock()
        client.search.side_effect = [
            ConnectionError_("down"),
            ConnectionError_("down"),
            {"hits": {"hits": []}},
        ]
        self.collector.client = client
        result = self.collector._search({"query": {}})
        self.assertEqual(result, {"hits": {"hits": []}})
        self.assertEqual(client.search.call_count, 3)
        self.assertEqual(self.mock_sleep.call_count, 2)


# ── on_retry hook / logai_retry_total wiring ─────────────────────────────

class TestOnRetryHook(_RetryTestBase):
    def test_hook_fires_once_per_retry(self):
        client = MagicMock()
        client.search.side_effect = [
            ConnectionError_("down"),
            ConnectionError_("down"),
            ConnectionError_("down"),
            {"hits": {"hits": []}},
        ]
        self.collector.client = client
        hook = MagicMock()
        self.collector._on_retry_hook = hook

        self.collector._search({"query": {}})

        # 3 retries -> 3 hook invocations, each with (attempt, exc).
        self.assertEqual(hook.call_count, 3)
        attempts = [c.args[0] for c in hook.call_args_list]
        self.assertEqual(attempts, [1, 2, 3])

    def test_hook_not_called_on_non_retryable(self):
        self._client_raising(BadRequestError("bad"))
        hook = MagicMock()
        self.collector._on_retry_hook = hook
        with self.assertRaises(BadRequestError):
            self.collector._search({"query": {}})
        self.assertEqual(hook.call_count, 0)

    def test_missing_hook_is_safe(self):
        """No hook wired (default None) must not raise."""
        client = MagicMock()
        client.search.side_effect = [ConnectionError_("x"), {"ok": True}]
        self.collector.client = client
        self.collector._on_retry_hook = None
        result = self.collector._search({"query": {}})
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()

"""Tests for O(1) template metrics tracking, edge cases, and performance benchmarks."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure lightweight mocks for external libraries not installed in minimal environment
for mod in [
    "numpy",
    "sklearn",
    "sklearn.ensemble",
    "hdbscan",
    "elasticsearch",
    "sentence_transformers",
    "drain3",
    "drain3.template_miner",
    "drain3.file_persistence",
    "drain3.template_miner_config",
    "drain3.masking",
    "prometheus_client",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from logai.config import StorageConfig
from logai.models import TemplateState, RawLog, ParsedEvent, GroupState
from logai.storage.registries import TemplateRegistry


class TestTemplateRegistryCounting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_cfg = StorageConfig(
            base_dir=self.temp_dir,
            template_registry_file="templates.json",
            template_embeddings_file="template_embeddings.pkl",
        )
        self.registry = TemplateRegistry(self.storage_cfg)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_counts_empty(self):
        self.assertEqual(self.registry.count_by_service("auth"), 0)
        self.assertEqual(self.registry.count_by_service("unknown"), 0)
        self.assertEqual(self.registry.total_count(), 0)
        self.assertEqual(self.registry.all_counts_by_service(), {})

    def test_upsert_new_template_increments_count(self):
        t1 = TemplateState(
            template_id="t1",
            template_text="User <*> logged in",
            service="auth",
            first_seen=100.0,
            last_seen=100.0,
            event_count=1,
        )
        is_new = self.registry.upsert(t1)
        self.assertTrue(is_new)
        self.assertEqual(self.registry.count_by_service("auth"), 1)
        self.assertEqual(self.registry.count_by_service("payment"), 0)
        self.assertEqual(self.registry.total_count(), 1)

    def test_upsert_existing_template_does_not_increment_count(self):
        t1 = TemplateState(
            template_id="t1",
            template_text="User <*> logged in",
            service="auth",
            first_seen=100.0,
            last_seen=100.0,
            event_count=1,
        )
        self.assertTrue(self.registry.upsert(t1))
        self.assertEqual(self.registry.count_by_service("auth"), 1)

        # Update event_count and last_seen
        t1.event_count = 50
        t1.last_seen = 200.0
        is_new = self.registry.upsert(t1)
        self.assertFalse(is_new)
        self.assertEqual(self.registry.count_by_service("auth"), 1)
        self.assertEqual(self.registry.total_count(), 1)

    def test_multiple_templates_and_services(self):
        templates = [
            TemplateState(template_id="t1", template_text="t1", service="auth"),
            TemplateState(template_id="t2", template_text="t2", service="auth"),
            TemplateState(template_id="t3", template_text="t3", service="payment"),
            TemplateState(template_id="t4", template_text="t4", service="payment"),
            TemplateState(template_id="t5", template_text="t5", service="payment"),
            TemplateState(template_id="t6", template_text="t6", service="orders"),
            TemplateState(template_id="t7", template_text="t7", service=""),
        ]
        for t in templates:
            self.registry.upsert(t)

        self.assertEqual(self.registry.count_by_service("auth"), 2)
        self.assertEqual(self.registry.count_by_service("payment"), 3)
        self.assertEqual(self.registry.count_by_service("orders"), 1)
        self.assertEqual(self.registry.count_by_service("unknown"), 1)
        self.assertEqual(self.registry.count_by_service(""), 1)
        self.assertEqual(self.registry.total_count(), 7)
        self.assertEqual(
            self.registry.all_counts_by_service(),
            {"auth": 2, "payment": 3, "orders": 1, "unknown": 1},
        )

    def test_reload_from_disk_restores_counts(self):
        self.registry.upsert(TemplateState(template_id="t1", template_text="t1", service="auth"))
        self.registry.upsert(TemplateState(template_id="t2", template_text="t2", service="auth"))
        self.registry.upsert(TemplateState(template_id="t3", template_text="t3", service="billing"))
        self.registry.flush()

        reloaded = TemplateRegistry(self.storage_cfg)
        self.assertEqual(reloaded.count_by_service("auth"), 2)
        self.assertEqual(reloaded.count_by_service("billing"), 1)
        self.assertEqual(reloaded.count_by_service("orders"), 0)
        self.assertEqual(reloaded.total_count(), 3)
        self.assertEqual(reloaded.all_counts_by_service(), {"auth": 2, "billing": 1})

    def test_service_change_updates_counts(self):
        t1 = TemplateState(template_id="t1", template_text="t1", service="auth")
        self.registry.upsert(t1)
        self.assertEqual(self.registry.count_by_service("auth"), 1)
        self.assertEqual(self.registry.count_by_service("iam"), 0)

        # Change service to iam
        t1.service = "iam"
        is_new = self.registry.upsert(t1)
        self.assertFalse(is_new)
        self.assertEqual(self.registry.count_by_service("auth"), 0)
        self.assertEqual(self.registry.count_by_service("iam"), 1)
        self.assertEqual(self.registry.total_count(), 1)

    def test_concurrent_upserts_thread_safety(self):
        threads = []
        templates_per_service = 25

        def worker(service_name: str, offset: int):
            for i in range(templates_per_service):
                tid = f"{service_name}_{offset}_{i}"
                state = TemplateState(template_id=tid, template_text=f"txt_{tid}", service=service_name)
                self.registry.upsert(state, flush=False)

        for s in ["svc_a", "svc_b", "svc_c"]:
            for worker_id in range(2):
                t = threading.Thread(target=worker, args=(s, worker_id))
                threads.append(t)
                t.start()

        for t in threads:
            t.join()

        self.assertEqual(self.registry.count_by_service("svc_a"), 50)
        self.assertEqual(self.registry.count_by_service("svc_b"), 50)
        self.assertEqual(self.registry.count_by_service("svc_c"), 50)
        self.assertEqual(self.registry.total_count(), 150)


class TestTemplateRegistryEdgeCases(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_cfg = StorageConfig(
            base_dir=self.temp_dir,
            template_registry_file="templates.json",
            template_embeddings_file="template_embeddings.pkl",
        )
        self.registry = TemplateRegistry(self.storage_cfg)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_whitespace_and_none_service_normalization(self):
        """Service names with surrounding whitespace or empty/None values should normalize cleanly."""
        t1 = TemplateState(template_id="t1", template_text="txt1", service="  auth  ")
        self.registry.upsert(t1)
        # Both padded and stripped queries resolve to normalized key
        self.assertEqual(self.registry.count_by_service("auth"), 1)
        self.assertEqual(self.registry.count_by_service("  auth  "), 1)

        t2 = TemplateState(template_id="t2", template_text="txt2", service="")
        self.registry.upsert(t2)
        self.assertEqual(self.registry.count_by_service("unknown"), 1)
        self.assertEqual(self.registry.count_by_service(""), 1)
        self.assertEqual(self.registry.count_by_service("   "), 1)

        # Modifying t2 from empty string to whitespace-only doesn't trigger spurious service changes
        t2.service = "    "
        is_new = self.registry.upsert(t2)
        self.assertFalse(is_new)
        self.assertEqual(self.registry.count_by_service("unknown"), 1)

    def test_delete_template_decrements_and_prunes_empty_services(self):
        """Deleting a template should decrement its service count and prune empty keys."""
        t1 = TemplateState(template_id="t1", template_text="txt1", service="payment")
        t2 = TemplateState(template_id="t2", template_text="txt2", service="payment")
        self.registry.upsert(t1)
        self.registry.upsert(t2)
        self.assertEqual(self.registry.count_by_service("payment"), 2)

        # Delete first template
        deleted = self.registry.delete("t1")
        self.assertTrue(deleted)
        self.assertEqual(self.registry.count_by_service("payment"), 1)
        self.assertEqual(self.registry.total_count(), 1)

        # Delete second template -> service key should be pruned
        self.assertTrue(self.registry.delete("t2"))
        self.assertEqual(self.registry.count_by_service("payment"), 0)
        self.assertEqual(self.registry.total_count(), 0)
        self.assertNotIn("payment", self.registry.all_counts_by_service())

        # Deleting non-existent template returns False and changes nothing
        self.assertFalse(self.registry.delete("non_existent"))
        self.assertEqual(self.registry.total_count(), 0)

    def test_all_counts_immutability(self):
        """External caller mutating returned all_counts_by_service() dict must not corrupt internal registry state."""
        self.registry.upsert(TemplateState(template_id="t1", template_text="txt", service="auth"))
        counts = self.registry.all_counts_by_service()
        counts["auth"] = 999
        counts["fake_service"] = 123

        # Internal state remains intact
        self.assertEqual(self.registry.count_by_service("auth"), 1)
        self.assertEqual(self.registry.count_by_service("fake_service"), 0)
        self.assertEqual(self.registry.all_counts_by_service(), {"auth": 1})

    def test_corrupt_file_graceful_recovery(self):
        """If templates.json is corrupted, registry gracefully starts empty without crashing."""
        reg_file = Path(self.temp_dir) / "templates.json"
        reg_file.write_text("{corrupt: true, invalid json....", encoding="utf-8")

        recovered = TemplateRegistry(self.storage_cfg)
        self.assertEqual(recovered.total_count(), 0)
        self.assertEqual(recovered.count_by_service("any"), 0)

    def test_high_concurrency_race_condition_same_template_id(self):
        """Multiple threads trying to upsert the exact same template ID simultaneously
        should result in count = 1, not N.
        """
        threads = []
        template_id = "shared_template_race"

        def race_worker():
            state = TemplateState(
                template_id=template_id,
                template_text="shared race text",
                service="auth",
            )
            self.registry.upsert(state, flush=False)

        for _ in range(30):
            t = threading.Thread(target=race_worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(self.registry.count_by_service("auth"), 1)
        self.assertEqual(self.registry.total_count(), 1)


class TestRealtimePipelineEdgeCases(unittest.TestCase):
    def test_pipeline_does_not_scan_all_templates_on_events(self):
        """Verify that processing events does not invoke all_templates() in O(T),
        and template metrics are only updated when a new template is registered.
        """
        with patch("logai.realtime.realtime_pipeline.DocumentationMatcher"):
            from logai.config import AppConfig
            from logai.realtime.realtime_pipeline import RealtimePipeline

            temp_dir = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.storage.base_dir = temp_dir

                initial_reg = TemplateRegistry(cfg.storage)
                for i in range(5):
                    initial_reg.upsert(
                        TemplateState(
                            template_id=f"t_{i}",
                            template_text=f"text {i}",
                            service="checkout",
                            group_id="G_CHECKOUT",
                        )
                    )
                initial_reg.flush()

                with patch.object(RealtimePipeline, "_update_template_metrics") as mock_update:
                    pipeline = RealtimePipeline(cfg)
                    pipeline.metrics.app_log_templates_total.labels.assert_any_call(service="checkout")

                    pipeline.template_registry.all_templates = MagicMock(
                        side_effect=AssertionError("all_templates() should NOT be called on hot path!")
                    )

                    # 1. Known template
                    raw1 = RawLog(
                        event_id="e1", timestamp=1000.0, service="checkout", level="INFO", message="text 0"
                    )
                    pipeline.parser.parse = MagicMock(
                        return_value=ParsedEvent(
                            raw=raw1, template="text 0", template_id="t_0", parameters=[], is_new_template=False
                        )
                    )
                    mock_update.reset_mock()
                    pipeline._process_one(raw1)
                    mock_update.assert_not_called()
                    pipeline.template_registry.all_templates.assert_not_called()

                    # 2. New template
                    raw2 = RawLog(
                        event_id="e2", timestamp=1001.0, service="checkout", level="INFO", message="new text"
                    )
                    pipeline.parser.parse = MagicMock(
                        return_value=ParsedEvent(
                            raw=raw2, template="new text", template_id="t_new", parameters=[], is_new_template=True
                        )
                    )
                    pipeline.embedder.embed_one = MagicMock(return_value=[0.1, 0.2])
                    pipeline.group_registry.all_centroids = MagicMock(return_value={})
                    pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=("G_CHECKOUT", 0.95))

                    pipeline._process_one(raw2)
                    mock_update.assert_called_once_with("checkout")
                    pipeline.template_registry.all_templates.assert_not_called()

                self.assertEqual(pipeline.template_registry.count_by_service("checkout"), 6)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_pending_to_grouped_template_lifecycle_no_double_counting(self):
        """Verify lifecycle of a template transitioning:
        Unknown/Pending -> Repeated Pending events -> Clustered to Group -> Known event.
        The template metric must be incremented EXACTLY ONCE across all stages.
        """
        with patch("logai.realtime.realtime_pipeline.DocumentationMatcher"):
            from logai.config import AppConfig
            from logai.realtime.realtime_pipeline import RealtimePipeline

            temp_dir = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.storage.base_dir = temp_dir

                with patch.object(RealtimePipeline, "_update_template_metrics") as mock_update:
                    pipeline = RealtimePipeline(cfg)
                    pipeline.template_registry.all_templates = MagicMock(
                        side_effect=AssertionError("all_templates() should NOT be called!")
                    )

                    pipeline.embedder.embed_one = MagicMock(return_value=[0.1, 0.2])
                    pipeline.group_registry.all_centroids = MagicMock(return_value={})

                    # Stage 1: Brand new template, no cluster match (similarity below threshold) -> Pending
                    pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=(None, 0.2))
                    raw1 = RawLog(event_id="e1", timestamp=100.0, service="orders", level="INFO", message="pending msg")
                    pipeline.parser.parse = MagicMock(
                        return_value=ParsedEvent(raw=raw1, template="pending msg", template_id="t_pend", parameters=[], is_new_template=True)
                    )
                    mock_update.reset_mock()
                    pipeline._process_one(raw1)

                    mock_update.assert_called_once_with("orders")
                    self.assertEqual(pipeline.template_registry.count_by_service("orders"), 1)

                    # Stage 2: Second event with same template (still pending)
                    raw2 = RawLog(event_id="e2", timestamp=101.0, service="orders", level="INFO", message="pending msg")
                    pipeline.parser.parse = MagicMock(
                        return_value=ParsedEvent(raw=raw2, template="pending msg", template_id="t_pend", parameters=[], is_new_template=False)
                    )
                    mock_update.reset_mock()
                    pipeline._process_one(raw2)

                    # MUST NOT be called again
                    mock_update.assert_not_called()
                    self.assertEqual(pipeline.template_registry.count_by_service("orders"), 1)

                    # Stage 3: Centroid match succeeds (e.g. after training), template assigned group G_ORDERS
                    pipeline.clusterer.assign_to_nearest_group = MagicMock(return_value=("G_ORDERS", 0.9))
                    raw3 = RawLog(event_id="e3", timestamp=102.0, service="orders", level="INFO", message="pending msg")
                    pipeline.parser.parse = MagicMock(
                        return_value=ParsedEvent(raw=raw3, template="pending msg", template_id="t_pend", parameters=[], is_new_template=False)
                    )
                    mock_update.reset_mock()
                    pipeline._process_one(raw3)

                    # MUST NOT be called again
                    mock_update.assert_not_called()
                    self.assertEqual(pipeline.template_registry.count_by_service("orders"), 1)

                    # Stage 4: Now template has group_id -> hits fast path directly
                    raw4 = RawLog(event_id="e4", timestamp=103.0, service="orders", level="INFO", message="pending msg")
                    pipeline.parser.parse = MagicMock(
                        return_value=ParsedEvent(raw=raw4, template="pending msg", template_id="t_pend", parameters=[], is_new_template=False)
                    )
                    mock_update.reset_mock()
                    pipeline._process_one(raw4)

                    mock_update.assert_not_called()
                    self.assertEqual(pipeline.template_registry.count_by_service("orders"), 1)

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)


class TestTemplateMetricsPerformance(unittest.TestCase):
    def test_performance_benchmark_old_vs_new(self):
        """Benchmark Old O(T) scan vs New O(1) query under realistic template cardinality (T=2,000)."""
        temp_dir = tempfile.mkdtemp()
        try:
            storage_cfg = StorageConfig(
                base_dir=temp_dir,
                template_registry_file="templates.json",
                template_embeddings_file="embeddings.pkl",
            )
            registry = TemplateRegistry(storage_cfg)

            # Seed 2,000 templates across 10 services
            n_templates = 2000
            services = [f"service_{i}" for i in range(10)]
            for i in range(n_templates):
                svc = services[i % 10]
                registry.upsert(
                    TemplateState(template_id=f"t_{i}", template_text=f"Template pattern {i}", service=svc),
                    flush=False,
                )
            registry.flush()

            n_events = 2000

            # 1. Measure Old Approach (O(T) per event)
            t0 = time.perf_counter()
            for i in range(n_events):
                svc = services[i % 10]
                _ = sum(1 for t in registry.all_templates() if t.service == svc)
            time_old = time.perf_counter() - t0

            # 2. Measure New Approach (O(1) per event)
            t0 = time.perf_counter()
            for i in range(n_events):
                svc = services[i % 10]
                _ = registry.count_by_service(svc)
            time_new = time.perf_counter() - t0

            speedup = time_old / max(time_new, 1e-9)
            latency_old_us = (time_old / n_events) * 1e6
            latency_new_us = (time_new / n_events) * 1e6

            # Performance assertions
            self.assertGreater(speedup, 50.0, f"Speedup was only {speedup:.1f}x, expected > 50x")
            self.assertLess(latency_new_us, 10.0, f"New latency {latency_new_us:.2f} µs was too slow")

            print(
                f"\n[BENCHMARK] Events: {n_events} | Templates: {n_templates}\n"
                f"  - Old O(T) approach: {time_old:.4f}s ({latency_old_us:.2f} µs/event)\n"
                f"  - New O(1) approach: {time_new:.4f}s ({latency_new_us:.2f} µs/event)\n"
                f"  - Speedup: {speedup:.1f}x faster!"
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

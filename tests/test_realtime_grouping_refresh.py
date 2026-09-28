from __future__ import annotations

import sys
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np

for module_name in (
    "hdbscan",
    "elasticsearch",
    "drain3",
    "drain3.template_miner",
    "drain3.file_persistence",
    "drain3.template_miner_config",
    "drain3.masking",
    "prometheus_client",
):
    if module_name not in sys.modules:
        sys.modules[module_name] = MagicMock()

from logai.config import AppConfig
from logai.models import FeatureVector, GroupState, ParsedEvent, RawLog, TemplateState
from logai.realtime.realtime_pipeline import RealtimePipeline


def _pipeline(tmp: str) -> RealtimePipeline:
    config = AppConfig()
    config.storage.base_dir = tmp
    config.storage.model_dir = f"{tmp}/models"
    config.doc_matcher.corpus_path = f"{tmp}/documentation_corpus.json"
    with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
        pipeline = RealtimePipeline(config)
    pipeline.metrics = MagicMock()
    pipeline.documentation_worker = MagicMock()
    pipeline.template_registry.upsert(
        TemplateState("T1", "one", "api", event_count=2, group_id="GA")
    )
    pipeline.template_registry.upsert(
        TemplateState("T2", "two", "api", event_count=5, group_id="GB")
    )
    pipeline.template_registry.set_embedding("T1", np.array([1.0, 0.0]))
    pipeline.template_registry.set_embedding("T2", np.array([0.0, 1.0]))
    pipeline.group_registry.replace_all(
        [
            GroupState("GA", template_ids=["T1"], event_count=2),
            GroupState("GB", template_ids=["T2"], event_count=5),
        ],
        {"GA": np.array([1.0, 0.0]), "GB": np.array([0.0, 1.0])},
    )
    return pipeline


def test_refresh_flushes_old_buffer_then_first_new_event_uses_target_group():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = _pipeline(tmp)
        snapshot = pipeline.grouping_store.load_overrides()
        pipeline.grouping_store.replace_assignment(
            "T1", "anchor", "T2", snapshot["revision"]
        )
        pipeline._pending_predictions = [
            (("api", "GA"), FeatureVector(("api", "GA"), 1.0))
        ]
        original_apply = pipeline.grouping_manager.apply_realtime
        order = []

        def flush_old():
            order.append("flush")
            pipeline._pending_predictions = []

        def apply(snapshot):
            order.append("apply")
            return original_apply(snapshot)

        pipeline._flush_batch = MagicMock(side_effect=flush_old)
        pipeline.grouping_manager.apply_realtime = MagicMock(side_effect=apply)
        assert pipeline._refresh_grouping_if_needed() is True
        assert order == ["flush", "apply"]

        raw = RawLog(2.0, "api", "INFO", "one", event_id="E1")
        parsed = ParsedEvent(raw, "T1", "one", [], False)
        grouped = pipeline._assign_group(parsed)
        assert grouped.group_id == "GB"
        assert pipeline.group_registry.get("GA") is None
        assert pipeline.grouping_store.load_status()["state"] == "applied"


def test_registry_write_failure_is_visible_and_does_not_claim_applied():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = _pipeline(tmp)
        snapshot = pipeline.grouping_store.load_overrides()
        changed = pipeline.grouping_store.replace_assignment(
            "T1", "anchor", "T2", snapshot["revision"]
        )
        # First flush fails; the second call is the manager's rollback flush.
        with patch.object(
            pipeline.template_registry,
            "flush",
            side_effect=[OSError("disk full"), None],
        ):
            assert pipeline._refresh_grouping_if_needed() is False
        status = pipeline.grouping_store.load_status()
        assert status["attempted_revision"] == changed["revision"]
        assert status["applied_revision"] != changed["revision"]
        assert status["state"] == "failed"
        assert status["error"]["reason_code"] == "registry_write_failed"

"""Realtime pipeline (plan section 4):

  Collect -> Parse -> Assign(Known/Unknown) -> Match(doc) -> Aggregate
  (features) -> Predict (anomaly) -> Alert State -> Export (Prometheus)

No retraining / re-clustering happens here - unresolved ("Unknown/Pending")
templates just wait for the next training run.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from logai.config import AppConfig
from logai.alert.alert_state_machine import AlertStateMachine
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel, GroupAnomalyModels
from logai.clustering.hdbscan_cluster import GroupClusterer
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.embedding.embedder import TemplateEmbedder
from logai.features.feature_engine import FeatureEngine
from logai.metrics.prometheus_exporter import MetricsExporter
from logai.models import FeatureVector, GroupedEvent, GroupState, ParsedEvent, RawLog, TemplateState
from logai.parsing.drain3_parser import Drain3Parser
from logai.reliability.dlq import DeadLetterQueue
from logai.storage.base import JSONStore, ModelStore
from logai.storage.checkpoint import CheckpointStore
from logai.storage.dedup import DedupIndex
from logai.storage.registries import GroupRegistry, TemplateRegistry

logger = logging.getLogger("logai.realtime")

PENDING_GROUP_ID = "UNASSIGNED_PENDING"


class RealtimePipeline:
    def __init__(self, config: AppConfig):
        self.config = config

        self.checkpoint = CheckpointStore(config.storage)
        self.collector = ElasticsearchCollector(config.elasticsearch, self.checkpoint)
        self.parser = Drain3Parser(config.drain3)
        self.embedder = TemplateEmbedder(config.embedding)
        self.clusterer = GroupClusterer(config.clustering)

        self.template_registry = TemplateRegistry(config.storage)
        self.group_registry = GroupRegistry(config.storage)

        self.doc_matcher = DocumentationMatcher(
            config.doc_matcher, self.embedder,
            f"{config.storage.base_dir}/{config.storage.doc_embeddings_file}",
        )
        self.feature_engine = FeatureEngine(config.features)
        self.model_store = ModelStore(config.storage.model_dir)
        self.anomaly_model = GlobalAnomalyModel(config.anomaly, self.model_store)
        self.anomaly_models = self.anomaly_model  # backward compatibility alias

        alert_state_store = JSONStore(
            f"{config.storage.base_dir}/{config.storage.anomaly_state_file}"
        )
        self.alert_sm = AlertStateMachine(config.alert, alert_state_store)
        self.dedup = DedupIndex(
            config.storage,
            config.reliability.dedup_ttl_seconds,
            config.reliability.dedup_max_size,
        )
        self.dlq = DeadLetterQueue(
            f"{config.storage.base_dir}/{config.storage.dlq_file}"
        )
        self.metrics = MetricsExporter(config.metrics)

        self._doc_refresh_at = 0.0
        self._template_counts: dict[str, int] = {}

    def start_metrics_server(self) -> None:
        self.metrics.start()
        logger.info(
            "Prometheus metrics available on %s:%d/metrics",
            self.config.metrics.http_host, self.config.metrics.http_port,
        )

    def run_forever(self) -> None:
        self.start_metrics_server()
        logger.info("Realtime pipeline started, polling Elasticsearch...")
        for batch in self.collector.run_forever():
            self.metrics.logai_queue_depth.set(len(batch))
            for raw in batch:
                self.metrics.logai_events_received_total.inc()
                self._process_one(raw)
            self.metrics.logai_queue_depth.set(0)
            self.dedup.gc()

    def _process_one(self, raw: RawLog) -> None:
        start = time.time()
        try:
            if self.dedup.seen(raw.event_id):
                return  # idempotency: already processed this event_id

            parsed = self.parser.parse(raw)
            grouped = self._assign_group(parsed)
            self.metrics.record_raw_event(parsed)
            self._update_template_metrics(parsed.raw.service)

            if grouped is not None:
                self._match_documentation_if_stale(grouped.group_id)
                fv = self.feature_engine.update(grouped.group_id, raw.timestamp)
                self._run_anomaly_and_alert(grouped.group_id, fv)

            self.dedup.mark(raw.event_id)
            self.metrics.logai_events_processed_total.inc()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process event %s: %s", raw.event_id, exc)
            self.metrics.logai_events_failed_total.inc()
            self.dlq.push(raw.to_dict(), str(exc))
        finally:
            self.metrics.logai_processing_latency_seconds.observe(time.time() - start)

    def _assign_group(self, parsed: ParsedEvent) -> Optional[GroupedEvent]:
        existing_template = self.template_registry.get(parsed.template_id)

        if existing_template and existing_template.group_id:
            # Known Template -> direct group mapping (plan 4.3), no embedding needed.
            existing_template.last_seen = parsed.raw.timestamp
            existing_template.event_count += 1
            self.template_registry.upsert(existing_template, flush=False)
            self._touch_group(existing_template.group_id, parsed.raw.timestamp)
            return GroupedEvent(parsed=parsed, group_id=existing_template.group_id)

        # Unknown Template (new to drain3, or known but not yet grouped) -> embed & assign.
        embedding = self.embedder.embed_one(parsed.template)
        centroids = self.group_registry.all_centroids()
        group_id, similarity = self.clusterer.assign_to_nearest_group(embedding, centroids)

        state = existing_template or TemplateState(
            template_id=parsed.template_id,
            template_text=parsed.template,
            service=parsed.raw.service,
            first_seen=parsed.raw.timestamp,
            last_seen=parsed.raw.timestamp,
            event_count=0,
        )
        state.last_seen = parsed.raw.timestamp
        state.event_count += 1
        self.template_registry.set_embedding(parsed.template_id, embedding)

        if group_id is None:
            # Doesn't clear the similarity threshold against any existing
            # group -> leave ungrouped/pending; a future training run will
            # cluster it properly (plan 4.4).
            state.group_id = None
            self.template_registry.upsert(state, flush=False)
            logger.info(
                "Template %s is Unknown/Pending (best similarity=%.2f) - "
                "awaiting next training run to form/join a group.",
                parsed.template_id, similarity,
            )
            return None

        state.group_id = group_id
        self.template_registry.upsert(state, flush=False)
        self._touch_group(group_id, parsed.raw.timestamp, new_template_id=parsed.template_id)
        return GroupedEvent(parsed=parsed, group_id=group_id, group_similarity=similarity)

    def _touch_group(
        self, group_id: str, timestamp: float, new_template_id: Optional[str] = None
    ) -> None:
        group = self.group_registry.get(group_id)
        if group is None:
            group = GroupState(group_id=group_id, first_seen=timestamp, last_seen=timestamp)
        group.last_seen = timestamp
        group.event_count += 1
        if new_template_id and new_template_id not in group.template_ids:
            group.template_ids.append(new_template_id)
        self.group_registry.upsert(group, flush=False)

    def _match_documentation_if_stale(self, group_id: str) -> None:
        now = time.time()
        if now - self._doc_refresh_at > self.config.doc_matcher.refresh_interval_seconds:
            self.doc_matcher.reload()
            self._doc_refresh_at = now

        group = self.group_registry.get(group_id)
        centroid = self.group_registry.get_centroid(group_id)
        if group is None or centroid is None:
            return
        match = self.doc_matcher.match(group_id, centroid)
        group.documented = match.documented
        group.documentation_id = match.documentation_id
        group.confidence = match.similarity
        if match.error_code:
            group.error_code = match.error_code
        self.group_registry.upsert(group, flush=False)

    def _run_anomaly_and_alert(self, group_id: str, feature_vector: FeatureVector) -> None:
        result = self.anomaly_model.predict(feature_vector)
        if result is None:
            return  # global model not yet trained - wait for initial training run
        group = self.group_registry.get(group_id) or GroupState(group_id=group_id)
        self.metrics.set_anomaly_score(group, result.anomaly_score)
        state = self.alert_sm.transition(result)
        self.metrics.set_alert_state(state)

    def _update_template_metrics(self, service: str) -> None:
        # NOTE: MVP implementation recomputes the per-service template count
        # on every event by scanning the full registry. Fine for the small
        # template cardinality expected at MVP scale; if this becomes a
        # bottleneck, maintain an incremental Counter[service] instead.
        count = sum(
            1 for t in self.template_registry.all_templates() if t.service == service
        )
        self.metrics.set_template_count(service, count)

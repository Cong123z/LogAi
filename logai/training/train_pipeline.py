"""Training pipeline (plan section 3): builds all artifacts consumed by the
realtime pipeline from historical logs.

  Historical Logs -> Drain3 -> Template Registry -> Embedding -> HDBSCAN ->
  Group Centroid -> Documentation Matcher -> Feature Generation ->
  Isolation Forest Training -> Model & Registry
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import numpy as np

from logai.config import AppConfig
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.clustering.hdbscan_cluster import GroupClusterer, NOISE_LABEL
from logai.embedding.embedder import TemplateEmbedder
from logai.features.feature_engine import FeatureEngine
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel, GroupAnomalyModels
from logai.models import FeatureVector, GroupState, ParsedEvent, RawLog, TemplateState
from logai.parsing.drain3_parser import Drain3Parser
from logai.storage.base import ModelStore
from logai.storage.checkpoint import CheckpointStore
from logai.storage.dedup import LocalTrainingDedup
from logai.storage.registries import GroupRegistry, TemplateRegistry
from logai.storage.training_event_index import TrainingEventIndex

logger = logging.getLogger("logai.training")


class TrainingPipeline:
    def __init__(self, config: AppConfig):
        self.config = config
        self.parser = Drain3Parser(config.drain3)
        self.embedder = TemplateEmbedder(config.embedding)
        self.clusterer = GroupClusterer(config.clustering)
        self.template_registry = TemplateRegistry(config.storage)
        self.group_registry = GroupRegistry(config.storage)
        self.feature_engine = FeatureEngine(config.features)
        self.model_store = ModelStore(config.storage.model_dir)
        self.anomaly_model = GlobalAnomalyModel(config.anomaly, self.model_store)
        self.anomaly_models = self.anomaly_model  # backward compatibility alias
        self.doc_matcher = DocumentationMatcher(
            config.doc_matcher, self.embedder,
            f"{config.storage.base_dir}/{config.storage.doc_embeddings_file}",
        )
        self.event_index = TrainingEventIndex(
            f"{config.storage.base_dir}/{config.storage.training_event_index_file}"
        )

    def run(
        self,
        historical_logs: Union[
            List[RawLog],
            Iterable[List[RawLog]],
            Iterable[Tuple[List[RawLog], Optional[List[Any]]]],
        ],
        checkpoint_store: Optional[CheckpointStore] = None,
        dedup_buffer_size: Optional[int] = None,
    ) -> None:
        logger.info("Training pipeline started...")
        # Phase 1: Stream parse batches into Drain3 with LocalTrainingDedup & Checkpoint
        if checkpoint_store is None or checkpoint_store.get_search_after() is None:
            self.event_index.clear()

        self._stream_parse(
            historical_logs,
            checkpoint_store=checkpoint_store,
            dedup_buffer_size=dedup_buffer_size,
        )
        self._rebuild_template_registry()
        self._embed_templates()
        template_to_group = self._cluster_templates()
        self._build_group_registry(template_to_group)
        self._compute_centroids()
        self._match_documentation()
        grouped_by_group = self._group_events(template_to_group)
        self._train_anomaly_models(grouped_by_group)
        self._flush_all()
        if checkpoint_store is not None:
            checkpoint_store.clear()
        self.event_index.clear()
        logger.info(
            "Training pipeline complete: %d templates, %d groups",
            len(self.template_registry.all_templates()),
            len(self.group_registry.all_groups()),
        )

    # -- steps -----------------------------------------------------------

    def _stream_parse(
        self,
        historical_logs: Union[
            List[RawLog],
            Iterable[List[RawLog]],
            Iterable[Tuple[List[RawLog], Optional[List[Any]]]],
        ],
        checkpoint_store: Optional[CheckpointStore] = None,
        dedup_buffer_size: Optional[int] = None,
    ) -> None:
        dedup_size = (
            dedup_buffer_size
            if dedup_buffer_size is not None
            else getattr(self.config.training, "dedup_buffer_size", 10_000)
        )
        dedup_buffer = LocalTrainingDedup(max_size=dedup_size)
        durable_event_ids: Set[str] = self.event_index.event_ids()

        if isinstance(historical_logs, list) and historical_logs and isinstance(historical_logs[0], RawLog):
            logger.info(
                "Training pipeline Phase 1: streaming %d in-memory historical logs",
                len(historical_logs),
            )
            sorted_logs = sorted(historical_logs, key=lambda r: r.timestamp)
            batch_size = getattr(self.config.training, "batch_size", 2000)
            batch_stream: Iterable[Any] = (
                (sorted_logs[i : i + batch_size], None)
                for i in range(0, len(sorted_logs), batch_size)
            )
        elif isinstance(historical_logs, list) and not historical_logs:
            batch_stream = []
        else:
            logger.info("Training pipeline Phase 1: streaming batches from generator")
            batch_stream = historical_logs

        total_batches = 0
        parsed_count = 0

        for item in batch_stream:
            if isinstance(item, tuple) and len(item) == 2:
                batch, cursor = item
            else:
                batch, cursor = item, None

            if not batch:
                continue

            total_batches += 1
            parsed_batch: List[ParsedEvent] = []
            for raw in batch:
                # 1. Deduplicate network retries / duplicate logs in RAM
                if raw.event_id in durable_event_ids or dedup_buffer.is_duplicate(raw.event_id):
                    continue

                # 2. Parse raw log with Drain3 immediately
                pe = self.parser.parse(raw)
                parsed_batch.append(pe)
                durable_event_ids.add(raw.event_id)

            # Persist the batch before advancing the cursor.  A restart can
            # therefore replay all historical events, not only new pages.
            self.event_index.append_batch(parsed_batch)
            parsed_count += len(parsed_batch)

            # Advance isolated training checkpoint only after durable index.
            if checkpoint_store is not None and cursor is not None:
                checkpoint_store.set_search_after(cursor, flush=True)

        logger.info(
            "Phase 1 complete: parsed %d events across %d batches (dropped %d duplicates, %d templates)",
            parsed_count,
            total_batches,
            dedup_buffer.duplicates_dropped,
            len({record.get("template_id") for record in self.event_index.records()}),
        )

    def _rebuild_template_registry(self) -> None:
        """Rebuild template aggregates from the durable event index."""
        states: Dict[str, TemplateState] = {}
        seen_event_ids: Set[str] = set()
        for record in self.event_index.records():
            event_id = record.get("event_id")
            template_id = record.get("template_id")
            timestamp = record.get("timestamp")
            if (
                not isinstance(event_id, str)
                or event_id in seen_event_ids
                or not isinstance(template_id, str)
                or not isinstance(timestamp, (int, float))
            ):
                continue
            seen_event_ids.add(event_id)
            state = states.get(template_id)
            if state is None:
                state = TemplateState(
                    template_id=template_id,
                    template_text=str(record.get("template_text") or ""),
                    service=str(record.get("service") or "unknown"),
                    first_seen=float(timestamp),
                    last_seen=float(timestamp),
                )
                states[template_id] = state
            state.first_seen = min(state.first_seen, float(timestamp))
            state.last_seen = max(state.last_seen, float(timestamp))
            state.event_count += 1
        self.template_registry.replace_all(list(states.values()))

    def _parse_all(self, raw_logs: List[RawLog]) -> List[ParsedEvent]:
        raw_logs_sorted = sorted(raw_logs, key=lambda r: r.timestamp)
        return [self.parser.parse(r) for r in raw_logs_sorted]

    def _build_template_registry(self, parsed_events: List[ParsedEvent]) -> None:
        for pe in parsed_events:
            existing = self.template_registry.get(pe.template_id)
            if existing is None:
                state = TemplateState(
                    template_id=pe.template_id,
                    template_text=pe.template,
                    service=pe.raw.service,
                    first_seen=pe.raw.timestamp,
                    last_seen=pe.raw.timestamp,
                    event_count=1,
                )
            else:
                existing.last_seen = max(existing.last_seen, pe.raw.timestamp)
                existing.first_seen = min(existing.first_seen, pe.raw.timestamp)
                existing.event_count += 1
                existing.template_text = pe.template
                state = existing
            self.template_registry.upsert(state, flush=False)
        self.template_registry.flush()

    def _embed_templates(self) -> None:
        templates = self.template_registry.all_templates()
        ids = [t.template_id for t in templates]
        texts = [t.template_text for t in templates]
        embeddings = self.embedder.embed(texts)
        for tid, emb in zip(ids, embeddings):
            self.template_registry.set_embedding(tid, emb)

    def _cluster_templates(self) -> Dict[str, str]:
        embeddings_map = self.template_registry.all_embeddings()
        ids = list(embeddings_map.keys())
        if not ids:
            return {}
        matrix = np.array([embeddings_map[i] for i in ids])
        raw_labels = self.clusterer.cluster(ids, matrix)

        template_to_group: Dict[str, str] = {}
        next_singleton = 0
        for tid, label in raw_labels.items():
            if label == NOISE_LABEL:
                group_id = f"G_SINGLE_{next_singleton:04d}"
                next_singleton += 1
            else:
                group_id = f"G{label:04d}"
            template_to_group[tid] = group_id
            self.template_registry.set_group(tid, group_id)
        return template_to_group

    def _build_group_registry(self, template_to_group: Dict[str, str]) -> None:
        """Build group metadata from template aggregates.

        TemplateRegistry already contains the event counts and time bounds for
        every template, so rebuilding an event-to-group index here would add a
        full O(N) pass and duplicate event references unnecessarily.
        """
        group_templates: Dict[str, List[str]] = defaultdict(list)
        for tid, gid in template_to_group.items():
            group_templates[gid].append(tid)

        for gid, tids in group_templates.items():
            templates = [self.template_registry.get(tid) for tid in tids]
            templates = [template for template in templates if template is not None]
            if not templates:
                logger.warning(
                    "Skipping group %s because none of its templates exist in the registry",
                    gid,
                )
                continue

            representative = templates[0]
            state = GroupState(
                group_id=gid,
                service=representative.service or "unknown",
                template_ids=tids,
                representative_template=representative.template_text,
                first_seen=min(template.first_seen for template in templates),
                last_seen=max(template.last_seen for template in templates),
                event_count=sum(template.event_count for template in templates),
            )
            self.group_registry.upsert(state, flush=False)
        self.group_registry.flush()

    def _compute_centroids(self) -> None:
        embeddings_map = self.template_registry.all_embeddings()
        for group in self.group_registry.all_groups():
            vecs = [embeddings_map[t] for t in group.template_ids if t in embeddings_map]
            if not vecs:
                continue
            centroid = self.clusterer.compute_centroid(np.array(vecs))
            self.group_registry.set_centroid(group.group_id, centroid)

    def _match_documentation(self) -> None:
        centroids = self.group_registry.all_centroids()
        matches = self.doc_matcher.match_all(centroids)
        for gid, match in matches.items():
            group = self.group_registry.get(gid)
            if not group:
                continue
            group.documented = match.documented
            group.documentation_id = match.documentation_id
            group.confidence = match.similarity
            group.error_code = match.error_code or group.error_code
            self.group_registry.upsert(group, flush=False)
        self.group_registry.flush()

    def _group_events(self, template_to_group: Dict[str, str]) -> Dict[str, List[float]]:
        result: Dict[str, List[float]] = defaultdict(list)
        records = sorted(self.event_index.records(), key=lambda r: float(r["timestamp"]))
        for record in records:
            gid = template_to_group.get(str(record.get("template_id")))
            if gid:
                result[gid].append(float(record["timestamp"]))
        return result

    def _train_anomaly_models(self, grouped: Dict[str, List[float]]) -> None:
        all_feature_vectors: List[FeatureVector] = []
        for gid, timestamps in grouped.items():
            engine = FeatureEngine(self.config.features)
            for timestamp in timestamps:
                fv = engine.update(gid, timestamp)
                all_feature_vectors.append(fv)

        logger.info(
            "Collected %d total feature vectors across %d groups for global model training",
            len(all_feature_vectors),
            len(grouped),
        )
        trained = self.anomaly_model.train(all_feature_vectors)
        if not trained:
            logger.warning(
                "Total feature vectors (%d) < min_training_samples (%d) - global IF model was not fitted.",
                len(all_feature_vectors),
                self.config.anomaly.min_training_samples,
            )

    def _flush_all(self) -> None:
        self.template_registry.flush()
        self.group_registry.flush()


def run_training_from_elasticsearch(
    config: AppConfig,
    lookback_seconds: Optional[float] = None,
    max_docs: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> None:
    lookback = (
        lookback_seconds
        if lookback_seconds is not None
        else getattr(config.training, "lookback_seconds", 7 * 24 * 3600)
    )
    total_docs = (
        max_docs
        if max_docs is not None
        else getattr(config.training, "max_docs", 200_000)
    )
    chunk_size = (
        batch_size
        if batch_size is not None
        else getattr(config.training, "batch_size", 2000)
    )

    checkpoint_file = getattr(
        config.storage, "training_checkpoint_file", "training_checkpoint.json"
    )
    training_checkpoint = CheckpointStore(config.storage, checkpoint_file=checkpoint_file)

    collector = ElasticsearchCollector(config.elasticsearch, training_checkpoint)
    end_ts = time.time()
    start_ts = end_ts - lookback

    initial_search_after = training_checkpoint.get_search_after()
    if initial_search_after:
        logger.info(
            "Resuming training pipeline from saved search_after cursor: %s",
            initial_search_after,
        )

    logger.info(
        "Streaming historical logs from ES (lookback=%.1fs, max_docs=%d, batch_size=%d)...",
        lookback,
        total_docs,
        chunk_size,
    )
    batch_stream = collector.stream_historical_batches(
        start_ts=start_ts,
        end_ts=end_ts,
        max_docs=total_docs,
        batch_size=chunk_size,
        initial_search_after=initial_search_after,
    )
    pipeline = TrainingPipeline(config)
    pipeline.run(batch_stream, checkpoint_store=training_checkpoint)

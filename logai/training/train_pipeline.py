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
from typing import Dict, List

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
from logai.storage.registries import GroupRegistry, TemplateRegistry

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

    def run(self, historical_logs: List[RawLog]) -> None:
        logger.info("Training pipeline started with %d historical logs", len(historical_logs))
        parsed_events = self._parse_all(historical_logs)
        self._build_template_registry(parsed_events)
        self._embed_templates()
        template_to_group = self._cluster_templates()
        self._build_group_registry(template_to_group)
        self._compute_centroids()
        self._match_documentation()
        grouped_by_group = self._group_events(parsed_events, template_to_group)
        self._train_anomaly_models(grouped_by_group)
        self._flush_all()
        logger.info("Training pipeline complete: %d templates, %d groups",
                    len(self.template_registry.all_templates()),
                    len(self.group_registry.all_groups()))

    # -- steps -----------------------------------------------------------

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

    def _group_events(
        self, parsed_events: List[ParsedEvent], template_to_group: Dict[str, str]
    ) -> Dict[str, List[ParsedEvent]]:
        result: Dict[str, List[ParsedEvent]] = defaultdict(list)
        for pe in sorted(parsed_events, key=lambda e: e.raw.timestamp):
            gid = template_to_group.get(pe.template_id)
            if gid:
                result[gid].append(pe)
        return result

    def _train_anomaly_models(self, grouped: Dict[str, List[ParsedEvent]]) -> None:
        all_feature_vectors: List[FeatureVector] = []
        for gid, events in grouped.items():
            engine = FeatureEngine(self.config.features)
            for e in events:
                fv = engine.update(gid, e.raw.timestamp)
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
    config: AppConfig, lookback_seconds: float = 7 * 24 * 3600
) -> None:
    checkpoint = CheckpointStore(config.storage)  # separate from realtime checkpoint file ideally
    collector = ElasticsearchCollector(config.elasticsearch, checkpoint)
    end_ts = time.time()
    start_ts = end_ts - lookback_seconds
    logger.info("Fetching historical logs from ES for training window...")
    historical_logs = collector.fetch_historical_range(start_ts, end_ts)
    pipeline = TrainingPipeline(config)
    pipeline.run(historical_logs)

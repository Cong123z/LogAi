"""Training pipeline (plan section 3): builds all artifacts consumed by the
realtime pipeline from historical logs.

  Historical Logs -> Drain3 -> Template Registry -> Embedding -> HDBSCAN ->
  Group Centroid -> Documentation Matcher -> Feature Generation ->
  Isolation Forest Training -> Model & Registry
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import numpy as np

from logai.config import AppConfig
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.docmatch.refresh_worker import DocumentationRefreshWorker
from logai.clustering.hdbscan_cluster import GroupClusterer, NOISE_LABEL
from logai.embedding.embedder import TemplateEmbedder
from logai.features.feature_engine import FeatureEngine
from logai.grouping.assignment_manager import GroupAssignmentManager
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel, GroupAnomalyModels
from logai.models import (
    DEFAULT_LEVEL,
    LEVEL_RANK,
    FeatureVector,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.parsing.drain3_parser import Drain3Parser, display_template
from logai.storage.base import ModelStore
from logai.storage.checkpoint import CheckpointStore
from logai.storage.dedup import LocalTrainingDedup
from logai.storage.documentation import DocumentationCorpusStore
from logai.storage.grouping import GroupingOverrideStore, GroupingStoreError
from logai.storage.index_selection import IndexSelectionStore
from logai.storage.registries import GroupRegistry, TemplateRegistry
from logai.storage.training_event_index import TrainingEventIndex

logger = logging.getLogger("logai.training")


class TrainingPipeline:
    def __init__(self, config: AppConfig):
        self.config = config
        # Bulk parsing: persist Drain3 once per batch (see _stream_parse)
        # instead of re-serialising the whole tree on every template change.
        self.parser = Drain3Parser(config.drain3, autosave=False)
        self.embedder = TemplateEmbedder(config.embedding)
        self.clusterer = GroupClusterer(config.clustering)
        self.template_registry = TemplateRegistry(config.storage)
        self.group_registry = GroupRegistry(config.storage)
        self.grouping_store = GroupingOverrideStore.from_config(config)
        self.grouping_manager = GroupAssignmentManager(
            self.template_registry, self.group_registry, self.embedder.embed_one
        )
        self._active_grouping_snapshot: Optional[Dict[str, Any]] = None
        self.feature_engine = FeatureEngine(config.features)
        self.model_store = ModelStore(config.storage.model_dir)
        self.anomaly_model = GlobalAnomalyModel(config.anomaly, self.model_store)
        self.anomaly_models = self.anomaly_model  # backward compatibility alias
        self.documentation_store = DocumentationCorpusStore.from_config(config)
        config.doc_matcher.corpus_path = str(self.documentation_store.corpus_path)
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
        try:
            self._run_impl(
                historical_logs,
                checkpoint_store=checkpoint_store,
                dedup_buffer_size=dedup_buffer_size,
            )
        except Exception as exc:
            if self._active_grouping_snapshot is not None:
                previous = {}
                try:
                    previous = self.grouping_store.load_status()
                except GroupingStoreError:
                    pass
                try:
                    self.grouping_store.write_status({
                        "applied_revision": previous.get("applied_revision"),
                        "attempted_revision": self._active_grouping_snapshot["revision"],
                        "last_attempt_at": time.time(),
                        "last_applied_at": previous.get("last_applied_at"),
                        "last_heartbeat_at": time.time(),
                        "state": "failed",
                        "error": {
                            "reason_code": "engine_exception",
                            "message": str(exc)[:500],
                            "retryable": False,
                        },
                        "results": {},
                        "unresolved": {},
                    })
                except Exception:  # noqa: BLE001
                    logger.exception("Unable to persist failed grouping training status")
            raise

    def _run_impl(
        self,
        historical_logs: Union[
            List[RawLog],
            Iterable[List[RawLog]],
            Iterable[Tuple[List[RawLog], Optional[List[Any]]]],
        ],
        checkpoint_store: Optional[CheckpointStore] = None,
        dedup_buffer_size: Optional[int] = None,
    ) -> None:
        t_start_total = time.time()
        logger.info("=== Training pipeline started ===")
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
        try:
            self._active_grouping_snapshot = self.grouping_store.load_overrides()
        except GroupingStoreError as exc:
            now = time.time()
            self.grouping_store.write_status({
                "applied_revision": None,
                "attempted_revision": None,
                "last_attempt_at": now,
                "last_applied_at": None,
                "last_heartbeat_at": now,
                "state": "failed",
                "error": {
                    "reason_code": "invalid_override_schema",
                    "message": str(exc)[:500],
                    "retryable": False,
                },
                "results": {},
                "unresolved": {},
            })
            raise
        template_to_group = self._cluster_templates()
        logger.info("Phase 5: Applying grouping overrides and building groups + centroids...")
        t_phase5 = time.time()
        template_states = {
            state.template_id: state for state in self.template_registry.all_templates()
        }
        resolution = self.grouping_manager.resolve(
            template_to_group,
            template_states,
            self._active_grouping_snapshot,
        )
        template_to_group = {
            template_id: group_id
            for template_id, group_id in resolution.effective_mapping.items()
            if group_id is not None
        }
        for template_id, group_id in template_to_group.items():
            self.template_registry.set_group(template_id, group_id, flush=False)
        groups, centroids, _deleted = self.grouping_manager.build_groups(
            resolution.effective_mapping,
            template_states,
            self.template_registry.all_embeddings(),
            {group.group_id: group for group in self.group_registry.all_groups()},
        )
        results = resolution.results.values()
        logger.info(
            "Phase 5 complete in %.2fs: Built %d groups with centroids (%d singleton) "
            "from %d templates; overrides applied=%d unresolved=%d; %d templates without a group",
            time.time() - t_phase5,
            len(groups),
            sum(1 for group_id in groups if group_id.startswith("G_SINGLE_")),
            len(template_states),
            sum(1 for result in results if result.get("state") == "applied"),
            sum(1 for result in results if result.get("state") == "unresolved"),
            len(template_states) - len(template_to_group),
        )
        grouped_by_group = self._group_events(template_to_group)
        self._train_anomaly_models(grouped_by_group)

        # Publish the complete effective membership only after feature/model
        # generation succeeds. A failed training run therefore never reports
        # the grouping revision as applied.
        logger.info("Phase 8: Publishing template registry, groups and centroids...")
        t_phase8 = time.time()
        self.template_registry.flush()
        self.group_registry.replace_all(groups, centroids)
        logger.info(
            "Phase 8 complete in %.2fs: Published %d templates, %d groups, %d centroids",
            time.time() - t_phase8,
            len(template_states),
            len(groups),
            len(centroids),
        )
        self._match_documentation()
        DocumentationRefreshWorker(
            self.documentation_store,
            self.doc_matcher,
            self.group_registry,
            self.template_registry,
            self.config.doc_matcher.refresh_interval_seconds,
        ).refresh_once()

        logger.info("Phase 10: Persisting all registries and cleaning temporary indexes...")
        t_phase10 = time.time()
        self._flush_all()
        self._write_training_grouping_status(resolution)
        if checkpoint_store is not None:
            checkpoint_store.clear()
        self.event_index.clear()
        logger.info("Phase 10 complete in %.2fs", time.time() - t_phase10)
        logger.info(
            "=== Training pipeline complete in %.2fs: %d templates, %d groups successfully saved ===",
            time.time() - t_start_total,
            len(self.template_registry.all_templates()),
            len(self.group_registry.all_groups()),
        )

    def _write_training_grouping_status(self, resolution: Any) -> None:
        snapshot = self._active_grouping_snapshot
        if snapshot is None:
            return
        previous = {}
        try:
            previous = self.grouping_store.load_status()
        except GroupingStoreError:
            pass
        results = resolution.results
        has_nonretryable = any(
            result.get("state") == "unresolved" and not result.get("retryable", False)
            for result in results.values()
        )
        applied = sum(result.get("state") == "applied" for result in results.values())
        unresolved = sum(result.get("state") == "unresolved" for result in results.values())
        if has_nonretryable:
            state = "failed"
        elif applied and unresolved:
            state = "partial"
        elif unresolved:
            state = "pending"
        else:
            state = "applied"
        first_error = next(
            (
                result for result in results.values()
                if result.get("state") == "unresolved" and not result.get("retryable", False)
            ),
            None,
        )
        now = time.time()
        self.grouping_store.write_status({
            "applied_revision": (
                snapshot["revision"] if state == "applied" else previous.get("applied_revision")
            ),
            "attempted_revision": snapshot["revision"],
            "last_attempt_at": now,
            "last_applied_at": now if state == "applied" else previous.get("last_applied_at"),
            "last_heartbeat_at": now,
            "state": state,
            "error": (
                {
                    "reason_code": first_error.get("reason_code"),
                    "message": first_error.get("message", "Grouping assignment failed"),
                    "retryable": False,
                }
                if first_error else None
            ),
            "results": results,
            "unresolved": resolution.unresolved,
        })

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
        logger.info(
            "Phase 1 start: Drain3 state %s (%s) restored with %d clusters / %d messages; "
            "event index has %d events from a previous interrupted run",
            self.parser.persistence_path,
            _format_size(_file_size(self.parser.persistence_path)),
            self.parser.cluster_count(),
            self.parser.message_count(),
            len(durable_event_ids),
        )

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
        fetched_count = 0
        state_bytes = 0
        t_phase1 = time.time()
        t_wait = time.time()

        for item in batch_stream:
            # Time spent inside the generator = Elasticsearch fetch latency.
            fetch_seconds = time.time() - t_wait
            if isinstance(item, tuple) and len(item) == 2:
                batch, cursor = item
            else:
                batch, cursor = item, None

            if not batch:
                t_wait = time.time()
                continue

            total_batches += 1
            fetched_count += len(batch)
            t_batch = time.time()
            parsed_batch: List[ParsedEvent] = []
            for raw in batch:
                # 1. Deduplicate network retries / duplicate logs in RAM
                if raw.event_id in durable_event_ids or dedup_buffer.is_duplicate(raw.event_id):
                    continue

                # 2. Parse raw log with Drain3 immediately
                pe = self.parser.parse(raw)
                parsed_batch.append(pe)
                durable_event_ids.add(raw.event_id)
            parse_seconds = time.time() - t_batch

            # Commit order per batch: Drain3 state -> event index -> cursor.
            # The state goes first because event-index records reference
            # Drain3 cluster ids: if the index were durable but the state not,
            # a resumed miner could re-issue those ids for different templates.
            # A crash after the state save only replays this batch into Drain3
            # (slightly inflated cluster sizes), never loses a template.
            save_seconds = 0.0
            if parsed_batch:
                t_save = time.time()
                state_bytes = self.parser.save_state(f"training batch {total_batches}")
                save_seconds = time.time() - t_save
            self.event_index.append_batch(parsed_batch)
            parsed_count += len(parsed_batch)

            # Advance isolated training checkpoint only after durable index.
            if checkpoint_store is not None and cursor is not None:
                checkpoint_store.set_search_after(cursor, flush=True)

            elapsed = time.time() - t_phase1
            last_ts = max(raw.timestamp for raw in batch)
            logger.info(
                "Phase 1 batch %d: fetched=%d parsed=%d skipped_dup=%d | total fetched=%d parsed=%d | "
                "last_event=%s | clusters=%d drain3_messages=%d state=%s event_index=%s | "
                "fetch=%.2fs parse=%.2fs save=%.2fs | elapsed=%.0fs rate=%.0f ev/s",
                total_batches,
                len(batch),
                len(parsed_batch),
                len(batch) - len(parsed_batch),
                fetched_count,
                parsed_count,
                _format_ts(last_ts),
                self.parser.cluster_count(),
                self.parser.message_count(),
                _format_size(state_bytes),
                _format_size(_file_size(self.event_index.path)),
                fetch_seconds,
                parse_seconds,
                save_seconds,
                elapsed,
                fetched_count / elapsed if elapsed > 0 else 0.0,
            )
            t_wait = time.time()

        logger.info(
            "Phase 1 complete in %.2fs: Parsed %d events across %d batches (dropped %d duplicates); "
            "Drain3 has %d clusters, state %s",
            time.time() - t_phase1,
            parsed_count,
            total_batches,
            dedup_buffer.duplicates_dropped,
            self.parser.cluster_count(),
            _format_size(_file_size(self.parser.persistence_path)),
        )

    def _rebuild_template_registry(self) -> None:
        """Rebuild template aggregates from the durable event index."""
        logger.info("Phase 2: Rebuilding template registry from event index...")
        t_phase2 = time.time()
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
            # Normalise once per record; the same value feeds both the initial
            # construction below and the promotion at the end of the iteration.
            event_level = str(record.get("level") or DEFAULT_LEVEL).strip().upper()
            state = states.get(template_id)
            if state is None:
                generalized = self._generalized_template_text(template_id)
                template_text = (
                    generalized
                    if generalized is not None
                    else str(record.get("template_text") or "")
                )
                state = TemplateState(
                    template_id=template_id,
                    template_text=template_text,
                    service=str(record.get("service") or "unknown"),
                    level=event_level,
                    first_seen=float(timestamp),
                    last_seen=float(timestamp),
                )
                states[template_id] = state
            state.first_seen = min(state.first_seen, float(timestamp))
            state.last_seen = max(state.last_seen, float(timestamp))
            state.event_count += 1
            # Keep the most severe level ever seen for this template (monotonic:
            # a later low-severity event must never downgrade an ERROR template).
            if LEVEL_RANK.get(event_level, LEVEL_RANK[DEFAULT_LEVEL]) > LEVEL_RANK.get(
                state.level, LEVEL_RANK[DEFAULT_LEVEL]
            ):
                state.level = event_level
        self.template_registry.replace_all(list(states.values()))
        logger.info(
            "Phase 2 complete in %.2fs: Rebuilt %d unique templates",
            time.time() - t_phase2,
            len(states),
        )

    def _generalized_template_text(self, template_id: str) -> Optional[str]:
        """Return Drain3's current generalised template for ``template_id``.

        ``template_id`` has the form ``T{cluster_id:05d}`` (e.g. ``T00001``).
        Returns ``None`` when the id cannot be mapped to a live Drain3 cluster
        (non-numeric id from a mocked parser, missing cluster, or any miner API
        difference) so the caller can fall back to the recorded text.
        """
        try:
            cluster_id = int(template_id[1:])
            cluster = self.parser.miner.drain.id_to_cluster.get(cluster_id)
            if cluster is None:
                return None
            template = cluster.get_template()
            return display_template(template) if isinstance(template, str) else None
        except (ValueError, TypeError, AttributeError):
            return None

    def _embed_templates(self) -> None:
        templates = self.template_registry.all_templates()
        logger.info("Phase 3: Generating embeddings for %d templates...", len(templates))
        t_phase3 = time.time()
        ids = [t.template_id for t in templates]
        texts = [t.template_text for t in templates]
        embeddings = self.embedder.embed(texts)
        for tid, emb in zip(ids, embeddings):
            self.template_registry.set_embedding(tid, emb, flush=False)
        self.template_registry.flush()
        logger.info(
            "Phase 3 complete in %.2fs: Generated embeddings for %d templates",
            time.time() - t_phase3,
            len(templates),
        )

    def _cluster_templates(self) -> Dict[str, str]:
        embeddings_map = self.template_registry.all_embeddings()
        ids = list(embeddings_map.keys())
        if not ids:
            logger.info("Phase 4: No template embeddings to cluster")
            return {}
        logger.info("Phase 4: Clustering %d templates using HDBSCAN...", len(ids))
        t_phase4 = time.time()
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
        logger.info(
            "Phase 4 complete in %.2fs: Clustered %d templates into %d groups "
            "(%d HDBSCAN clusters + %d noise singletons)",
            time.time() - t_phase4,
            len(ids),
            len(set(template_to_group.values())),
            len(set(template_to_group.values())) - next_singleton,
            next_singleton,
        )
        return template_to_group

    def _match_documentation(self) -> None:
        logger.info("Phase 9: Matching documentation with cluster centroids...")
        t_phase9 = time.time()
        if not self.doc_matcher.ready:
            logger.warning(
                "Documentation matcher is unavailable; training will keep groups undocumented"
            )
            return
        centroids = self.group_registry.all_centroids()
        try:
            matches = self.doc_matcher.match_all(centroids)
        except Exception as exc:  # noqa: BLE001 - documentation is optional enrichment
            logger.warning(
                "Documentation matching failed; continuing training without enrichment: %s",
                exc,
            )
            return
        for gid, match in matches.items():
            group = self.group_registry.get(gid)
            if not group:
                continue
            group.documented = match.documented
            group.documentation_id = match.documentation_id
            group.confidence = match.similarity
            group.error_code = match.error_code
            group.documentation_source = "automatic" if match.documented else "none"
            self.group_registry.upsert(group, flush=False)
        self.group_registry.flush()
        logger.info(
            "Phase 9 complete in %.2fs: Matched documentation for %d groups",
            time.time() - t_phase9,
            len(matches),
        )

    def _group_events(
        self, template_to_group: Dict[str, str]
    ) -> Dict[Tuple[str, str], List[float]]:
        """Bucket event timestamps by (service, group_id) window key.

        The per-event `service` is already carried in each event-index record, so
        splitting the training windows per service costs no extra data. This
        mirrors the realtime pipeline's window key exactly - train/serve parity
        requires both to bucket by (service, group), not by group alone.
        """
        logger.info("Phase 6: Grouping and sorting event timestamps by (service, group)...")
        t_phase6 = time.time()
        result: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        records = sorted(self.event_index.records(), key=lambda r: float(r["timestamp"]))
        for record in records:
            gid = template_to_group.get(str(record.get("template_id")))
            if gid:
                result[(str(record.get("service") or "unknown"), gid)].append(
                    float(record["timestamp"])
                )
        logger.info(
            "Phase 6 complete in %.2fs: Grouped %d events into %d (service, group) windows",
            time.time() - t_phase6,
            len(records),
            len(result),
        )
        return result

    def _train_anomaly_models(
        self, grouped: Dict[Tuple[str, str], List[float]]
    ) -> None:
        logger.info("Phase 7: Extracting feature vectors and training global anomaly model...")
        t_phase7 = time.time()
        all_feature_vectors: List[FeatureVector] = []
        # Every window shares the training start as its bucket origin, matching
        # a realtime engine that has been listening since then: buckets before a
        # window's first event count as silent.
        origin = min((ts[0] for ts in grouped.values() if ts), default=None)
        for window_key, timestamps in grouped.items():
            engine = FeatureEngine(self.config.features, origin=origin)
            for timestamp in timestamps:
                fv = engine.update(window_key, timestamp)
                all_feature_vectors.append(fv)

        logger.info(
            "Phase 7: Collected %d total feature vectors across %d (service, group) windows for global model training",
            len(all_feature_vectors),
            len(grouped),
        )
        t_fit = time.time()
        trained = self.anomaly_model.train(all_feature_vectors)
        if not trained:
            logger.warning(
                "Total feature vectors (%d) < min_training_samples (%d) - global IF model was not fitted.",
                len(all_feature_vectors),
                self.config.anomaly.min_training_samples,
            )
        else:
            logger.info("Phase 7: Model fitting complete in %.2fs", time.time() - t_fit)

        logger.info("Phase 7 complete in %.2fs", time.time() - t_phase7)

    def _flush_all(self) -> None:
        self.template_registry.flush()
        self.group_registry.flush()


def _format_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts)


def _file_size(path: Any) -> int:
    try:
        return os.path.getsize(path)
    except (OSError, TypeError):
        return 0


def _format_size(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def run_training_from_elasticsearch(
    config: AppConfig,
    lookback_seconds: Optional[float] = None,
    max_docs: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> None:
    lookback = config.training.lookback_seconds if lookback_seconds is None else lookback_seconds
    total_docs = config.training.max_docs if max_docs is None else max_docs
    chunk_size = config.training.batch_size if batch_size is None else batch_size

    # Train on what the realtime engine reads: the web's index selection when
    # one was saved, else the configured index.
    selection = IndexSelectionStore(
        f"{config.storage.base_dir}/{config.storage.es_index_selection_file}"
    ).load()
    if selection and selection["entries"]:
        config.elasticsearch.index = ",".join(e["pattern"] for e in selection["entries"])

    checkpoint_file = getattr(
        config.storage, "training_checkpoint_file", "training_checkpoint.json"
    )
    training_checkpoint = CheckpointStore(config.storage, checkpoint_file=checkpoint_file)

    collector = ElasticsearchCollector(config.elasticsearch, training_checkpoint)
    end_ts = time.time()
    start_ts = end_ts - lookback

    initial_search_after = training_checkpoint.get_search_after()
    pipeline = TrainingPipeline(config)

    # On resume, events already in the durable index count towards max_docs;
    # otherwise every restart would fetch another full max_docs window.
    already_indexed = (
        sum(1 for _ in pipeline.event_index.records()) if initial_search_after else 0
    )
    remaining_docs = max(total_docs - already_indexed, 0)

    logger.info(
        "Training job config: index=%s range=[%s .. %s] lookback=%.1fh max_docs=%d "
        "batch_size=%d resume_cursor=%s already_indexed=%d remaining_docs=%d",
        config.elasticsearch.index,
        _format_ts(start_ts),
        _format_ts(end_ts),
        lookback / 3600,
        total_docs,
        chunk_size,
        initial_search_after,
        already_indexed,
        remaining_docs,
    )
    logger.info(
        "Drain3 config: sim_threshold=%.2f depth=%d max_children=%d preprocess=%s "
        "max_tokens=%d level_search_tokens=%d extra_masking_rules=%d | "
        "clustering: min_cluster_size=%d min_samples=%d assign_threshold=%.2f",
        config.drain3.sim_threshold,
        config.drain3.depth,
        config.drain3.max_children,
        config.drain3.preprocess,
        config.drain3.max_tokens,
        config.drain3.level_search_tokens,
        len(config.drain3.masking_rules),
        config.clustering.min_cluster_size,
        config.clustering.min_samples,
        config.clustering.assignment_similarity_threshold,
    )
    batch_stream = collector.stream_historical_batches(
        start_ts=start_ts,
        end_ts=end_ts,
        max_docs=remaining_docs,
        batch_size=chunk_size,
        initial_search_after=initial_search_after,
    )
    t_start = time.time()
    try:
        pipeline.run(batch_stream, checkpoint_store=training_checkpoint)
    except BaseException:
        logger.exception(
            "TRAINING FAILED after %.0fs; training checkpoint (cursor=%s) and event index "
            "(%s) are kept so the next run resumes",
            time.time() - t_start,
            training_checkpoint.get_search_after(),
            _format_size(_file_size(pipeline.event_index.path)),
        )
        raise
    logger.info(
        "TRAINING SUCCEEDED in %.0fs; training checkpoint and event index cleared",
        time.time() - t_start,
    )

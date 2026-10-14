"""Training pipeline (plan section 3): builds all artifacts consumed by the
realtime pipeline from historical logs.

  Historical Logs -> Drain3 -> Template Registry -> Embedding -> HDBSCAN ->
  Group Centroid -> Documentation Matcher -> Feature Generation ->
  Isolation Forest Training -> Model & Registry
"""
from __future__ import annotations

import logging
import os
import random
import re
import shutil
import time
from array import array
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

import numpy as np

from logai.config import AppConfig
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.docmatch.refresh_worker import DocumentationRefreshWorker
from logai.clustering.hdbscan_cluster import GroupClusterer, NOISE_LABEL
from logai.embedding.embedder import TemplateEmbedder
from logai.features.feature_engine import FeatureEngine
from logai.grouping import PENDING_GROUP_ID
from logai.grouping.assignment_manager import GroupAssignmentManager
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel
from logai.models import (
    DEFAULT_LEVEL,
    LEVEL_RANK,
    FeatureVector,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.parsing.drain3_parser import Drain3Parser, display_template
from logai.storage.base import JSONStore, ModelStore
from logai.storage.checkpoint import CheckpointStore
from logai.storage.documentation import DocumentationCorpusStore, DocumentationStoreError
from logai.storage.grouping import (
    GroupingOverrideStore,
    GroupingStoreError,
    derive_grouping_state,
    failed_status,
    first_nonretryable,
)
from logai.storage import es_connection
from logai.storage.index_selection import IndexSelectionStore
from logai.storage.registries import GroupRegistry, TemplateRegistry
from logai.storage.training_event_index import TrainingEventIndex

logger = logging.getLogger("logai.training")
_GROUP_NUMBER_RE = re.compile(r"^G(\d+)$")
# Recent event ids kept for training dedup (~25 MB). Duplicates in one
# historical stream are close together: a resume replays the last batch, and
# the collector re-reads docs that share the cursor's timestamp.
TRAINING_DEDUP_WINDOW = 200_000
# Disk reserved per document for the training event index (~230 B measured).
EVENT_INDEX_BYTES_PER_DOC = 300

WindowKey = Tuple[str, str]


class _RecentIds:
    """Bounded LRU of event ids, so dedup memory does not grow with the window.

    ponytail: a duplicate further apart than `limit` ids is counted twice; only
    the same doc in two indices at very different times can do that.
    """

    def __init__(self, ids: Iterable[str] = (), limit: int = TRAINING_DEDUP_WINDOW):
        self._limit = limit
        self._ids: "OrderedDict[str, None]" = OrderedDict()
        for event_id in ids:
            self.add(event_id)

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, event_id: str) -> None:
        self._ids[event_id] = None
        self._ids.move_to_end(event_id)
        if len(self._ids) > self._limit:
            self._ids.popitem(last=False)


class TrainingProgress:
    """The steps of one training run, for the web Retrain page.

    `on_change(new_step)` is called after every change, with `new_step` true
    when a step starts; the in-process retrain writes `snapshot()` into
    `retrain_status.json`. Without a callback it only records.
    """

    LABELS = {
        "prepare": "Pausing the engine and backing up artifacts",
        "range": "Finding the training range",
        "read": "Reading and parsing logs",
        "templates": "Merging the template registry",
        "embed": "Embedding new templates",
        "cluster": "Grouping new templates",
        "groups": "Building groups",
        "model": "Training the anomaly model",
        "publish": "Publishing registries",
    }

    def __init__(self, on_change: Optional[Callable[[bool], None]] = None):
        self.steps: List[Dict[str, Any]] = []
        self._on_change = on_change

    def step(self, key: str, **detail: Any) -> None:
        self.finish()
        self.steps.append({
            "key": key, "label": self.LABELS[key], "started_at": time.time(),
            "finished_at": None, "detail": detail,
        })
        if self._on_change is not None:
            self._on_change(True)

    def detail(self, **detail: Any) -> None:
        if not self.steps:
            return
        self.steps[-1]["detail"].update(detail)
        if self._on_change is not None:
            self._on_change(False)

    def finish(self) -> None:
        if self.steps and self.steps[-1]["finished_at"] is None:
            self.steps[-1]["finished_at"] = time.time()

    @property
    def current(self) -> Optional[Dict[str, Any]]:
        return self.steps[-1] if self.steps else None

    def snapshot(self) -> List[Dict[str, Any]]:
        return [{**s, "detail": dict(s["detail"])} for s in self.steps]


class TrainingPipeline:
    def __init__(self, config: AppConfig, progress: Optional[TrainingProgress] = None):
        self.config = config
        self.progress = progress or TrainingProgress()
        # Documents the read step should fetch (the page's progress bar).
        self.read_target: Optional[int] = None
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
        self.documentation_store = DocumentationCorpusStore.from_config(config)
        config.doc_matcher.corpus_path = str(self.documentation_store.corpus_path)
        self.doc_matcher = DocumentationMatcher(
            config.doc_matcher, self.embedder,
            f"{config.storage.base_dir}/{config.storage.doc_embeddings_file}",
        )
        self.event_index = TrainingEventIndex(
            f"{config.storage.base_dir}/{config.storage.training_event_index_file}"
        )
        self._lineage_store = JSONStore(
            f"{config.storage.base_dir}/{config.storage.group_lineage_file}"
        )
        self._frozen: Dict[str, str] = {}
        self._lineage: Optional[Dict[str, Any]] = None

    def run(
        self,
        historical_logs: Union[
            List[RawLog],
            Iterable[List[RawLog]],
            Iterable[Tuple[List[RawLog], Optional[List[Any]]]],
        ],
        checkpoint_store: Optional[CheckpointStore] = None,
    ) -> None:
        try:
            self._run_impl(historical_logs, checkpoint_store=checkpoint_store)
        except Exception as exc:
            if self._active_grouping_snapshot is not None:
                previous = {}
                try:
                    previous = self.grouping_store.load_status()
                except GroupingStoreError:
                    pass
                try:
                    self.grouping_store.write_status(failed_status(
                        "engine_exception", str(exc),
                        attempted_revision=self._active_grouping_snapshot["revision"],
                        previous=previous,
                    ))
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
    ) -> None:
        t_start_total = time.time()
        logger.info("=== Training pipeline started ===")
        # Phase 1: Stream parse batches into Drain3 with dedup & Checkpoint
        if checkpoint_store is None or checkpoint_store.get_search_after() is None:
            self.event_index.clear()

        self._stream_parse(historical_logs, checkpoint_store=checkpoint_store)
        self._rebuild_template_registry()
        self._embed_templates()
        try:
            self._active_grouping_snapshot = self.grouping_store.load_overrides()
        except GroupingStoreError as exc:
            self.grouping_store.write_status(
                failed_status("invalid_override_schema", str(exc), attempted_revision=None)
            )
            raise
        template_to_group = self._cluster_templates()
        logger.info("Phase 5: Applying grouping overrides and building groups + centroids...")
        self.progress.step("groups")
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
        self.progress.detail(
            groups=len(groups),
            singletons=sum(1 for group in groups.values() if len(group.template_ids) == 1),
            ungrouped=len(template_states) - len(template_to_group),
        )
        logger.info(
            "Phase 5 complete in %.2fs: Built %d groups with centroids (%d singleton) "
            "from %d templates; overrides applied=%d unresolved=%d; %d templates without a group",
            time.time() - t_phase5,
            len(groups),
            sum(1 for group in groups.values() if len(group.template_ids) == 1),
            len(template_states),
            sum(1 for result in results if result.get("state") == "applied"),
            sum(1 for result in results if result.get("state") == "unresolved"),
            len(template_states) - len(template_to_group),
        )
        self._train_anomaly_model(template_to_group)

        # Publish the complete effective membership only after feature/model
        # generation succeeds. A failed training run therefore never reports
        # the grouping revision as applied.
        logger.info("Phase 8: Publishing template registry, groups and centroids...")
        # Also covers documentation matching and the final save (Phases 9-10).
        self.progress.step("publish", templates=len(template_states), groups=len(groups))
        t_phase8 = time.time()
        self.template_registry.flush()
        self.group_registry.replace_all(groups, centroids)
        self._publish_lineage()
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

    def _publish_lineage(self) -> None:
        """Record which groups grew and which were created (after the
        registries are published, so a failed run never reports one)."""
        if self._lineage is not None:
            self._lineage_store.replace_all(self._lineage)

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
        state = derive_grouping_state(results)
        first_error = first_nonretryable(results)
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
    ) -> None:
        durable_event_ids = _RecentIds(
            self.event_index.recent_event_ids(TRAINING_DEDUP_WINDOW)
        )
        logger.info(
            "Phase 1 start: Drain3 state %s (%s) restored with %d clusters / %d messages; "
            "%d recent event ids loaded from a previous interrupted run",
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
            if self.read_target is None:
                self.read_target = len(historical_logs)
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

        self.progress.step(
            "read", fetched=0, target=self.read_target, parsed=0, skipped_dup=0,
            templates=self.parser.cluster_count(),
        )
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
                if raw.event_id in durable_event_ids:
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
            self.progress.detail(
                fetched=fetched_count,
                parsed=parsed_count,
                skipped_dup=fetched_count - parsed_count,
                templates=self.parser.cluster_count(),
                last_event_ts=last_ts,
                rate=round(fetched_count / elapsed) if elapsed > 0 else 0,
            )
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
            "Phase 1 complete in %.2fs: Parsed %d events across %d batches; "
            "Drain3 has %d clusters, state %s",
            time.time() - t_phase1,
            parsed_count,
            total_batches,
            self.parser.cluster_count(),
            _format_size(_file_size(self.parser.persistence_path)),
        )

    def _rebuild_template_registry(self) -> None:
        """Merge the window's template aggregates into the existing registry.

        Templates seen in the window take its stats; older templates are kept
        with their embeddings. Group membership is frozen: a template that has
        a group keeps it and is never pruned; only an ungrouped template is
        dropped after ``training.template_ttl_days`` unseen.
        """
        logger.info("Phase 2: Merging template registry with event index...")
        self.progress.step("templates")
        t_phase2 = time.time()
        existing = {s.template_id: s for s in self.template_registry.all_templates()}
        self._frozen = {
            s.template_id: s.group_id
            for s in existing.values()
            if s.group_id and s.group_id != PENDING_GROUP_ID
        }
        states: Dict[str, TemplateState] = {}
        for record in self._valid_records():
            template_id = record["template_id"]
            timestamp = record["timestamp"]
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
        seen_in_window = len(states)

        # Event time, not wall clock: a replayed historical window must not
        # look like every template went silent long ago.
        newest = max(
            [s.last_seen for s in states.values()] + [s.last_seen for s in existing.values()],
            default=time.time(),
        )
        cutoff = newest - float(self.config.training.template_ttl_days) * 86400
        protected = self._override_template_ids()
        pruned = 0
        for template_id, old in existing.items():
            state = states.get(template_id)
            if state is not None:
                state.group_id = old.group_id
                state.first_seen = min(state.first_seen, old.first_seen)
                if LEVEL_RANK.get(old.level, 0) > LEVEL_RANK.get(state.level, 0):
                    state.level = old.level
                continue
            if (
                template_id not in self._frozen
                and old.last_seen < cutoff
                and template_id not in protected
            ):
                pruned += 1
                continue
            old.template_text = self._generalized_template_text(template_id) or old.template_text
            states[template_id] = old
        # Text changed (Drain3 generalised it further) -> its embedding is stale.
        for template_id, state in states.items():
            old = existing.get(template_id)
            if old is not None and old.template_text != state.template_text:
                self.template_registry.delete(template_id, flush=False)
        self.template_registry.replace_all(list(states.values()))
        self.progress.detail(
            templates=len(states), seen_in_window=seen_in_window,
            kept=len(states) - seen_in_window, pruned=pruned,
        )
        logger.info(
            "Phase 2 complete in %.2fs: %d templates (%d seen in window, %d kept from "
            "previous runs, %d pruned after %s days unseen)",
            time.time() - t_phase2,
            len(states),
            seen_in_window,
            len(states) - seen_in_window,
            pruned,
            self.config.training.template_ttl_days,
        )

    def _valid_records(self) -> Iterator[Dict[str, Any]]:
        """Event-index records with a usable id, template and timestamp, each
        event once. Phase 2 and the feature pass both read this, so template
        counts and feature windows cover the same events."""
        seen = _RecentIds()
        for record in self.event_index.records():
            event_id = record.get("event_id")
            if (
                not isinstance(event_id, str)
                or event_id in seen
                or not isinstance(record.get("template_id"), str)
                or not isinstance(record.get("timestamp"), (int, float))
            ):
                continue
            seen.add(event_id)
            yield record

    def _override_template_ids(self) -> Set[str]:
        """Templates a grouping override names (as source or anchor): never pruned."""
        try:
            assignments = self.grouping_store.load_overrides().get("assignments", {})
        except GroupingStoreError:
            return set()  # the Phase 5 load reports the invalid file
        ids = set(assignments)
        for value in assignments.values():
            if isinstance(value, dict) and value.get("target_kind") == "anchor":
                ids.add(str(value.get("target_id")))
        return ids

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
        cached = self.template_registry.all_embeddings()
        todo = [t for t in templates if t.template_id not in cached]
        logger.info(
            "Phase 3: Generating embeddings for %d new/changed templates (%d reused)...",
            len(todo),
            len(templates) - len(todo),
        )
        self.progress.step("embed", to_embed=len(todo), reused=len(templates) - len(todo))
        t_phase3 = time.time()
        if todo:
            embeddings = self.embedder.embed([t.template_text for t in todo])
            for state, emb in zip(todo, embeddings):
                self.template_registry.set_embedding(state.template_id, emb, flush=False)
        self.template_registry.flush()
        logger.info(
            "Phase 3 complete in %.2fs: Generated embeddings for %d templates",
            time.time() - t_phase3,
            len(todo),
        )

    def _cluster_templates(self) -> Dict[str, str]:
        """Existing groups are frozen: their templates and IDs never change.
        Only templates without a group are placed: into the nearest existing
        group when similar enough (the same rule realtime uses), otherwise
        HDBSCAN over just those templates forms new groups with new IDs."""
        embeddings_map = self.template_registry.all_embeddings()
        template_to_group = {
            tid: gid for tid, gid in self._frozen.items() if tid in embeddings_map
        }
        candidates = [tid for tid in embeddings_map if tid not in template_to_group]
        logger.info(
            "Phase 4: %d templates keep their group; placing %d without a group...",
            len(template_to_group), len(candidates),
        )
        self.progress.step("cluster", kept_group=len(template_to_group), to_place=len(candidates))
        t_phase4 = time.time()

        added:Dict[str, List[str]] = defaultdict(list)
        centroids = self.group_registry.all_centroids()
        rest: List[str] = []
        for tid in candidates:
            gid, _similarity = self.clusterer.assign_to_nearest_group(
                embeddings_map[tid], centroids
            )
            if gid is None:
                rest.append(tid)
            else:
                template_to_group[tid] = gid
                added[gid].append(tid)

        new: Dict[str, List[str]] = {}
        lineage = self._lineage_store.all()
        try:
            overrides = self.documentation_store.load_overrides().get("overrides", {})
        except DocumentationStoreError:
            overrides = {}  # only used to avoid ID collisions
        next_number = next_group_number(
            [*template_to_group.values(), *centroids, *overrides],
            floor=int(lineage.get("next_group_number") or 1),
        )
        if rest:
            raw_labels = self.clusterer.cluster(
                rest, np.array([embeddings_map[tid] for tid in rest])
            )
            # HDBSCAN labels only collect members; noise templates become
            # single-template groups. Numbered by smallest member for stable IDs.
            by_label: Dict[Any, List[str]] = defaultdict(list)
            for tid, label in raw_labels.items():
                by_label[("noise", tid) if label == NOISE_LABEL else label].append(tid)
            for members in sorted(by_label.values(), key=min):
                gid = f"G{next_number:04d}"
                next_number += 1
                new[gid] = sorted(members)
                template_to_group.update({tid: gid for tid in members})

        self._lineage = {
            "trained_at": time.time(),
            "next_group_number": next_number,
            "added": {gid: sorted(tids) for gid, tids in sorted(added.items())},
            "new": new,
        }
        self.progress.detail(
            joined_existing=sum(len(t) for t in added.values()),
            new_groups=len(new),
            new_group_templates=len(rest),
        )
        logger.info(
            "Phase 4 complete in %.2fs: %d templates added to %d existing groups; "
            "%d new groups from %d templates",
            time.time() - t_phase4,
            sum(len(t) for t in added.values()),
            len(added),
            len(new),
            len(rest),
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

    def _train_anomaly_model(self, template_to_group: Dict[str, str]) -> None:
        """Phases 6-7: feature vectors per (service, group) window, then the
        global Isolation Forest, in memory that does not grow with the events
        beyond 8 bytes each (see `bucket_window_timestamps`)."""
        logger.info("Phase 6: Bucketing event timestamps by (service, group)...")
        self.progress.step("model")
        t_phase6 = time.time()
        windows = bucket_window_timestamps(self._valid_records(), template_to_group)
        self.progress.detail(windows=len(windows))
        logger.info(
            "Phase 6 complete in %.2fs: %d events in %d (service, group) windows",
            time.time() - t_phase6,
            sum(len(ts) for ts in windows.values()),
            len(windows),
        )

        logger.info("Phase 7: Extracting feature vectors and training global anomaly model...")
        t_phase7 = time.time()
        sample_size = max(
            int(self.config.training.feature_sample_size),
            int(self.config.anomaly.min_training_samples),
        )
        kept, scored = sample_window_features(
            windows, self.config.features, sample_size, self.config.anomaly.random_state
        )
        logger.info(
            "Phase 7: scored %d events across %d (service, group) windows; kept %d "
            "feature vectors for the global model (sample_size=%d)",
            scored,
            len(windows),
            len(kept),
            sample_size,
        )
        self.progress.detail(scored=scored, kept=len(kept), sample_size=sample_size)
        t_fit = time.time()
        trained = self.anomaly_model.train(kept)
        self.progress.detail(fitted=bool(trained))
        if not trained:
            logger.warning(
                "Total feature vectors (%d) < min_training_samples (%d) - global IF model was not fitted.",
                len(kept),
                self.config.anomaly.min_training_samples,
            )
        else:
            logger.info("Phase 7: Model fitting complete in %.2fs", time.time() - t_fit)

        logger.info("Phase 7 complete in %.2fs", time.time() - t_phase7)

    def _flush_all(self) -> None:
        self.template_registry.flush()
        self.group_registry.flush()


def next_group_number(ids: Iterable[str], floor: int = 1) -> int:
    """First counter value above every `G<n>` id seen; IDs are never reused.
    Manual (`G_MANUAL_*`) and legacy (`G_SINGLE_*`) ids are ignored."""
    numbers = [int(m.group(1)) for gid in ids if (m := _GROUP_NUMBER_RE.match(str(gid)))]
    return max([floor - 1, *numbers]) + 1


def bucket_window_timestamps(
    records: Iterable[Dict[str, Any]], template_to_group: Dict[str, str]
) -> Dict[WindowKey, array]:
    """Event timestamps per (service, group) window, each sorted ascending.

    Only (window, timestamp) is kept per event, as an 8-byte float in a compact
    array, instead of the whole record. Windows are independent, so sorting
    within each window is all the feature pass needs. The key mirrors the
    realtime window exactly (train/serve parity). Templates without a group are
    skipped.
    """
    windows: Dict[WindowKey, array] = {}
    for record in records:
        gid = template_to_group.get(str(record.get("template_id")))
        if not gid:
            continue
        key = (str(record.get("service") or "unknown"), gid)
        bucket = windows.get(key)
        if bucket is None:
            bucket = windows[key] = array("d")
        bucket.append(float(record["timestamp"]))
    for key, bucket in windows.items():
        # The index is already in Elasticsearch @timestamp order; only a
        # timestamp the collector could not parse (read as "now") breaks it.
        # ponytail: sorting an out-of-order window builds a list of that
        # window's floats (~32 B/event); use an external sort past ~100M events.
        if any(a > b for a, b in zip(bucket, islice(bucket, 1, None))):
            windows[key] = array("d", sorted(bucket))
    return windows


def sample_window_features(
    windows: Dict[WindowKey, array],
    config: Any,
    sample_size: int,
    seed: int,
) -> Tuple[List[FeatureVector], int]:
    """Feed every event through the feature engine (baselines need the full
    stream) and keep a uniform random sample of exactly
    min(`sample_size`, events) vectors (selection sampling, Knuth's
    Algorithm S: an exact count, so a small budget never falls under
    `min_training_samples` by chance). Returns the kept vectors and the number
    of events scored.

    Uniform over events on purpose: the model must learn the volume-weighted
    mix realtime scores. Over-weighting quiet windows (stratifying) teaches it
    that their spiky vectors are normal and it stops flagging real bursts; see
    tests/test_training_scale.py. Below ~20k vectors the threshold and the
    detection of a burst vary from seed to seed, hence the 100k default.
    """
    total = sum(len(ts) for ts in windows.values())
    wanted = min(sample_size, total)
    # Every window shares the training start as its bucket origin, matching a
    # realtime engine that has been listening since then: buckets before a
    # window's first event count as silent.
    origin = min((ts[0] for ts in windows.values() if ts), default=None)
    rng = random.Random(seed)
    kept: List[FeatureVector] = []
    scored = 0
    for key, timestamps in windows.items():
        # One engine per window: a finished window's deques are freed instead
        # of piling up for every window at once.
        engine = FeatureEngine(config, origin=origin)
        for timestamp in timestamps:
            fv = engine.update(key, timestamp)
            # Keep with probability (still wanted) / (still to come).
            if rng.random() * (total - scored) < wanted - len(kept):
                kept.append(fv)
            scored += 1
    return kept, scored


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
    progress: Optional[TrainingProgress] = None,
) -> None:
    lookback =config.training.lookback_seconds if lookback_seconds is None else lookback_seconds
    total_docs = config.training.max_docs if max_docs is None else max_docs
    chunk_size = config.training.batch_size if batch_size is None else batch_size

    # Train on what the realtime engine reads: the web's index selection when
    # one was saved, else the configured index.
    selection = IndexSelectionStore(
        f"{config.storage.base_dir}/{config.storage.es_index_selection_file}"
    ).load()
    if selection and selection["entries"]:
        config.elasticsearch.index = ",".join(e["pattern"] for e in selection["entries"])
    # ... and the endpoint/credentials saved on the web, when there are any.
    config.elasticsearch = es_connection.apply(
        config.elasticsearch,
        es_connection.ESConnectionStore(
            f"{config.storage.base_dir}/{config.storage.es_connection_file}"
        ).load(),
    )

    checkpoint_file = getattr(
        config.storage, "training_checkpoint_file", "training_checkpoint.json"
    )
    training_checkpoint = CheckpointStore(config.storage, checkpoint_file=checkpoint_file)

    collector = ElasticsearchCollector(config.elasticsearch, training_checkpoint)
    end_ts = time.time()
    start_ts = end_ts - lookback

    pipeline = TrainingPipeline(config, progress)
    pipeline.progress.step(
        "range", index=config.elasticsearch.index, start_ts=start_ts, end_ts=end_ts,
    )
    initial_search_after = training_checkpoint.get_search_after()
    if initial_search_after is None and total_docs > 0:
        # Keep the newest max_docs, not the oldest; a resume follows its cursor.
        # 1% headroom: on a live index, docs indexed late into the range and
        # re-read page boundaries also count toward max_docs, and would make
        # the stream stop just before the newest logs.
        start_ts = collector.newest_start(start_ts, end_ts, total_docs - total_docs // 100)

    # On resume, events already in the durable index count towards max_docs;
    # otherwise every restart would fetch another full max_docs window.
    already_indexed = (
        sum(1 for _ in pipeline.event_index.records()) if initial_search_after else 0
    )
    remaining_docs = max(total_docs - already_indexed, 0)
    pipeline.read_target = remaining_docs
    pipeline.progress.detail(
        start_ts=start_ts, resumed=initial_search_after is not None,
        already_indexed=already_indexed,
    )
    # The event index takes ~230 B/event (measured on real logs) on the volume
    # the engine also writes to: refuse up front instead of filling it minutes
    # into the run.
    need = remaining_docs * EVENT_INDEX_BYTES_PER_DOC
    free = shutil.disk_usage(pipeline.event_index.path.parent).free
    if need > free:
        raise RuntimeError(
            f"Not enough disk for max_docs={total_docs}: the training event index "
            f"needs about {need / 1e9:.1f} GB, {free / 1e9:.1f} GB free in "
            f"{pipeline.event_index.path.parent}. Lower max_docs or free space."
        )

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

"""Realtime pipeline (plan section 4):

  Collect -> Parse -> Assign(Known/Unknown) -> Match(doc) -> Aggregate
  (features) -> Predict (anomaly) -> Alert State -> Export (Prometheus)

No retraining / re-clustering happens here - unresolved ("Unknown/Pending")
templates just wait for the next training run.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Optional, Tuple, Union

from logai.config import AppConfig
from logai.alert.alert_state_machine import (
    AlertStateMachine,
    _parse_group_id_key,
    group_id_key,
)
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel, GroupAnomalyModels
from logai.clustering.hdbscan_cluster import GroupClusterer
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.docmatch.refresh_worker import DocumentationRefreshWorker
from logai.embedding.embedder import TemplateEmbedder
from logai.features.feature_engine import FeatureEngine
from logai.features.template_activity import TemplateActivity
from logai.grouping import PENDING_GROUP_ID
from logai.grouping.assignment_manager import (
    GroupAssignmentError,
    GroupAssignmentManager,
)
from logai.incident.classifier import IncidentClassifier
from logai.incident.profiles import LLMProfileStore
from logai.incident.cases import load_cases
from logai.incident.requests import DEFAULT_LANGUAGE, load_requests, parse_key
from logai.incident.service_analysis import build_service_evidence, service_signature
from logai.incident.template_triage import build_template_evidence
from logai.metrics.prometheus_exporter import MetricsExporter
from logai.models import (
    AlertStateEnum,
    AnomalyResult,
    AnomalyState,
    DEFAULT_LEVEL,
    LEVEL_RANK,
    FeatureVector,
    GroupedEvent,
    GroupState,
    ParsedEvent,
    RawLog,
    TemplateState,
)
from logai.parsing.drain3_parser import Drain3Parser
from logai.reliability.dlq import DeadLetterQueue
from logai.storage.base import JSONStore, ModelStore
from logai.storage.checkpoint import CheckpointStore
from logai.storage.dedup import DedupIndex
from logai.storage.documentation import DocumentationCorpusStore
from logai.storage.grouping import GroupingOverrideStore, GroupingStoreError
from logai.storage.index_selection import (
    IndexSelectionStore,
    entries_from_config,
    selection_revision,
    write_status,
)
from logai.storage.registries import GroupRegistry, TemplateRegistry
from logai.storage.retrain_schedule import (
    RetrainScheduleStore,
    next_run_at,
    read_json,
    restore_artifacts,
    snapshot_artifacts,
)

logger = logging.getLogger("logai.realtime")

# Recent extracted template parameters kept per (service, group) window as
# evidence for the LLM incident analysis.
RECENT_PARAMS_PER_WINDOW = 200

class RealtimePipeline:
    def __init__(self, config: AppConfig):
        self.config = config
        # Before anything loads artifacts: undo a retrain this engine was
        # killed in the middle of, and note scheduled runs it was down for.
        self.retrain_store = RetrainScheduleStore.from_config(config)
        self._recover_interrupted_retrain()
        self._record_missed_retrains()

        self.checkpoint = CheckpointStore(config.storage)
        self.collector = ElasticsearchCollector(config.elasticsearch, self.checkpoint)
        self.parser = Drain3Parser(config.drain3)
        self.embedder = TemplateEmbedder(config.embedding)
        self.clusterer = GroupClusterer(config.clustering)

        self.template_registry = TemplateRegistry(config.storage)
        self.group_registry = GroupRegistry(config.storage)
        self.grouping_store = GroupingOverrideStore.from_config(config)
        self.grouping_manager = GroupAssignmentManager(
            self.template_registry,
            self.group_registry,
            self.embedder.embed_one,
        )

        self.documentation_store = DocumentationCorpusStore.from_config(config)
        config.doc_matcher.corpus_path = str(self.documentation_store.corpus_path)
        self.doc_matcher = DocumentationMatcher(
            config.doc_matcher, self.embedder,
            f"{config.storage.base_dir}/{config.storage.doc_embeddings_file}",
        )
        self.feature_engine = FeatureEngine(config.features)
        # Recent per-template counts and each template's normal rate: the only
        # counts LLM evidence uses. Reloaded so a restart keeps the baseline.
        self._template_activity_path = (
            f"{config.storage.base_dir}/{config.storage.template_activity_file}"
        )
        self.template_activity = TemplateActivity.load(self._template_activity_path)
        self._template_activity_saved_at = time.monotonic()
        self.model_store = ModelStore(config.storage.model_dir)
        self.anomaly_model = GlobalAnomalyModel(config.anomaly, self.model_store)
        self.anomaly_models = self.anomaly_model  # backward compatibility alias
        self.documentation_worker = DocumentationRefreshWorker(
            self.documentation_store,
            self.doc_matcher,
            self.group_registry,
            self.template_registry,
            config.doc_matcher.refresh_interval_seconds,
        )

        alert_state_store = JSONStore(
            f"{config.storage.base_dir}/{config.storage.anomaly_state_file}"
        )
        self.alert_sm = AlertStateMachine(config.alert, alert_state_store)
        self.dedup = DedupIndex(
            config.storage,
            config.reliability.dedup_ttl_seconds,
            config.reliability.dedup_max_size,
            flush_interval_seconds=config.reliability.dedup_flush_interval_seconds,
        )
        self.dlq = DeadLetterQueue(
            f"{config.storage.base_dir}/{config.storage.dlq_file}"
        )
        self.metrics = MetricsExporter(config.metrics)

        # Wire metric hooks into the collector. Done here (not next to the
        # collector's construction) because self.metrics must exist first.
        self.collector._on_retry_hook = (
            lambda attempt, exc: self.metrics.logai_retry_total.inc()
        )
        self.collector._malformed_counter = self.metrics.logai_es_malformed_hits_total

        # LLM incident analysis: one job per alert episode (entering ALERTING),
        # run on a background thread. The endpoint comes from the active web
        # profile, else from LLM env config; with neither it stays disabled.
        self.incident_classifier: Optional[IncidentClassifier] = None
        self.incident_classifier = IncidentClassifier(
            config.llm,
            self.doc_matcher,
            self.group_registry,
            self.template_registry,
            JSONStore(
                f"{config.storage.base_dir}/{config.storage.incident_analysis_file}"
            ),
            on_result=lambda result: self.metrics.logai_llm_requests_total.labels(
                result=result
            ).inc(),
            service_store=JSONStore(
                f"{config.storage.base_dir}/{config.storage.service_analysis_file}"
            ),
            template_store=JSONStore(
                f"{config.storage.base_dir}/{config.storage.template_triage_file}"
            ),
            cases_path=f"{config.storage.base_dir}/{config.storage.incident_cases_file}",
            on_call=lambda kind, meta: self.metrics.record_llm_call(kind, meta),
            history_path=f"{config.storage.base_dir}/{config.storage.analysis_history_file}",
        )
        self._drop_vanished_groups(alert_state_store)
        # On-demand whole-service analysis requests (web-owned file).
        self._analysis_requests_path = (
            f"{config.storage.base_dir}/{config.storage.analysis_requests_file}"
        )
        self._handled_requests: dict[str, float] = {}
        self._env_llm_config = config.llm
        self._llm_profiles = LLMProfileStore(
            f"{config.storage.base_dir}/{config.storage.llm_profiles_file}"
        )
        self._llm_profile_id: Optional[str] = None
        self._llm_profile_name: Optional[str] = None
        self._llm_profile_error: Optional[str] = None
        self._es_error: Optional[str] = None
        self._es_error_since: Optional[float] = None
        self._refresh_llm_profile()
        # Recent template parameters per window: evidence for on-demand LLM
        # analysis. Written by the poll thread, read by the control thread.
        self._recent_params: dict[Tuple[str, str], deque] = {}
        self._params_lock = threading.Lock()

        # DocumentationMatcher already attempted its initial load in __init__.
        # Avoid encoding the same corpus again on the first realtime event.
        self._template_counts: dict[str, int] = {}
        for svc, count in self.template_registry.all_counts_by_service().items():
            self.metrics.set_template_count(svc, count)

        # Event-clock for the idle-alert tick. Event timestamps and time.time()
        # share the epoch-UTC scale, so the clock advances by real batch events
        # and, while idle, drifts forward with wall-clock (monotonic) time.
        self._event_clock: float = 0.0            # max event timestamp observed
        self._event_clock_wall: float = time.monotonic()
        self._last_idle_tick: float = 0.0         # throttle marker for the tick

        # Micro-batch predict buffer (TODO #7). Feature vectors accumulate here
        # across polls - both live event vectors and idle-tick snapshots - and
        # are scored in a single vectorized predict_batch when the buffer holds
        # `predict_batch_size` entries OR `predict_max_wait_seconds` have passed
        # since its first entry (whichever comes first). The cursor is tracked
        # separately from the buffer so mixing idle snapshots (which advance no
        # cursor) never corrupts the checkpoint.
        self._pending_predictions: list[Tuple[str, FeatureVector]] = []
        # index -> (cursor, last event timestamp) of batches processed since the
        # last flush; the key None is the legacy single configured-index cursor.
        self._pending_cursors: dict[Optional[str], Tuple[Any, float]] = {}
        self._buffer_started_at: Optional[float] = None
        self._grouping_initialized = False
        self._grouping_retry_needed = False
        self._last_grouping_template_generation = (
            self.template_registry.mutation_generation
        )
        self._last_grouping_heartbeat = 0.0
        self._last_successful_poll_at: Optional[float] = None
        self._last_processed_event_at: Optional[float] = None
        self._last_checkpoint_commit_at: Optional[float] = None

        # Indices chosen on the web "Data sources" page. Until the web saves a
        # selection the engine reads config.elasticsearch.index with the legacy
        # single cursor (_active_indices None); afterwards every concrete index
        # has its own cursor (see _refresh_index_selection).
        storage = config.storage
        self.index_selection = IndexSelectionStore(
            f"{storage.base_dir}/{storage.es_index_selection_file}"
        )
        self._index_status_path = f"{storage.base_dir}/{storage.es_index_status_file}"
        self._active_indices: Optional[list[str]] = None
        self._selection_entries: list[dict] = entries_from_config(
            config.elasticsearch.index, self.checkpoint.get_start_ts() or time.time()
        )
        self._resolved: dict[str, list[str]] = {}
        self._applied_selection_revision: Optional[str] = None
        self._selection_mtime: Optional[int] = None
        self._last_resolve_at = float("-inf")
        self._resolve_error: Optional[str] = None
        self._index_progress: dict[str, dict] = {}
        self._index_lock = threading.Lock()
        self._available_indices: Optional[list] = None
        self._available_error: Optional[str] = None
        self._last_index_list_at = float("-inf")
        self._last_index_status: Optional[dict] = None
        # Retrain from the web "Retrain" page: the control tick decides, the
        # poll loop runs it at a batch boundary (see _run_retrain).
        self._started_at = time.time()
        self._retrain_trigger: Optional[dict] = None

    def _drop_vanished_groups(self, alert_state_store: JSONStore) -> None:
        """Drop alert state and LLM analyses of groups a retrain merged, split
        or retired (see group_lineage.json): their IDs are never reused, so the
        state would otherwise linger forever."""
        live = {group.group_id for group in self.group_registry.all_groups()}
        if not live:
            return  # not trained yet: nothing to compare against
        vanished = set()
        for store in (alert_state_store, self.incident_classifier.store):
            for key in store.all():
                identity = _parse_group_id_key(key)
                gid = identity[1] if isinstance(identity, tuple) and len(identity) == 2 else identity
                if gid not in live:
                    vanished.add(gid)
        for gid in vanished:
            self.alert_sm.drop_group(gid)
            self.incident_classifier.drop_group(gid)
        if vanished:
            logger.info("Dropped state of %d groups that no longer exist", len(vanished))

    def start_metrics_server(self) -> None:
        self.metrics.start()
        logger.info(
            "Prometheus metrics available on %s:%d/metrics",
            self.config.metrics.http_host, self.config.metrics.http_port,
        )

    def run_forever(self) -> None:
        self.start_metrics_server()
        documentation_worker = getattr(self, "documentation_worker", None)
        if (
            documentation_worker is not None
            and self.documentation_store.corpus_path.suffix.lower() == ".json"
        ):
            documentation_worker.start()
        incident_classifier = getattr(self, "incident_classifier", None)
        if incident_classifier is not None:
            incident_classifier.start()
        # Profile switches and the heartbeat (with the LLM status reason) run on
        # their own timer: an Elasticsearch outage blocks the poll loop for
        # minutes of retries, and the web must still learn why.
        self._control_stop = threading.Event()
        threading.Thread(target=self._control_loop, name="engine-control", daemon=True).start()
        logger.info("Realtime pipeline started, polling Elasticsearch...")
        try:
            self._poll_loop()
        finally:
            self._control_stop.set()
            self._save_template_activity(force=True)

    def _poll_loop(self) -> None:
        consecutive_poll_failures = 0
        MAX_POLL_BACKOFF = 300.0  # 5 minutes

        while True:
            trigger = getattr(self, "_retrain_trigger", None)
            if trigger is not None:
                self._run_retrain(trigger)
            self._refresh_grouping_if_needed()
            self._refresh_index_selection()
            # ── Phase 1: Poll ES (one batch per selected index) ───────────
            polled, failures = self._poll_indices()
            if failures and len(failures) == len(polled) + len(failures):
                exc = failures[-1]
                self._record_es_error(exc)
                consecutive_poll_failures += 1
                backoff = min(
                    self.config.elasticsearch.poll_interval_seconds
                    * (2 ** consecutive_poll_failures),
                    MAX_POLL_BACKOFF,
                )
                logger.error(
                    "ES poll failed (%d consecutive): %s. Retrying in %.1fs...",
                    consecutive_poll_failures, exc, backoff,
                )
                self.metrics.logai_es_poll_errors_total.inc()
                self.metrics.logai_pipeline_errors_total.labels(
                    stage="elasticsearch", reason_code="poll_failed"
                ).inc()
                time.sleep(backoff)
                continue
            if failures:
                # Some indices failed: count it, keep reading the others.
                self.metrics.logai_es_poll_errors_total.inc(len(failures))
            self._record_es_ok()
            consecutive_poll_failures = 0
            self._last_successful_poll_at = time.time()
            self.metrics.logai_engine_heartbeat_timestamp_seconds.set(
                self._last_successful_poll_at
            )
            self.metrics.logai_last_successful_poll_timestamp_seconds.set(
                self._last_successful_poll_at
            )

            # A command accepted while the ES request was in flight applies to
            # the fetched batch as a whole, before its first event is processed.
            self._refresh_grouping_if_needed()

            # ── Phase 2: Accumulate batch into the predict buffer ─────────
            # Only parse/group/feature-extract/append here; scoring happens at
            # the flush boundary so 500 events become one vectorized inference.
            batch = [raw for _, b, _ in polled for raw in b]
            for index, index_batch, cursor in polled:
                if not index_batch:
                    continue
                self.metrics.logai_queue_depth.set(len(index_batch))
                self.metrics.logai_events_received_total.inc(len(index_batch))
                try:
                    for raw in index_batch:
                        if not self._process_one(raw):
                            raise RuntimeError(
                                f"Event {raw.event_id} did not reach a terminal state"
                            )
                    # Cursor advances only after a successful flush (Phase 5),
                    # never here - a crash before commit replays idempotently.
                    if cursor is not None:
                        self._pending_cursors[index] = (cursor, index_batch[-1].timestamp)
                    self._event_clock = max(self._event_clock, index_batch[-1].timestamp)
                    self._event_clock_wall = time.monotonic()
                finally:
                    self.metrics.logai_queue_depth.set(0)

            # ── Phase 3: Idle tick (throttled) ────────────────────────────
            # Appends snapshot vectors for silent groups into the SAME buffer.
            self._maybe_tick_idle(self._now_event_time())

            # Start the buffer timer the moment it first becomes non-empty,
            # regardless of whether the entry came from a live event or an
            # idle snapshot, so the time-based flush criterion always applies.
            if self._pending_predictions and self._buffer_started_at is None:
                self._buffer_started_at = time.monotonic()

            # ── Phase 4: Flush decision (count OR time, whichever first) ──
            n = len(self._pending_predictions)
            waited = (
                time.monotonic() - self._buffer_started_at
                if self._buffer_started_at is not None else 0.0
            )
            should_flush = (
                (n > 0 and (
                    n >= self.config.anomaly.predict_batch_size
                    or waited >= self.config.anomaly.predict_max_wait_seconds
                ))
                or (bool(batch) and bool(self._pending_cursors))
            )
            if should_flush:
                self._flush_batch()
                # A fetched batch is no longer queued only after prediction and
                # durability work has completed.
                if batch:
                    self.metrics.logai_queue_depth.set(0)

            # ── Phase 5: Sleep when the stream was empty this cycle ───────
            if not batch:
                poll_interval = self.config.elasticsearch.poll_interval_seconds
                if self._pending_predictions and self._buffer_started_at is not None:
                    # Buffer still filling: wake in time to honour the 1s timer.
                    remaining = (
                        self.config.anomaly.predict_max_wait_seconds
                        - (time.monotonic() - self._buffer_started_at)
                    )
                    time.sleep(max(0.0, min(poll_interval, remaining)))
                else:
                    time.sleep(poll_interval)

    def _flush_batch(self) -> None:
        """Score the accumulated buffer, then run the durability sequence.

        Ordering (unchanged crash-safety contract): predict+apply -> registry
        flush -> dedup gc -> checkpoint.commit. A cursor advances only when a
        real stream batch contributed to this flush (`_pending_cursors`); an
        idle-only flush (empty stream) scores snapshots but commits nothing.
        """
        try:
            self._flush_predictions()
        except Exception:
            self.metrics.logai_pipeline_errors_total.labels(
                stage="prediction", reason_code="prediction_failed"
            ).inc()
            raise

        # Registry updates used flush=False on the hot path; make them durable
        # before the cursor advances (no-op when nothing new was written).
        try:
            self.template_registry.flush()
        except Exception:
            self.metrics.logai_pipeline_errors_total.labels(
                stage="storage", reason_code="template_registry_write_failed"
            ).inc()
            raise
        try:
            self.group_registry.flush()
        except Exception:
            self.metrics.logai_pipeline_errors_total.labels(
                stage="storage", reason_code="group_registry_write_failed"
            ).inc()
            raise
        try:
            self.dedup.gc()
        except Exception:
            self.metrics.logai_pipeline_errors_total.labels(
                stage="dedup", reason_code="dedup_flush_failed"
            ).inc()
            raise

        if self._pending_cursors:
            cursors = dict(self._pending_cursors)
            legacy = cursors.pop(None, None)
            try:
                if legacy is not None:
                    self.checkpoint.commit(*legacy)
                if cursors:
                    self.checkpoint.commit_indices(cursors)
            except Exception:
                self.metrics.logai_pipeline_errors_total.labels(
                    stage="checkpoint", reason_code="checkpoint_write_failed"
                ).inc()
                raise
            self._last_checkpoint_commit_at = time.time()
            self.metrics.logai_last_checkpoint_commit_timestamp_seconds.set(
                self._last_checkpoint_commit_at
            )

        self._pending_cursors = {}
        self._buffer_started_at = None

    def _flush_predictions(self) -> None:
        """Run one vectorized inference over the buffered feature vectors and
        apply the resulting alert-state transitions in event order.

        Batching is result-equivalent to per-event scoring: predict is a pure
        function of each captured feature vector and the (immutable) model, and
        applying transitions in buffer order reproduces the exact state-machine
        evolution of the old per-event path. One entry is kept per EVENT (never
        collapsed per group) so intermediate escalations are not lost.
        """
        buf = self._pending_predictions
        if not buf:
            return
        results = self.anomaly_model.predict_batch([fv for _, fv in buf])
        scored = []
        for (window_key, _fv), result in zip(buf, results):
            if result is None:
                continue  # global model not yet trained
            # window_key is (service, group_id); the group registry is keyed by the
            # plain group_id, and set_anomaly_score needs the service separately.
            service, group_id = window_key
            group = self.group_registry.get(group_id) or GroupState(group_id=group_id)
            self.metrics.set_anomaly_score(group, result.anomaly_score, service=service)
            scored.append(result)

        states = self.alert_sm.transition_batch(scored)
        for state in states:
            self.metrics.set_alert_state(state)
        self._pending_predictions = []

    def _process_one(self, raw: RawLog) -> bool:
        start = time.time()
        try:
            if self.dedup.seen(raw.event_id):
                return True  # idempotency: already processed this event_id

            parsed = self.parser.parse(raw)
            self.template_activity.record(raw.service, parsed.template_id, raw.timestamp)
            grouped = self._assign_group(parsed)
            self.metrics.record_raw_event(parsed)

            if grouped is not None:
                # Window/alert identity is (service, group_id): each service keeps
                # its own rate baseline + alert state inside a shared semantic
                # group. doc-match above stays group-level (grouped.group_id); only
                # the feature/alert path is per-service. One update + one append.
                window_key = (raw.service, grouped.group_id)
                fv = self.feature_engine.update(window_key, raw.timestamp)
                if getattr(self, "incident_classifier", None) is not None and any(
                    value != "<*>" for value in parsed.parameters
                ):
                    with self._params_lock:
                        self._recent_params.setdefault(
                            window_key, deque(maxlen=RECENT_PARAMS_PER_WINDOW)
                        ).append((parsed.template_id, parsed.parameters))
                # Defer scoring: buffer the (window_key, vector) pair for the next
                # batch flush instead of calling predict() once per event.
                self._pending_predictions.append((window_key, fv))

            self.dedup.mark(raw.event_id)
            self.metrics.logai_events_processed_total.inc()
            self._last_processed_event_at = raw.timestamp
            self.metrics.logai_last_processed_event_timestamp_seconds.set(
                self._last_processed_event_at
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process event %s: %s", raw.event_id, exc)
            self.metrics.logai_events_failed_total.inc()
            self.metrics.logai_pipeline_errors_total.labels(
                stage="event", reason_code="processing_failed"
            ).inc()
            self.dlq.push(raw.to_dict(), str(exc))
            self.metrics.logai_dlq_records_total.labels(
                reason_code="processing_failed"
            ).inc()
            return True  # durable DLQ is a terminal outcome for this event
        finally:
            self.metrics.logai_processing_latency_seconds.observe(time.time() - start)

    def _assign_group(self, parsed: ParsedEvent) -> Optional[GroupedEvent]:
        existing_template = self.template_registry.get(parsed.template_id)
        # Normalise the incoming level once for all three branches below. Levels
        # arrive free-form from Elasticsearch (casing/synonyms vary per app), so
        # they are upper-cased for ranking; an empty value falls back to the
        # collector's own default rather than poisoning the monotonic max.
        event_level = str(parsed.raw.level or DEFAULT_LEVEL).strip().upper()
        event_rank = LEVEL_RANK.get(event_level, LEVEL_RANK[DEFAULT_LEVEL])

        if existing_template:
            if existing_template.group_id and existing_template.group_id != PENDING_GROUP_ID:
                # Known Template -> direct group mapping (plan 4.3), no embedding needed.
                existing_template.last_seen = parsed.raw.timestamp
                existing_template.event_count += 1
                # Monotonic: a low-severity event never downgrades the template.
                if event_rank > LEVEL_RANK.get(
                    existing_template.level, LEVEL_RANK[DEFAULT_LEVEL]
                ):
                    existing_template.level = event_level
                self.template_registry.upsert(existing_template, flush=False)
                self._touch_group(existing_template.group_id, parsed.raw.timestamp)
                return GroupedEvent(parsed=parsed, group_id=existing_template.group_id)

            # Known Pending Template -> already attempted clustering and awaiting next training run.
            # Fast-path bypass: update stats and return None immediately without re-embedding.
            existing_template.last_seen = parsed.raw.timestamp
            existing_template.event_count += 1
            if existing_template.group_id != PENDING_GROUP_ID:
                existing_template.group_id = PENDING_GROUP_ID
            if event_rank > LEVEL_RANK.get(
                existing_template.level, LEVEL_RANK[DEFAULT_LEVEL]
            ):
                existing_template.level = event_level
            self.template_registry.upsert(existing_template, flush=False)
            return None

        # Unknown Template (new to drain3, or known but not yet grouped) -> embed & assign.
        embedding = self.embedder.embed_one(parsed.template)
        centroids = self.group_registry.all_centroids()
        group_id, similarity = self.clusterer.assign_to_nearest_group(embedding, centroids)

        state = TemplateState(
            template_id=parsed.template_id,
            template_text=parsed.template,
            service=parsed.raw.service,
            level=event_level,
            first_seen=parsed.raw.timestamp,
            last_seen=parsed.raw.timestamp,
            event_count=1,
            group_id=group_id if group_id is not None else PENDING_GROUP_ID,
        )
        self.template_registry.set_embedding(parsed.template_id, embedding, flush=False)
        is_new = self.template_registry.upsert(state, flush=False)
        if is_new:
            self._update_template_metrics(parsed.raw.service)
            self._grouping_retry_needed = True

        if group_id is None:
            # Doesn't clear the similarity threshold against any existing
            # group -> leave ungrouped/pending; a future training run will
            # cluster it properly (plan 4.4).
            logger.info(
                "Template %s is Unknown/Pending (best similarity=%.2f) - "
                "awaiting next training run to form/join a group.",
                parsed.template_id, similarity,
            )
            return None

        self._touch_group(group_id, parsed.raw.timestamp, new_template_id=parsed.template_id)
        return GroupedEvent(parsed=parsed, group_id=group_id, group_similarity=similarity)

    def _touch_group(
        self, group_id: str, timestamp: float, new_template_id: Optional[str] = None
    ) -> None:
        self.group_registry.touch(group_id, timestamp, new_template_id)

    def _match_documentation_if_stale(self, group_id: str) -> None:
        """Compatibility helper; global refresh is owned by the background worker."""
        if not self.doc_matcher.ready:
            return
        group = self.group_registry.get(group_id)
        if group is not None and group.documentation_source == "manual":
            return
        centroid = self.group_registry.get_centroid(group_id)
        if group is None or centroid is None:
            return
        match = self.doc_matcher.match(group_id, centroid)
        group.documented = match.documented
        group.documentation_id = match.documentation_id
        group.confidence = match.similarity
        group.error_code = match.error_code
        group.documentation_source = "automatic" if match.documented else "none"
        self.group_registry.upsert(group, flush=False)

    # --- idle-alert tick ------------------------------------------------------

    def _now_event_time(self) -> float:
        """Current time on the event-timestamp scale.

        Anchored to the newest observed event timestamp and advanced by
        monotonic wall-clock since that anchor, so the idle tick keeps making
        progress while the stream is empty (no new events to move the clock).
        """
        return self._event_clock + (time.monotonic() - self._event_clock_wall)

    def _evaluate_idle_alerting_groups(self, current_timestamp: float) -> None:
        """Collect snapshot feature vectors for windows (service, group_id) that
        have gone silent and append them to the SAME predict buffer as live
        events, so they are scored together in the next batch flush.

        The set of windows to re-evaluate is the union of:
          - (C) alert_sm.groups_not_normal() - persisted tuples (survive a
            restart; a stuck ALERTING cell cools down even with zero new events),
          - (B) feature_engine.live_window_keys() - every cell that has a live
            window this process lifetime (keeps NORMAL cells' scores fresh).
        Both yield (service, group_id) tuples, so the union is per-CELL, never
        guessing a group's services from representative metadata (which would
        miss a second service sharing a template).

        The silence guard is PER-CELL via feature_engine.last_event_ts(): it
        skips any cell still receiving logs (owned by the per-event path, whose
        live vector is already buffered), so a cell can never appear twice in one
        flush. A cell that is in the alert set but has no live window yet (e.g. a
        pre-restart ALERTING cell with no traffic since boot) has last_event_ts()
        == None, so it is still snapshotted - snapshot() materializes an empty
        window via defaultdict and yields a neutral vector, cooling it to NORMAL.
        This is why we do NOT drive the tick off live_window_keys() alone.
        """
        gap = self.config.alert.idle_eval_seconds
        cooling = self.alert_sm.groups_not_normal()
        live = self.feature_engine.live_window_keys()
        for window_key in set(cooling) | set(live):
            last = self.feature_engine.last_event_ts(window_key)
            if last is not None and current_timestamp - last < gap:
                continue  # still receiving logs -> the per-event path owns it
            try:
                fv = self.feature_engine.snapshot(window_key, current_timestamp)
            except Exception as exc:  # noqa: BLE001
                # Snapshot collection is the only idle-specific step; a single
                # bad cell must not abort the whole tick.
                logger.warning(
                    "idle snapshot failed for window %s: %s", window_key, exc
                )
                continue
            self._pending_predictions.append((window_key, fv))

    def _maybe_tick_idle(self, current_timestamp: float) -> None:
        """Throttle the idle-alert tick to at most once per idle_eval_seconds so
        it never runs on the hot path under high throughput."""
        if current_timestamp - self._last_idle_tick < self.config.alert.idle_eval_seconds:
            return
        self._last_idle_tick = current_timestamp
        try:
            self._evaluate_idle_alerting_groups(current_timestamp)
        except Exception as exc:  # noqa: BLE001
            # Idle-tick is enrichment; a failure here must not crash the poll
            # loop or block checkpointing.
            logger.warning("idle alert tick failed: %s", exc)

    def _update_template_metrics(self, service: str) -> None:
        svc = service or "unknown"
        count = self.template_registry.count_by_service(svc)
        self.metrics.set_template_count(svc, count)

    # --- Elasticsearch index selection -----------------------------------------

    def _poll_indices(self) -> Tuple[list, list]:
        """One batch per selected concrete index: ([(index, batch, cursor)],
        [exceptions]). Legacy mode polls the configured index once (index None).
        An index that fails is recorded and skipped; the others still run."""
        active = getattr(self, "_active_indices", None)
        polled, failures = [], []
        for index in ([None] if active is None else active):
            try:
                if index is None:
                    batch, cursor = self.collector.poll_batch()
                else:
                    batch, cursor = self.collector.poll_batch(index)
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
                if index is not None:
                    self._note_index_poll(index, [], str(exc)[:300])
                continue
            polled.append((index, batch, cursor))
            if index is not None:
                self._note_index_poll(index, batch, None)
        return polled, failures

    def _note_index_poll(self, index: str, batch: list, error: Optional[str]) -> None:
        with self._index_lock:
            progress = self._index_progress.setdefault(
                index, {"last_poll_at": None, "last_event_ts": None, "events_total": 0}
            )
            progress["last_poll_at"] = time.time()
            progress["error"] = error
            if batch:
                progress["events_total"] += len(batch)
                progress["last_event_ts"] = max(
                    progress["last_event_ts"] or 0.0, batch[-1].timestamp
                )

    def _refresh_index_selection(self) -> bool:
        """Apply the web's index selection: on a change (or every
        index_refresh_seconds) resolve patterns to concrete indices, start new
        ones at their floor and forget removed ones. True when applied."""
        store = getattr(self, "index_selection", None)
        if store is None:
            return False
        mtime = store.mtime_ns()
        changed = mtime != self._selection_mtime
        due = (
            time.monotonic() - self._last_resolve_at
            >= self.config.elasticsearch.index_refresh_seconds
        )
        if not changed and not (due and self._active_indices is not None):
            return False
        self._selection_mtime = mtime
        self._last_resolve_at = time.monotonic()
        selection = store.load()
        if selection is None:
            return False  # never saved on the web: keep the configured index

        entries = selection["entries"]
        patterns = [entry["pattern"] for entry in entries]
        try:
            resolved = self.collector.resolve(patterns)
            self._resolve_error = None
        except Exception as exc:  # noqa: BLE001 - keep reading what is known
            logger.warning("Unable to resolve Elasticsearch indices: %s", exc)
            self._resolve_error = str(exc)[:300]
            resolved = {
                pattern: self._resolved.get(pattern, [] if "*" in pattern else [pattern])
                for pattern in patterns
            }
        active = sorted({index for indices in resolved.values() for index in indices})

        known = set(self.checkpoint.index_names())
        legacy_floor = self.checkpoint.legacy_floor()
        floors: dict[str, float] = {}
        for entry in entries:
            for index in resolved.get(entry["pattern"], []):
                if index in known:
                    continue
                # From now: an index first seen under an entry starts at the
                # entry's added_at. Switching from the legacy cursor starts no
                # later than where it stopped, so nothing in between is lost.
                floor_ts = entry["added_at"]
                if legacy_floor is not None:
                    floor_ts = min(floor_ts, legacy_floor)
                floors[index] = min(floors.get(index, floor_ts), floor_ts)
        dropped = known - set(active)

        if active != self._active_indices or floors or dropped:
            # Buffered work and cursors belong to the previous selection.
            if self._pending_predictions or self._pending_cursors:
                self._flush_batch()
            self.checkpoint.set_index_floors(floors, drop=dropped)
            with self._index_lock:
                for index in dropped:
                    self._index_progress.pop(index, None)
            if active != self._active_indices:
                logger.info(
                    "Reading %d Elasticsearch index(es) for %d selected pattern(s): %s",
                    len(active), len(patterns), ", ".join(active) or "none",
                )
        self._active_indices = active
        self._resolved = resolved
        self._selection_entries = entries
        self._applied_selection_revision = selection["revision"]
        return True

    def _publish_index_status(self) -> None:
        """Write what the engine reads (and can read) for the web; the
        available-index list is refreshed every index_refresh_seconds."""
        if getattr(self, "index_selection", None) is None:
            return
        if (
            time.monotonic() - self._last_index_list_at
            >= self.config.elasticsearch.index_refresh_seconds
        ):
            self._last_index_list_at = time.monotonic()
            try:
                self._available_indices = self.collector.list_indices()
                self._available_error = None
            except Exception as exc:  # noqa: BLE001 - shown on the web page
                self._available_error = str(exc)[:300]
        legacy = self._active_indices is None
        with self._index_lock:
            progress = {index: dict(value) for index, value in self._index_progress.items()}
        payload = {
            "mode": "configured" if legacy else "selected",
            "selection_revision": (
                None if legacy else self._applied_selection_revision
            ),
            "configured_revision": selection_revision(self._selection_entries),
            "entries": self._selection_entries,
            "resolved": None if legacy else self._resolved,
            "active_indices": None if legacy else self._active_indices,
            "indices": progress,
            "available": self._available_indices,
            "error": self._resolve_error or self._available_error,
        }
        if payload == self._last_index_status:
            return
        try:
            write_status(self._index_status_path, {**payload, "updated_at": time.time()})
            self._last_index_status = payload
        except OSError as exc:
            logger.warning("Unable to write index status: %s", exc)

    # --- LLM profile ---------------------------------------------------------

    def _llm_enabled(self) -> bool:
        classifier = getattr(self, "incident_classifier", None)
        return classifier is not None and classifier.enabled

    CONTROL_INTERVAL_SECONDS = 2.0
    TEMPLATE_ACTIVITY_SAVE_SECONDS = 300.0

    def _control_tick(self) -> None:
        self._check_retrain()
        self._refresh_llm_profile()
        self._process_analysis_requests()
        self._heartbeat_grouping()
        self._save_template_activity()
        self._publish_index_status()

    # --- retrain (web "Retrain" page) -------------------------------------------

    def _check_retrain(self) -> None:
        """Decide whether a retrain is due; the poll loop runs it. Missed
        scheduled runs (engine down at the time) are skipped, like cron."""
        if getattr(self, "retrain_store", None) is None or self._retrain_trigger is not None:
            return
        loaded = self.retrain_store.load()
        schedule, request = loaded["schedule"], loaded["run_request"]
        status = self.retrain_store.load_status()
        if request and float(request.get("requested_at") or 0) > float(
            status.get("handled_request_at") or 0
        ):
            self._retrain_trigger = {"trigger": "manual", **request}
            return
        upcoming = next_run_at(
            schedule, max(self._started_at, float(status.get("started_at") or 0))
        )
        if upcoming is not None and upcoming <= time.time():
            self._retrain_trigger = {
                "trigger": "schedule",
                "lookback_hours": schedule["lookback_hours"],
                "max_docs": schedule["max_docs"],
            }
            return
        if status.get("next_run_at") != upcoming:
            self.retrain_store.write_status({**status, "next_run_at": upcoming})

    def _run_retrain(self, trigger: dict) -> None:
        """Pause, train in this process, then re-exec so every registry, the
        model and Drain3 state reload from disk. Logs that arrive meanwhile are
        read from the per-index checkpoint after the restart."""
        from logai.training.train_pipeline import run_training_from_elasticsearch

        status = self.retrain_store.load_status()
        started = time.time()
        run = {
            "trigger": trigger["trigger"],
            "started_at": started,
            "lookback_hours": trigger["lookback_hours"],
            "max_docs": trigger["max_docs"],
        }
        status.update(run, state="running", heartbeat_at=started, finished_at=None, error=None)
        if trigger["trigger"] == "manual":
            status["handled_request_at"] = trigger["requested_at"]
        status_lock = threading.Lock()
        self.retrain_store.write_status(status)
        logger.info("Retrain (%s) starting: lookback=%.1fh max_docs=%d",
                    run["trigger"], run["lookback_hours"], run["max_docs"])

        done = threading.Event()

        def heartbeat() -> None:  # keeps healthcheck.py ready while training
            while not done.wait(10):
                with status_lock:
                    status["heartbeat_at"] = time.time()
                    self.retrain_store.write_status(status)

        threading.Thread(target=heartbeat, name="retrain-heartbeat", daemon=True).start()
        try:
            if self._pending_predictions or self._pending_cursors:
                self._flush_batch()
            self._save_template_activity(force=True)
            control_stop = getattr(self, "_control_stop", None)
            if control_stop is not None:
                control_stop.set()
            for worker in (self.documentation_worker, self.incident_classifier):
                if worker is not None:
                    worker.stop()
            snapshot_artifacts(self.config)
            run_training_from_elasticsearch(
                self.config,
                lookback_seconds=run["lookback_hours"] * 3600,
                max_docs=run["max_docs"],
            )
            run.update(result="succeeded", **self._retrain_summary(started))
        except Exception as exc:  # noqa: BLE001 - recorded, then the engine restarts
            logger.exception("Retrain failed; restoring the artifacts from before it")
            run.update(result="failed", error=str(exc)[:500], rolled_back=self._restore())
        finally:
            done.set()
            run["duration_seconds"] = time.time() - started
            with status_lock:
                status.update(
                    state=run["result"], finished_at=time.time(),
                    error=run.get("error"), heartbeat_at=time.time(),
                    history=[*status.get("history", []), run],
                )
                self.retrain_store.write_status(status)
        logger.info("Retrain %s in %.0fs; restarting the engine", run["result"], run["duration_seconds"])
        # Always restart: after success to load the new artifacts, after a
        # failure to reload the restored ones.
        # ponytail: process re-exec instead of in-place reload; add hot reload
        # only if restart cost matters
        self._reexec()

    def _retrain_summary(self, started: float) -> dict:
        base = self.config.storage.base_dir
        lineage = JSONStore(f"{base}/{self.config.storage.group_lineage_file}").all()
        summary = {
            "templates": len(JSONStore(f"{base}/{self.config.storage.template_registry_file}").all()),
            "groups": len(JSONStore(f"{base}/{self.config.storage.group_registry_file}").all()),
        }
        if float(lineage.get("trained_at") or 0) >= started:
            added = lineage.get("added") or {}
            new = lineage.get("new") or {}
            summary["lineage"] = {
                "added_templates": sum(len(t) for t in added.values()),
                "grown_groups": len(added),
                "new_groups": len(new),
                "new_group_templates": sum(len(t) for t in new.values()),
            }
        return summary

    def _restore(self) -> bool:
        try:
            restored = restore_artifacts(self.config)
        except Exception:  # noqa: BLE001 - reported in the history entry
            logger.exception("Restoring the pre-retrain artifacts failed")
            return False
        if not restored:
            logger.warning("No complete pre-retrain backup; artifacts left as they are")
        return restored

    def _recover_interrupted_retrain(self) -> None:
        """A status still "running" at startup means the engine stopped while
        retraining: restore the pre-retrain artifacts and record a failure."""
        status = self.retrain_store.load_status()
        if status.get("state") != "running":
            return
        last = float(status.get("heartbeat_at") or status.get("started_at") or 0)
        rolled_back = self._restore()
        logger.warning(
            "The engine stopped during a retrain (last heartbeat %s); %s",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)),
            "restored the artifacts from before it" if rolled_back else "no backup to restore",
        )
        run = {
            key: status.get(key)
            for key in ("trigger", "started_at", "lookback_hours", "max_docs")
        }
        run.update(
            result="failed", interrupted=True, rolled_back=rolled_back,
            duration_seconds=max(0.0, last - float(status.get("started_at") or last)),
            error="The engine stopped while retraining"
            + ("; the previous version was restored" if rolled_back else ""),
        )
        status.update(
            state="failed", finished_at=time.time(), error=run["error"],
            history=[*status.get("history", []), run],
        )
        self.retrain_store.write_status(status)

    def _record_missed_retrains(self) -> None:
        """Scheduled runs that fell while the engine was down are skipped (like
        cron) but recorded, so the history shows them."""
        loaded = self.retrain_store.load()
        schedule = loaded["schedule"]
        if not schedule["enabled"]:
            return
        status = self.retrain_store.load_status()
        grouping_status = read_json(
            f"{self.config.storage.base_dir}/{self.config.storage.grouping_status_file}"
        )
        # Last moment the engine is known to have been up (or the schedule saved).
        since = max(
            float(value or 0) for value in (
                grouping_status.get("last_heartbeat_at"), status.get("heartbeat_at"),
                status.get("finished_at"), status.get("missed_checked_until"),
                loaded.get("updated_at"),
            )
        )
        if since <= 0:
            return  # first start: nothing could have been missed
        now = time.time()
        missed = []
        at = since
        while len(missed) < 20 and (at := next_run_at(schedule, at)) is not None and at <= now:
            missed.append({
                "trigger": "schedule", "result": "missed", "started_at": at,
                "lookback_hours": schedule["lookback_hours"], "max_docs": schedule["max_docs"],
                "duration_seconds": 0,
                "error": "The engine was not running at the scheduled time",
            })
        status["missed_checked_until"] = now
        if missed:
            logger.warning("%d scheduled retrain(s) were missed while the engine was down", len(missed))
            status["history"] = [*status.get("history", []), *missed]
        self.retrain_store.write_status(status)

    @staticmethod
    def _reexec() -> None:
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _save_template_activity(self, force: bool = False) -> None:
        """Persist per-template baselines every few minutes (and on shutdown);
        losing them only means the baseline rebuilds."""
        if getattr(self, "template_activity", None) is None:
            return
        if not force and time.monotonic() - self._template_activity_saved_at < (
            self.TEMPLATE_ACTIVITY_SAVE_SECONDS
        ):
            return
        self._template_activity_saved_at = time.monotonic()
        try:
            self.template_activity.save(self._template_activity_path)
        except OSError as exc:
            logger.warning("Unable to save template activity: %s", exc)

    def _control_loop(self) -> None:
        """Runs until run_forever's poll loop exits."""
        while not self._control_stop.wait(self.CONTROL_INTERVAL_SECONDS):
            try:
                self._control_tick()
            except Exception as exc:  # noqa: BLE001 - keep the control timer alive
                logger.warning("Engine control tick failed: %s", exc)

    def _record_es_error(self, exc: BaseException) -> None:
        self._es_error = str(exc)[:300]
        if getattr(self, "_es_error_since", None) is None:
            self._es_error_since = time.time()

    def _record_es_ok(self) -> None:
        self._es_error = None
        self._es_error_since = None

    def _llm_status(self) -> tuple[str, str, Optional[float]]:
        """(status, reason, since) explaining whether LLM analysis can run:
        status is ok | disabled | error; never contains the API key."""
        classifier = getattr(self, "incident_classifier", None)
        if not self._llm_enabled():
            profile_error = getattr(self, "_llm_profile_error", None)
            if profile_error:
                return "disabled", f"{profile_error}; no server default LLM is configured", None
            return (
                "disabled",
                "No active LLM profile and no server default (LOGAI_LLM_ENDPOINT); "
                "choose a profile on the LLM profiles page",
                None,
            )
        es_error = getattr(self, "_es_error", None)
        if es_error:
            return (
                "error",
                f"Cannot reach Elasticsearch, so no new logs are analyzed: {es_error}",
                getattr(self, "_es_error_since", None),
            )
        if classifier.last_error and classifier.last_error_at >= classifier.last_ok_at:
            return "error", f"Last LLM call failed: {classifier.last_error}", classifier.last_error_at
        name = getattr(self, "_llm_profile_name", None)
        source = f"profile {name!r}" if name else "the server default"
        return "ok", f"Using {source} ({classifier.config.model or 'no model set'})", None

    def _refresh_llm_profile(self) -> None:
        """Apply the web's active LLM profile (or fall back to env config)
        without a restart. A broken profile file keeps the env config."""
        classifier = getattr(self, "incident_classifier", None)
        if classifier is None:
            return
        try:
            profile = self._llm_profiles.active_profile()
        except Exception as exc:  # noqa: BLE001 - never raise into the poll loop
            # Logged once per distinct error; this runs every poll loop.
            if str(exc) != getattr(self, "_llm_profile_error", None):
                logger.warning("Unable to read LLM profiles, using env config: %s", exc)
            self._llm_profile_error = str(exc)
            profile = None
        else:
            self._llm_profile_error = None
        target = self._env_llm_config
        if profile is not None:
            target = dataclasses.replace(
                self._env_llm_config,
                endpoint=profile.get("endpoint", ""),
                api_key=profile.get("api_key") or None,
                model=profile.get("model", ""),
            )
        profile_id = profile.get("id") if profile else None
        self._llm_profile_name = profile.get("name") if profile else None
        if target != classifier.config or profile_id != self._llm_profile_id:
            logger.info(
                "LLM source: %s",
                f"profile {profile.get('name')!r} ({target.model})" if profile else
                ("env config" if target.endpoint else "disabled"),
            )
            classifier.config = target
            self._llm_profile_id = profile_id

    # --- on-demand analysis requests -----------------------------------------

    def _params_snapshot(self, window_filter) -> dict:
        with self._params_lock:
            return {key: list(dq) for key, dq in self._recent_params.items() if window_filter(key)}

    def _analysis_store(self, kind: str):
        classifier = self.incident_classifier
        return {"window": classifier.store, "service": classifier.service_store,
                "template": classifier.template_store}[kind]

    @staticmethod
    def _store_key(kind: str, target_id: str) -> str:
        if kind == "window":
            return group_id_key(tuple(json.loads(target_id)))
        return target_id

    def _process_analysis_requests(self) -> None:
        """Apply each web request (analyze or delete, for a window, service or
        template) exactly once. An analyze request is new when it is newer
        than both what this process handled and the stored record's
        requested_at (restart-safe). Deletes apply even while LLM is disabled.
        A request that cannot run gets a failed record so the web never waits.
        Never raises.
        """
        classifier = getattr(self, "incident_classifier", None)
        if classifier is None:
            return
        enabled = self._llm_enabled()
        # The file is tiny; reading it every tick avoids mtime-granularity misses.
        for key, (action, requested_at, language) in load_requests(
            self._analysis_requests_path
        ).items():
            parsed = parse_key(key)
            if parsed is None or requested_at <= self._handled_requests.get(key, 0.0):
                continue
            kind, target_id = parsed
            try:
                store = self._analysis_store(kind)
                store_key = self._store_key(kind, target_id)
                if action == "delete":
                    classifier.delete_record(kind, target_id)
                    self._handled_requests[key] = requested_at
                    continue
                if not enabled:
                    continue  # web blocks new analyze requests while disabled
                record = (store.get(store_key) if store is not None else None) or {}
                if requested_at <= float(record.get("requested_at") or 0.0):
                    self._handled_requests[key] = requested_at
                    continue
                if self._submit_request(kind, target_id, requested_at, language):
                    self._handled_requests[key] = requested_at
            except Exception as exc:  # noqa: BLE001 - one bad request must not block others
                logger.warning("LLM request %s failed: %s", key, exc)
                self._handled_requests[key] = requested_at
                self._write_failed(kind, target_id, requested_at, str(exc))

    def _submit_request(
        self, kind: str, target_id: str, requested_at: float,
        language: str = DEFAULT_LANGUAGE,
    ) -> bool:
        classifier = self.incident_classifier
        # Everything is measured at the request time on the event clock, so a
        # replay at any speed gets the same numbers as live traffic.
        now = self._now_event_time()
        if kind == "window":
            identity = tuple(json.loads(target_id))
            if len(identity) != 2 or self.group_registry.get(identity[1]) is None:
                raise LookupError(f"Unknown window {target_id}")
            state = self.alert_sm._load(identity)
            # `at` (last scored event) anchors template ages.
            rate = self.feature_engine.describe(identity, now)
            alert = {
                "state": state.alert_state, "score": state.anomaly_score,
                "at": state.timestamp or now,
                "count_1m": rate["count_1m"] if rate else None, "rate": rate,
            }
            params = self._params_snapshot(lambda key: key == identity).get(identity, [])
            activity = self.template_activity.counts(now, identity[0])
            signature = service_signature(
                identity[0], self.template_registry,
                self.alert_sm.states_for_service(identity[0]), activity, now,
            )
            return classifier.submit(
                identity, alert, params, requested_at=requested_at, language=language,
                context={"activity": activity, "signature": signature["templates"]},
            )
        if kind == "service":
            alert_states = self.alert_sm.states_for_service(target_id)
            if self.template_registry.count_by_service(target_id) == 0 and not alert_states:
                raise LookupError(f"Unknown service {target_id!r}")
            params = {g: entries for (_, g), entries in
                      self._params_snapshot(lambda key: key[0] == target_id).items()}
            activity = self.template_activity.counts(now, target_id)
            signature = service_signature(
                target_id, self.template_registry, alert_states, activity, now
            )
            evidence, candidates, sent_groups = build_service_evidence(
                target_id, self.group_registry, self.template_registry,
                alert_states, params, self.doc_matcher,
                load_cases(classifier.cases_path) if classifier.cases_path else (),
                activity, signature["templates"],
            )
            return classifier.submit_service(
                target_id, requested_at, evidence, candidates, sent_groups, language,
                {**signature, "occurred_at": now},
            )
        evidence, candidates = build_template_evidence(
            target_id, self.template_registry, self.group_registry,
            self.template_activity.counts(now),
        )
        return classifier.submit_template(
            target_id, requested_at, evidence, candidates, language
        )

    def _write_failed(self, kind: str, target_id: str, requested_at: float, error: str) -> None:
        classifier = self.incident_classifier
        try:
            if kind == "window":
                identity = tuple(json.loads(target_id))
                record = {**classifier._failed(identity, error), "requested_at": requested_at}
            elif kind == "service":
                record = classifier._failed_service(target_id, requested_at, error)
            else:
                record = classifier._failed_template(target_id, requested_at, error)
            store = self._analysis_store(kind)
            if store is not None:
                store.set(self._store_key(kind, target_id), record)
        except Exception:  # noqa: BLE001
            logger.exception("Unable to persist failed LLM request for %s:%s", kind, target_id)

    # --- grouping revision activation ---------------------------------------

    def _heartbeat_grouping(self) -> None:
        now = time.time()
        if (
            now - self._last_grouping_heartbeat
            < self.config.grouping.heartbeat_interval_seconds
        ):
            return
        self.grouping_store.update_heartbeat(now, runtime={
            "llm_enabled": self._llm_enabled(),
            "llm_profile_id": getattr(self, "_llm_profile_id", None),
            **dict(zip(("llm_status", "llm_reason", "llm_reason_since"), self._llm_status())),
            "last_successful_poll_at": self._last_successful_poll_at,
            "last_processed_event_at": self._last_processed_event_at,
            "last_checkpoint_commit_at": self._last_checkpoint_commit_at,
        })
        self._last_grouping_heartbeat = now

    @staticmethod
    def _derive_grouping_state(results: dict[str, dict[str, Any]]) -> str:
        if any(
            value.get("state") == "unresolved" and not value.get("retryable", False)
            for value in results.values()
        ):
            return "failed"
        applied = sum(value.get("state") == "applied" for value in results.values())
        unresolved = sum(
            value.get("state") == "unresolved" for value in results.values()
        )
        if unresolved and applied:
            return "partial"
        if unresolved:
            return "pending"
        return "applied"

    def _refresh_grouping_if_needed(self) -> bool:
        # Some embedders/tests construct a lightweight pipeline object without
        # calling __init__. Grouping is optional for that compatibility path.
        if not hasattr(self, "grouping_store"):
            return False
        self._heartbeat_grouping()
        try:
            if not self._grouping_initialized:
                snapshot = self.grouping_store.load_overrides()
                self._grouping_initialized = True
            else:
                generation_changed = (
                    self.template_registry.mutation_generation
                    != self._last_grouping_template_generation
                )
                if self._grouping_retry_needed and generation_changed:
                    snapshot = self.grouping_store.cached_overrides()
                else:
                    snapshot = self.grouping_store.load_if_changed()
            if snapshot is None:
                return False
        except GroupingStoreError as exc:
            logger.error("Invalid grouping override: %s", exc)
            self.metrics.logai_pipeline_errors_total.labels(
                stage="grouping", reason_code="invalid_override_schema"
            ).inc()
            self.grouping_store.write_status({
                "applied_revision": None,
                "attempted_revision": None,
                "last_attempt_at": time.time(),
                "last_applied_at": None,
                "last_heartbeat_at": time.time(),
                "state": "failed",
                "error": {
                    "reason_code": "invalid_override_schema",
                    "message": str(exc)[:500],
                    "retryable": False,
                },
                "results": {},
                "unresolved": {},
            })
            return False

        started = time.monotonic()
        previous = self.grouping_store.load_status()
        attempt_at = time.time()
        pending_status = {
            "applied_revision": previous.get("applied_revision"),
            "attempted_revision": snapshot["revision"],
            "last_attempt_at": attempt_at,
            "last_applied_at": previous.get("last_applied_at"),
            "last_heartbeat_at": attempt_at,
            "state": "pending",
            "error": None,
            "results": previous.get("results", {}),
            "unresolved": previous.get("unresolved", {}),
            "runtime": previous.get("runtime", {}),
        }
        self.grouping_store.write_status(pending_status)
        self.metrics.set_grouping_revision(snapshot["revision"], "pending")

        # Every buffered vector and real cursor belongs to the pre-change map.
        if self._pending_predictions or self._pending_cursors:
            self._flush_batch()
        try:
            outcome = self.grouping_manager.apply_realtime(snapshot)
            for group_id in outcome.deleted_groups:
                self.feature_engine.drop_group(group_id)
                self.alert_sm.drop_group(group_id)
                self.metrics.drop_group(group_id)
                if getattr(self, "incident_classifier", None) is not None:
                    self.incident_classifier.drop_group(group_id)
                    with self._params_lock:
                        for key in [k for k in self._recent_params if k[1] == group_id]:
                            del self._recent_params[key]
            if outcome.resolution.affected_groups:
                self.documentation_worker.invalidate_groups(
                    outcome.resolution.affected_groups
                )

            results = outcome.resolution.results
            state = self._derive_grouping_state(results)
            nonretryable = next(
                (
                    value for value in results.values()
                    if value.get("state") == "unresolved"
                    and not value.get("retryable", False)
                ),
                None,
            )
            applied_revision = (
                snapshot["revision"]
                if state == "applied"
                else previous.get("applied_revision")
            )
            status = {
                "applied_revision": applied_revision,
                "attempted_revision": snapshot["revision"],
                "last_attempt_at": attempt_at,
                "last_applied_at": (
                    time.time() if state == "applied" else previous.get("last_applied_at")
                ),
                "last_heartbeat_at": time.time(),
                "state": state,
                "error": (
                    {
                        "reason_code": nonretryable.get("reason_code"),
                        "message": nonretryable.get("message", "Grouping assignment failed"),
                        "retryable": False,
                    }
                    if nonretryable else None
                ),
                "results": results,
                "unresolved": outcome.resolution.unresolved,
                "runtime": previous.get("runtime", {}),
            }
            self.grouping_store.write_status(status)
            self.metrics.set_grouping_revision(snapshot["revision"], state)
            self._grouping_retry_needed = any(
                value.get("state") == "unresolved" and value.get("retryable", False)
                for value in results.values()
            )
            self._last_grouping_template_generation = (
                self.template_registry.mutation_generation
            )
            reason = "none" if state == "applied" else state
            self.metrics.logai_grouping_apply_total.labels(
                result=state, reason_code=reason
            ).inc()
            return True
        except GroupAssignmentError as exc:
            logger.error("Grouping revision %s failed: %s", snapshot["revision"], exc)
            self.metrics.logai_pipeline_errors_total.labels(
                stage="grouping", reason_code=exc.reason_code
            ).inc()
            if not exc.rollback_succeeded:
                # A restart must reconcile the pending durable marker before any
                # further event can observe a partially persisted mapping.
                raise
            self.grouping_store.write_status({
                **pending_status,
                "last_heartbeat_at": time.time(),
                "state": "failed",
                "error": {
                    "reason_code": exc.reason_code,
                    "message": str(exc)[:500],
                    "retryable": False,
                },
                "results": {},
                "unresolved": {},
            })
            self.metrics.set_grouping_revision(snapshot["revision"], "failed")
            self.metrics.logai_grouping_apply_total.labels(
                result="failed", reason_code=exc.reason_code
            ).inc()
            return False
        finally:
            self.metrics.logai_grouping_apply_duration_seconds.observe(
                time.monotonic() - started
            )

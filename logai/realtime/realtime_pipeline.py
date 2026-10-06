"""Realtime pipeline (plan section 4):

  Collect -> Parse -> Assign(Known/Unknown) -> Match(doc) -> Aggregate
  (features) -> Predict (anomaly) -> Alert State -> Export (Prometheus)

No retraining / re-clustering happens here - unresolved ("Unknown/Pending")
templates just wait for the next training run.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Optional, Tuple, Union

from logai.config import AppConfig
from logai.alert.alert_state_machine import AlertStateMachine
from logai.anomaly.isolation_forest_model import GlobalAnomalyModel, GroupAnomalyModels
from logai.clustering.hdbscan_cluster import GroupClusterer
from logai.collector.es_collector import ElasticsearchCollector
from logai.docmatch.doc_matcher import DocumentationMatcher
from logai.docmatch.refresh_worker import DocumentationRefreshWorker
from logai.embedding.embedder import TemplateEmbedder
from logai.features.feature_engine import FeatureEngine
from logai.grouping import PENDING_GROUP_ID
from logai.grouping.assignment_manager import (
    GroupAssignmentError,
    GroupAssignmentManager,
)
from logai.incident.classifier import IncidentClassifier
from logai.incident.requests import load_requests
from logai.incident.service_analysis import build_service_evidence
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
from logai.storage.registries import GroupRegistry, TemplateRegistry

logger = logging.getLogger("logai.realtime")

# Recent extracted template parameters kept per (service, group) window as
# evidence for the LLM incident analysis.
RECENT_PARAMS_PER_WINDOW = 200

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
        # run on a background thread. Disabled while no endpoint is configured.
        self.incident_classifier: Optional[IncidentClassifier] = None
        if config.llm.endpoint:
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
            )
        # On-demand whole-service analysis requests (web-owned file).
        self._analysis_requests_path = (
            f"{config.storage.base_dir}/{config.storage.analysis_requests_file}"
        )
        self._analysis_requests: dict[str, float] = {}
        self._analysis_requests_mtime: Optional[int] = None
        self._handled_requests: dict[str, float] = {}
        self._recent_params: dict[Tuple[str, str], deque] = {}
        # Windows already analyzed in their current episode. COOLING is still
        # the same episode, so a restart never re-triggers an ongoing alert.
        self._alert_episodes: set[Tuple[str, str]] = self.alert_sm.keys_in_states(
            {"ALERTING", "COOLING"}
        )

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
        self._pending_cursor: Optional[Any] = None
        self._pending_last_ts: Optional[float] = None
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
        logger.info("Realtime pipeline started, polling Elasticsearch...")
        consecutive_poll_failures = 0
        MAX_POLL_BACKOFF = 300.0  # 5 minutes

        while True:
            self._refresh_grouping_if_needed()
            self._process_analysis_requests()
            # ── Phase 1: Poll ES ──────────────────────────────────────────
            try:
                batch, cursor = self.collector.poll_batch()
                consecutive_poll_failures = 0
                self._last_successful_poll_at = time.time()
                self.metrics.logai_engine_heartbeat_timestamp_seconds.set(
                    self._last_successful_poll_at
                )
                self.metrics.logai_last_successful_poll_timestamp_seconds.set(
                    self._last_successful_poll_at
                )
            except Exception as exc:  # noqa: BLE001
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

            # A command accepted while the ES request was in flight applies to
            # the fetched batch as a whole, before its first event is processed.
            self._refresh_grouping_if_needed()

            # ── Phase 2: Accumulate batch into the predict buffer ─────────
            # Only parse/group/feature-extract/append here; scoring happens at
            # the flush boundary so 500 events become one vectorized inference.
            if batch:
                self.metrics.logai_queue_depth.set(len(batch))
                self.metrics.logai_events_received_total.inc(len(batch))
                try:
                    for raw in batch:
                        if not self._process_one(raw):
                            raise RuntimeError(
                                f"Event {raw.event_id} did not reach a terminal state"
                            )
                    # Cursor advances only after a successful flush (Phase 5),
                    # never here - a crash before commit replays idempotently.
                    self._pending_cursor = cursor
                    self._pending_last_ts = batch[-1].timestamp
                    self._event_clock = max(self._event_clock, batch[-1].timestamp)
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
                or (bool(batch) and self._pending_cursor is not None)
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
        flush -> dedup gc -> checkpoint.commit. The cursor advances only when a
        real stream batch contributed to this flush (`_pending_cursor` set); an
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

        if self._pending_cursor is not None:
            try:
                self.checkpoint.commit(self._pending_cursor, self._pending_last_ts)
            except Exception:
                self.metrics.logai_pipeline_errors_total.labels(
                    stage="checkpoint", reason_code="checkpoint_write_failed"
                ).inc()
                raise
            self._last_checkpoint_commit_at = time.time()
            self.metrics.logai_last_checkpoint_commit_timestamp_seconds.set(
                self._last_checkpoint_commit_at
            )

        self._pending_cursor = None
        self._pending_last_ts = None
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
        self._track_alert_episodes(scored, states)

    def _track_alert_episodes(
        self, scored: list[AnomalyResult], states: list[AnomalyState]
    ) -> None:
        """Submit an LLM analysis when a window enters a new ALERTING episode.

        `states` is aligned 1:1 with `scored` (transition_batch keeps order).
        An episode ends only at NORMAL; COOLING -> ALERTING does not re-trigger.
        """
        classifier = getattr(self, "incident_classifier", None)
        if classifier is None:
            return
        for result, state in zip(scored, states):
            key = tuple(state.group_id)
            if state.alert_state == AlertStateEnum.NORMAL.value:
                self._alert_episodes.discard(key)
            elif (
                state.alert_state == AlertStateEnum.ALERTING.value
                and key not in self._alert_episodes
            ):
                self._alert_episodes.add(key)
                classifier.submit(
                    key,
                    {
                        "state": state.alert_state,
                        "score": result.anomaly_score,
                        "count_1m": result.count_1m,
                    },
                    list(self._recent_params.get(key, ())),
                )

    def _process_one(self, raw: RawLog) -> bool:
        start = time.time()
        try:
            if self.dedup.seen(raw.event_id):
                return True  # idempotency: already processed this event_id

            parsed = self.parser.parse(raw)
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

    # --- on-demand service analysis -----------------------------------------

    def _process_analysis_requests(self) -> None:
        """Submit each web request for a whole-service analysis exactly once.

        A request is new when its requested_at is newer than both what this
        process already handled and the stored record's requested_at (the
        latter keeps it restart-safe). Never raises into the poll loop.
        """
        classifier = getattr(self, "incident_classifier", None)
        if classifier is None or classifier.service_store is None:
            return
        try:
            try:
                mtime = os.stat(self._analysis_requests_path).st_mtime_ns
            except FileNotFoundError:
                return
            if mtime != self._analysis_requests_mtime:
                self._analysis_requests = load_requests(self._analysis_requests_path)
                self._analysis_requests_mtime = mtime
            for service, requested_at in self._analysis_requests.items():
                record = classifier.service_store.get(service) or {}
                if requested_at <= max(
                    self._handled_requests.get(service, 0.0),
                    float(record.get("requested_at") or 0.0),
                ):
                    continue
                alert_states = self.alert_sm.states_for_service(service)
                if self.template_registry.count_by_service(service) == 0 and not alert_states:
                    logger.info("Ignoring analysis request for unknown service %r", service)
                    self._handled_requests[service] = requested_at
                    continue
                evidence, candidates, sent_groups = build_service_evidence(
                    service,
                    self.group_registry,
                    self.template_registry,
                    alert_states,
                    {g: list(dq) for (s, g), dq in self._recent_params.items() if s == service},
                    self.doc_matcher,
                )
                if classifier.submit_service(
                    service, requested_at, evidence, candidates, sent_groups
                ):
                    self._handled_requests[service] = requested_at
        except Exception as exc:  # noqa: BLE001 - enrichment must not stop the loop
            logger.warning("Service analysis request handling failed: %s", exc)

    # --- grouping revision activation ---------------------------------------

    def _heartbeat_grouping(self) -> None:
        now = time.time()
        if (
            now - self._last_grouping_heartbeat
            < self.config.grouping.heartbeat_interval_seconds
        ):
            return
        self.grouping_store.update_heartbeat(now, runtime={
            "llm_enabled": getattr(self, "incident_classifier", None) is not None,
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
        if self._pending_predictions or self._pending_cursor is not None:
            self._flush_batch()
        try:
            outcome = self.grouping_manager.apply_realtime(snapshot)
            for group_id in outcome.deleted_groups:
                self.feature_engine.drop_group(group_id)
                self.alert_sm.drop_group(group_id)
                self.metrics.drop_group(group_id)
                if getattr(self, "incident_classifier", None) is not None:
                    self.incident_classifier.drop_group(group_id)
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

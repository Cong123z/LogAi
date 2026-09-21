"""Prometheus metrics (plan section 4.9 / 8).

Exposes:
  Business / log metrics (derived from application logs):
    app_log_events_total{service}
    app_log_errors_total{service, error_code}
    app_log_templates_total{service}

  Analysis metrics:
    log_anomaly_score{group_id, documented}
    log_alert_state{group_id, state}   (1 for the active state, 0 otherwise)
    log_alerts_total{group_id}         (cumulative transitions into ALERTING)

  Engine health metrics:
    logai_events_received_total
    logai_events_processed_total
    logai_events_failed_total
    logai_retry_total
    logai_processing_latency_seconds (Histogram)
    logai_queue_depth (Gauge)
"""
from __future__ import annotations

from typing import Dict, Tuple

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from logai.config import MetricsConfig
from logai.models import AlertStateEnum, AnomalyState, GroupState, ParsedEvent


class MetricsExporter:
    def __init__(self, config: MetricsConfig):
        self.config = config

        # Business / log metrics
        self.app_log_events_total = Counter(
            "app_log_events_total", "Total log events processed", ["service"]
        )
        self.app_log_errors_total = Counter(
            "app_log_errors_total", "Total error-level log events",
            ["service", "error_code"],
        )
        self.app_log_templates_total = Gauge(
            "app_log_templates_total", "Distinct log templates seen", ["service"]
        )

        # Analysis metrics. All three are keyed by BOTH service and group_id:
        # window/alert identity is per-(service, group), so a group shared by
        # several services exposes one series per service. Cardinality therefore
        # scales with (#services x #groups) - see ARCHITECTURE.md 9.1.
        self.log_anomaly_score = Gauge(
            "log_anomaly_score", "Anomaly score for a (service, group) window",
            ["service", "group_id", "documented"],
        )
        self.log_alert_state = Gauge(
            "log_alert_state", "1 if this is the window's current alert state",
            ["service", "group_id", "state"],
        )
        self.log_alerts_total = Counter(
            "log_alerts_total",
            "Cumulative count of transitions into the ALERTING state",
            ["service", "group_id"],
        )
        # Last alert_state seen per (service, group_id) tuple, so we only count a
        # fresh escalation (X -> ALERTING) once instead of re-incrementing on
        # every event that keeps a window in ALERTING.
        self._last_alert_state: Dict[Tuple[str, str], str] = {}
        self._last_documented_label: Dict[Tuple[str, str], str] = {}

        # Engine health metrics
        self.logai_events_received_total = Counter(
            "logai_events_received_total", "Raw log events received from Elasticsearch"
        )
        self.logai_events_processed_total = Counter(
            "logai_events_processed_total", "Events fully processed"
        )
        self.logai_events_failed_total = Counter(
            "logai_events_failed_total", "Events that failed processing (sent to DLQ)"
        )
        self.logai_retry_total = Counter(
            "logai_retry_total", "Retry attempts across all stages"
        )
        self.logai_processing_latency_seconds = Histogram(
            "logai_processing_latency_seconds",
            "End-to-end per-event processing latency",
        )
        self.logai_queue_depth = Gauge(
            "logai_queue_depth", "Events buffered waiting for processing"
        )
        self.logai_es_poll_errors_total = Counter(
            "logai_es_poll_errors_total",
            "ES poll failures (transport errors, auth errors, etc.)",
        )
        self.logai_es_malformed_hits_total = Counter(
            "logai_es_malformed_hits_total",
            "ES hits skipped due to missing/invalid _source or _id",
        )

    def start(self) -> None:
        start_http_server(self.config.http_port, addr=self.config.http_host)

    # --- convenience updaters -------------------------------------------------

    def record_raw_event(self, parsed: ParsedEvent) -> None:
        self.app_log_events_total.labels(service=parsed.raw.service).inc()
        if parsed.raw.level.upper() in ("ERROR", "CRITICAL", "FATAL"):
            error_code = parsed.template_id
            self.app_log_errors_total.labels(
                service=parsed.raw.service, error_code=error_code
            ).inc()

    def set_template_count(self, service: str, count: int) -> None:
        self.app_log_templates_total.labels(service=service).inc(0)
        self.app_log_templates_total.labels(service=service).set(count)

    def set_anomaly_score(
        self, group: GroupState, score: float, service: str
    ) -> None:
        # service is passed explicitly: the GroupState is looked up by the plain
        # group_id (wkey[1]) so group.group_id is NOT the (service, group) tuple.
        key = (service, group.group_id)
        documented = str(group.documented).lower()
        previous = self._last_documented_label.get(key)
        if previous is not None and previous != documented:
            self.log_anomaly_score.remove(service, group.group_id, previous)
        self._last_documented_label[key] = documented
        self.log_anomaly_score.labels(
            service=service,
            group_id=group.group_id,
            documented=documented,
        ).set(score)

    def set_alert_state(self, state: AnomalyState) -> None:
        # state.group_id is a (service, group_id) tuple; unpack once and feed the
        # gauges as strings so no tuple ever reaches a Prometheus label value.
        service, group_id = state.group_id
        alerting = AlertStateEnum.ALERTING.value
        previous = self._last_alert_state.get(state.group_id)
        # Nothing to do when the window stays in the same state: the gauges
        # already hold the correct values and no fresh escalation occurred.
        # Skipping avoids re-writing N gauge series on every event that keeps a
        # window in its current state (hot-path lock/IO under high throughput).
        if previous == state.alert_state:
            return
        # Count only the phase transition into ALERTING, not each event that
        # keeps the window alerting. A recovery (-> NORMAL) followed by a new
        # escalation increments again, which is the intended behaviour.
        if state.alert_state == alerting and previous != alerting:
            self.log_alerts_total.labels(service=service, group_id=group_id).inc()
        self._last_alert_state[state.group_id] = state.alert_state

        for candidate in AlertStateEnum:
            self.log_alert_state.labels(
                service=service, group_id=group_id, state=candidate.value
            ).set(1 if candidate.value == state.alert_state else 0)

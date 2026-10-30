"""Prometheus metrics. Never a per-template series: alert rows only, by service and document."""
from __future__ import annotations

import ctypes

from prometheus_client import CollectorRegistry, Counter, Gauge, ProcessCollector, start_http_server

from logai2.alerts import RANK

LABEL = {"service": "service", "template": "undocumented"}


def rss_mib() -> float:
    """VmRSS of this process in MiB; 0.0 where /proc is unreadable."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def trim() -> None:
    """Hand freed heap back to the OS (glibc); a no-op elsewhere."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


class Metrics:
    def __init__(self, port: int | None):
        # own registry: tests build many Metrics without duplicate-series errors
        self.registry = r = CollectorRegistry()
        self.events = Counter("logai_events_total", "Events read", registry=r)
        self.cache_hits = Counter("logai_cache_hits_total", "Known-line cache hits", registry=r)
        self.cache_misses = Counter("logai_cache_misses_total", "Known-line cache misses", registry=r)
        self.late = Counter("logai_late_counts_total", "Counts for already-closed minutes", registry=r)
        self.lag = Gauge("logai_lag_seconds", "now - watermark", registry=r)
        self.miss_ratio = Gauge("logai_miss_ratio", "Cache miss ratio over 5 min", ["service"], registry=r)
        self.flush_seconds = Gauge("logai_flush_seconds", "Last flush duration", registry=r)
        self.heartbeat = Gauge("logai_engine_heartbeat_timestamp_seconds", "Last flush time", registry=r)
        self.alert_state = Gauge("log_alert_state", "1 for the alert row's current state",
                                 ["service", "doc_id", "state"], registry=r)
        ProcessCollector(registry=r)   # process_resident_memory_bytes etc.
        self._alert_keys: set[tuple[str, str]] = set()
        if port is not None:
            start_http_server(port, registry=r)

    def export_rows(self, rows: list[dict]) -> None:
        worst: dict[tuple[str, str], str] = {}   # undocumented template rows of a service share one series
        for row in rows:
            key = (row["service"], row["doc_id"] if row["kind"] == "document" else LABEL[row["kind"]])
            if RANK[row["state"]] > RANK.get(worst.get(key), -1):
                worst[key] = row["state"]
        # set the new series, then drop stale ones: clear() first would let a scrape see none
        for (service, doc_id), current in worst.items():
            for state in RANK:
                self.alert_state.labels(service, doc_id, state).set(1 if state == current else 0)
        for service, doc_id in self._alert_keys - set(worst):
            for state in RANK:
                self.alert_state.remove(service, doc_id, state)
        self._alert_keys = set(worst)

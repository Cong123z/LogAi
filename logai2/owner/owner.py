"""Owner: Drain3 on misses, open minutes, detection, and the flush transaction.

Everything a flush writes, the ES cursors included, goes in ONE transaction.
The cursor is the start of the oldest open minute, so after a crash every
open minute is re-read whole and every closed minute was written exactly once.
Pending rows are cleared only after the commit; a failed flush keeps them all
and the next flush writes them once.
"""
from __future__ import annotations

import logging
import time
import zlib
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from typing import Callable

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from logai2.config import AppConfig
from logai2.db import get_setting
from logai2.links import ALERTING_SOURCES
from logai2.owner.baseline import Baseline
from logai2.owner.counts import ERROR_LEVELS, SEVERITY, OpenMinutes
from logai2.owner.detect import AlertRow, Detector
from logai2.owner.miners import Miners
from logai2.reader.reader import MinuteBatch

logger = logging.getLogger("logai2.owner")

SAMPLES_KEPT = 20

# ponytail: executemany per table; switch to COPY into temp tables (spec section 4) when flush time shows in the Pipeline stats.
SQL = {
    "services": "INSERT INTO services (service, partition) VALUES (%s, %s) ON CONFLICT DO NOTHING",
    "modules": "INSERT INTO modules (module_code, service) VALUES (%s, %s) ON CONFLICT DO NOTHING",
    "templates": (
        "INSERT INTO templates (service, template_id, level, text, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (service, template_id) DO UPDATE SET "
        "level = EXCLUDED.level, text = EXCLUDED.text, "
        "last_seen = GREATEST(templates.last_seen, EXCLUDED.last_seen), "
        # a changed text makes the vector stale; the linker embeds it again
        "embedding = CASE WHEN templates.text IS DISTINCT FROM EXCLUDED.text THEN NULL ELSE templates.embedding END"),
    # a cache hit carries no text; its template row was inserted by the miss that taught the cache
    "templates_seen": (
        "UPDATE templates SET level = %s, last_seen = GREATEST(last_seen, %s) "
        "WHERE service = %s AND template_id = %s"),
    "drain3_state": (
        "INSERT INTO drain3_state (service, blob) VALUES (%s, %s) ON CONFLICT (service) DO UPDATE SET "
        "blob = EXCLUDED.blob, version = drain3_state.version + 1"),
    "service_hours": (
        "INSERT INTO service_hours (service, day, hour, n_minutes) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (service, day, hour) DO UPDATE SET n_minutes = service_hours.n_minutes + EXCLUDED.n_minutes"),
    "hourly_counts": (
        "INSERT INTO hourly_counts (service, key, day, hour, sum, sumsq) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (service, key, day, hour) DO UPDATE SET "
        "sum = hourly_counts.sum + EXCLUDED.sum, sumsq = hourly_counts.sumsq + EXCLUDED.sumsq"),
    "alert_minutes": (
        "INSERT INTO alert_minutes (service, key, module_code, day, minute, count) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"),
    "alert_state": (
        "INSERT INTO alert_state (service, key, state, since, streak, score, count, band_low, band_high) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (service, key) DO UPDATE SET "
        "state = EXCLUDED.state, since = EXCLUDED.since, streak = EXCLUDED.streak, score = EXCLUDED.score, "
        "count = EXCLUDED.count, band_low = EXCLUDED.band_low, band_high = EXCLUDED.band_high"),
    "alert_changes": (
        "INSERT INTO alert_changes (service, key, at, from_state, to_state, count, band_low, band_high) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"),
    "error_samples": "INSERT INTO error_samples (service, template_id, at, line) VALUES (%s, %s, %s, %s)",
    "error_samples_trim": (
        "DELETE FROM error_samples WHERE ctid IN (SELECT ctid FROM error_samples "
        f"WHERE service = %s AND template_id = %s ORDER BY at DESC OFFSET {SAMPLES_KEPT})"),
    "es_cursors": (
        "INSERT INTO es_cursors (index, since_ms, floor_ms) VALUES (%s, %s, %s) ON CONFLICT (index) DO UPDATE "
        "SET since_ms = EXCLUDED.since_ms, floor_ms = EXCLUDED.floor_ms, updated_at = now()"),
    "worker_status": (
        "INSERT INTO worker_status (worker, status) VALUES (%s, %s) ON CONFLICT (worker) DO UPDATE "
        "SET status = EXCLUDED.status, updated_at = now()"),
}


def _ts(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


def _more_severe(level: str, than: str | None) -> bool:
    return than is None or SEVERITY.get(level.upper(), 0) > SEVERITY.get(than.upper(), 0)


class Owner:
    def __init__(self, conn, cfg: AppConfig, start_minute: int, now: Callable[[], float] = time.time):
        self.conn = conn
        self.cfg = cfg
        self.now = now
        with conn.transaction():
            blobs = {r["service"]: r["blob"] for r in conn.execute("SELECT service, blob FROM drain3_state")}
            self.levels = {(r["service"], r["template_id"]): r["level"]
                           for r in conn.execute("SELECT service, template_id, level FROM templates")}
            # service -> first minute it is zero-filled from; a known service counts from its first
            # template (or from start), so a restart does not zero-fill minutes before it existed
            self.services = {r["service"]: max(start_minute, int(r["first"].timestamp()) // 60)
                             if r["first"] else start_minute for r in conn.execute(
                                 "SELECT s.service, min(t.first_seen) AS first FROM services s "
                                 "LEFT JOIN templates t USING (service) GROUP BY s.service")}
            self.modules = {(r["service"], r["module_code"]) for r in conn.execute("SELECT service, module_code FROM modules")}
            states = {(r["service"], r["key"]): AlertRow(
                r["service"], r["key"], r["state"], r["streak"], r["since"].timestamp(), r["score"],
                r["count"], r["band_low"], r["band_high"]) for r in conn.execute("SELECT * FROM alert_state")}
            self.detection_enabled = get_setting(conn, "detection_enabled") is True
            baseline = Baseline.load(conn)
            links = self._load_links()
        self.miners = Miners(cfg.drain3, blobs)
        self.minutes = OpenMinutes(start_minute)
        self.detector = Detector(cfg, baseline, links, self.levels, states)
        self._unparsed_logged: dict[str, int] = {}  # service -> minute last logged
        # samples of open minutes, queued only when their minute closes: open minutes are
        # re-read after a crash, so queuing earlier would insert their samples twice
        self._open_samples: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
        self._clear()

    def _clear(self) -> None:
        self._services: dict[str, int] = {}
        self._modules: list[tuple[str, str]] = []
        self._templates: dict[tuple[str, str], dict] = {}
        self._blobs: dict[str, bytes] = {}
        self._hours: Counter = Counter()
        self._sums: dict[tuple, list[int]] = {}
        self._alert_minutes: list[tuple] = []
        self._changes: list = []
        self._samples: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=SAMPLES_KEPT))

    def _load_links(self) -> dict[tuple[str, str], str]:
        return {(r["service"], r["template_id"]): r["doc_id"]
                for r in self.conn.execute("SELECT service, template_id, doc_id FROM template_doc_links "
                                           "WHERE doc_id IS NOT NULL AND source = ANY(%s)", (list(ALERTING_SOURCES),))}

    def reload_baseline(self) -> None:
        with self.conn.transaction():
            self.detector.baseline = Baseline.load(self.conn)

    def reload_links(self) -> None:
        with self.conn.transaction():
            self.detector.links = self._load_links()

    @property
    def boundary_ms(self) -> int:
        return self.minutes.next_to_close * 60_000

    def handle(self, batch: MinuteBatch) -> dict[tuple[str, str], str]:
        resolved: dict[tuple[str, str], str] = {}
        for c in batch["counts"]:
            service, level, minute = c["service"], c["level"], c["minute"]
            template_id, text = c["template_id"], None
            if template_id is None:
                try:
                    if c["text"] is None:
                        raise ValueError("the reader could not normalize the line")
                    template_id, text, _ = self.miners.match(service, c["text"])
                    resolved[(service, c["text"])] = template_id
                except Exception:  # noqa: BLE001 — one bad line must not stop the engine
                    template_id, text = f"{service}:unparsed", "(unparsed)"
                    if self._unparsed_logged.get(service) != minute:
                        self._unparsed_logged[service] = minute
                        logger.exception("could not template a %s line; counted as %s", service, template_id)
            self._see(service, template_id, text, level, c["module"], minute)
            if minute >= self.minutes.next_to_close:  # a late count is dropped, its sample too
                self._open_samples[minute].append((service, template_id, c["sample"]))
            self.minutes.add(service, template_id, level, c["module"], minute, c["n"])

        for cm in self.minutes.close_before(batch["watermark_ms"] // 60_000, self.services):
            for service, template_id, sample in self._open_samples.pop(cm.minute, ()):
                if self.levels[(service, template_id)].upper() in ERROR_LEVELS:
                    self._samples[(service, template_id)].append((cm.minute, sample))
            for (service, key), n in cm.keys.items():
                if key == "*total":  # close_before gives every service a *total key
                    self._hours[(service, cm.day, cm.hour)] += 1
                if n:
                    acc = self._sums.setdefault((service, key, cm.day, cm.hour), [0, 0])
                    acc[0] += n
                    acc[1] += n * n
            if not self.detection_enabled:
                continue
            historic = self.now() - (cm.minute + 1) * 60 > self.cfg.baseline.historic_after_minutes * 60
            self._changes += self.detector.run(cm, historic)
            if historic:
                continue
            for (service, template_id, module), n in cm.modules.items():
                row = self.detector.states.get((service, template_id))
                if row is not None and (row.state != "NORMAL" or row.streak):
                    self._alert_minutes.append((service, template_id, module, cm.day, _ts(cm.minute * 60), n))
        return resolved

    def _see(self, service: str, template_id: str, text: str | None, level: str, module: str, minute: int) -> None:
        first = self.services.get(service)
        if first is None:
            self._services[service] = zlib.crc32(service.encode("utf-8")) % 60
        if first is None or minute < first:
            self.services[service] = minute
        if module and (service, module) not in self.modules:
            self.modules.add((service, module))
            self._modules.append((module, service))
        key = (service, template_id)
        if _more_severe(level, self.levels.get(key)):
            self.levels[key] = level
        row = self._templates.get(key)
        if row is None:
            self._templates[key] = {"text": text, "first": minute, "last": minute}
        else:
            row["text"] = text or row["text"]
            row["first"] = min(row["first"], minute)
            row["last"] = max(row["last"], minute)

    def flush(self, cursors: dict[str, tuple[int, int]], status: dict) -> None:
        if self.conn.info.transaction_status != TransactionStatus.IDLE:
            # inside an open transaction conn.transaction() is only a savepoint: nothing would commit
            raise RuntimeError("Owner.flush needs an idle connection")
        self._blobs.update(self.miners.dirty())  # kept pending: a failed flush must not lose them
        new_t = [(s, t, self.levels[(s, t)], r["text"], _ts(r["first"] * 60), _ts(r["last"] * 60))
                 for (s, t), r in self._templates.items() if r["text"] is not None]
        seen_t = [(self.levels[(s, t)], _ts(r["last"] * 60), s, t)
                  for (s, t), r in self._templates.items() if r["text"] is None]
        states = self.detector.states
        touched = list(self.detector.touched)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(SQL["services"], list(self._services.items()))
            cur.executemany(SQL["modules"], self._modules)
            cur.executemany(SQL["templates"], new_t)
            cur.executemany(SQL["templates_seen"], seen_t)
            cur.executemany(SQL["drain3_state"], list(self._blobs.items()))
            cur.executemany(SQL["service_hours"], [(*k, n) for k, n in self._hours.items()])
            cur.executemany(SQL["hourly_counts"], [(*k, s, sq) for k, (s, sq) in self._sums.items()])
            cur.executemany(SQL["alert_minutes"], self._alert_minutes)
            cur.executemany(SQL["alert_state"], [
                (r.service, r.key, r.state, _ts(r.since), r.streak, r.score, r.count, r.band_low, r.band_high)
                for r in (states[k] for k in touched)])
            cur.executemany(SQL["alert_changes"], [(c.service, c.key, _ts(c.at), c.from_state, c.to_state,
                                                    c.count, c.band_low, c.band_high) for c in self._changes])
            cur.executemany(SQL["error_samples"], [(s, t, _ts(m * 60), line)
                                                   for (s, t), q in self._samples.items() for m, line in q])
            cur.executemany(SQL["error_samples_trim"], list(self._samples))
            cur.executemany(SQL["es_cursors"], [(i, since, floor) for i, (since, floor) in cursors.items()])
            cur.execute("DELETE FROM es_cursors WHERE index <> ALL(%s)", (list(cursors),))
            cur.executemany(SQL["worker_status"], [("engine", Jsonb(status))])
        self._clear()
        self.detector.touched.clear()

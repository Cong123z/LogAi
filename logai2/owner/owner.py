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
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg import sql
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
from logai2.timeutil import day_hour, local

logger = logging.getLogger("logai2.owner")

SAMPLES_KEPT = 20

# ponytail: executemany per table but the two module tables (COPY, copy_upsert: ~2x faster at 20k-60k rows, see
# docs/perf/2026-10-23-template-modules.md); move the others when flush time shows in the Pipeline stats.
SQL = {
    "services": "INSERT INTO services (service, partition) VALUES (%s, %s) ON CONFLICT DO NOTHING",
    "modules": "INSERT INTO modules (module_code, service) VALUES (%s, %s) ON CONFLICT DO NOTHING",
    "templates": (
        "INSERT INTO templates (service, template_id, level, text, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (service, template_id) DO UPDATE SET "
        "level = EXCLUDED.level, text = EXCLUDED.text, "
        "last_seen = GREATEST(templates.last_seen, EXCLUDED.last_seen), "
        "first_seen = LEAST(templates.first_seen, EXCLUDED.first_seen), "
        # a changed text makes the vector stale; the linker embeds it again
        "embedding = CASE WHEN templates.text IS DISTINCT FROM EXCLUDED.text THEN NULL ELSE templates.embedding END"),
    # a cache hit carries no text; its template row was inserted by the miss that taught the cache
    "templates_seen": (
        "UPDATE templates SET level = %s, last_seen = GREATEST(last_seen, %s), "
        "first_seen = LEAST(first_seen, %s) WHERE service = %s AND template_id = %s"),
    "drain3_state": (
        "INSERT INTO drain3_state (service, blob) VALUES (%s, %s) ON CONFLICT (service) DO UPDATE SET "
        "blob = EXCLUDED.blob, version = drain3_state.version + 1"),
    "service_hours": (
        "INSERT INTO service_hours (service, day, hour, n_minutes) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (service, day, hour) DO UPDATE SET n_minutes = service_hours.n_minutes + EXCLUDED.n_minutes"),
    # these two read the rows copy_upsert COPYs into tmp_<table> (keys unique: they come from dicts)
    "template_module_hours": (
        "INSERT INTO template_module_hours (service, template_id, module_code, day, hour, count) "
        "SELECT * FROM tmp_template_module_hours ON CONFLICT (service, template_id, module_code, day, hour) "
        "DO UPDATE SET count = template_module_hours.count + EXCLUDED.count"),
    "template_modules": (
        "INSERT INTO template_modules (service, template_id, module_code, first_seen, last_seen) "
        "SELECT * FROM tmp_template_modules ON CONFLICT (service, template_id, module_code) DO UPDATE SET "
        "first_seen = LEAST(template_modules.first_seen, EXCLUDED.first_seen), "
        "last_seen = GREATEST(template_modules.last_seen, EXCLUDED.last_seen)"),
    "hourly_counts": (
        "INSERT INTO hourly_counts (service, key, day, hour, sum, sumsq) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (service, key, day, hour) DO UPDATE SET "
        "sum = hourly_counts.sum + EXCLUDED.sum, sumsq = hourly_counts.sumsq + EXCLUDED.sumsq"),
    # element-wise add of the minutes closed since the last flush; reads tmp_lab_minute_counts (copy_upsert)
    "lab_minute_counts": (
        "INSERT INTO lab_minute_counts (service, key, day, hour, counts) SELECT * FROM tmp_lab_minute_counts "
        "ON CONFLICT (service, key, day, hour) DO UPDATE SET counts = ARRAY("
        "SELECT a + b FROM unnest(lab_minute_counts.counts, EXCLUDED.counts) WITH ORDINALITY AS t(a, b, i) ORDER BY i)"),
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
    # re-read lane: replace one whole (service, day, hour)
    "lane_had": "SELECT EXISTS (SELECT 1 FROM service_hours WHERE service = %s AND day = %s AND hour = %s) AS had",
    "lane_delete": ("service_hours", "hourly_counts", "template_module_hours", "lab_minute_counts"),
    "retrain_run": "UPDATE retrain_runs SET pos_ms = %s, steps = %s, log = COALESCE(%s, log) WHERE id = %s",
    "error_samples_trim": (
        "DELETE FROM error_samples WHERE ctid IN (SELECT ctid FROM error_samples "
        f"WHERE service = %s AND template_id = %s ORDER BY at DESC OFFSET {SAMPLES_KEPT})"),
    "index_services": "INSERT INTO index_services (pattern, service) VALUES (%s, %s) ON CONFLICT DO NOTHING",
    "es_cursors": (
        "INSERT INTO es_cursors (index, since_ms, floor_ms) VALUES (%s, %s, %s) ON CONFLICT (index) DO UPDATE "
        "SET since_ms = EXCLUDED.since_ms, floor_ms = EXCLUDED.floor_ms, updated_at = now()"),
    "worker_status": (
        "INSERT INTO worker_status (worker, status) VALUES (%s, %s) ON CONFLICT (worker) DO UPDATE "
        "SET status = EXCLUDED.status, updated_at = now()"),
}


def copy_upsert(cur, table: str, rows: list[tuple]) -> None:
    """COPY rows into a session temp table emptied at each commit, then upsert them with SQL[table]."""
    if not rows:   # most 2 s flushes carry no closed minute
        return
    cur.execute(sql.SQL("CREATE TEMP TABLE IF NOT EXISTS {} (LIKE {}) ON COMMIT DELETE ROWS").format(
        sql.Identifier(f"tmp_{table}"), sql.Identifier(table)))
    with cur.copy(sql.SQL("COPY {} FROM STDIN").format(sql.Identifier(f"tmp_{table}"))) as cp:
        for row in rows:
            cp.write_row(row)
    cur.execute(SQL[table])
    cur.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(f"tmp_{table}")))   # a second call in one transaction


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
            self._source_pairs = {(r["pattern"], r["service"])
                                  for r in conn.execute("SELECT pattern, service FROM index_services")}
            states = {(r["service"], r["key"]): AlertRow(
                r["service"], r["key"], r["state"], r["streak"], r["since"].timestamp(), r["score"],
                r["count"], r["band_low"], r["band_high"]) for r in conn.execute("SELECT * FROM alert_state")}
            self.detection_enabled = get_setting(conn, "detection_enabled") is True
            baseline = Baseline.load(conn)
            links = self._load_links()
        self.miners = Miners(cfg.drain3, blobs)
        self.minutes = OpenMinutes(start_minute)
        self.detector = Detector(cfg, baseline, links, self.levels, states)
        self.service_minutes: dict[str, deque[tuple[int, int]]] = defaultdict(lambda: deque(maxlen=60))
        self._unparsed_logged: dict[str, int] = {}  # service -> minute last logged
        # samples of open minutes, queued only when their minute closes: open minutes are
        # re-read after a crash, so queuing earlier would insert their samples twice
        self._open_samples: dict[int, dict[tuple[str, str], list[str]]] = defaultdict(dict)
        self._clear()

    def _clear(self) -> None:
        self._services: dict[str, int] = {}
        self._modules: list[tuple[str, str]] = []
        self._new_sources: set[tuple[str, str]] = set()   # not yet in index_services; known after the commit
        self._templates: dict[tuple[str, str], dict] = {}
        self._blobs: dict[str, bytes] = {}
        self._hours: Counter = Counter()
        self._sums: dict[tuple, list[int]] = {}
        self._alert_minutes: list[tuple] = []
        self._lab: dict[tuple, list[int]] = {}   # (service, key, day, hour) -> 60 counts of minutes closed since the last flush
        self._mod_hours: dict[tuple, int] = {}                 # (service, template_id, module, day, hour) -> n
        self._pairs: dict[tuple[str, str, str], list[int]] = {}  # (service, template_id, module) -> [first, last] minute
        self._changes: list = []
        self._samples: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=SAMPLES_KEPT))
        self._lane_hours: list = []                    # re-read LaneHours replacing whole hours in the next flush
        self._lane_run: tuple[int, dict] | None = None  # (run id, {"pos_ms", "steps"}) written in that flush
        self._closed = 0   # minutes closed since the last flush

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
    def close_full(self) -> bool:
        return self._closed >= self.cfg.flush.max_closed_minutes

    @property
    def boundary_ms(self) -> int:
        return self.minutes.next_to_close * 60_000

    def handle(self, batch: MinuteBatch) -> dict[tuple[str, str], str]:
        resolved: dict[tuple[str, str], str] = {}
        self._new_sources |= set(map(tuple, batch.get("sources", ()))) - self._source_pairs
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
            # a late count is dropped, its sample too; only error templates keep samples (memory while lagging)
            if minute >= self.minutes.next_to_close and self.levels[(service, template_id)].upper() in ERROR_LEVELS:
                kept = self._open_samples[minute].setdefault((service, template_id), [])
                if len(kept) < SAMPLES_KEPT:
                    kept.append(c["sample"])
            self.minutes.add(service, template_id, level, c["module"], minute, c["n"])

        today = day_hour(int(self.now() // 60))[0]
        mod_cutoff = today - timedelta(days=self.cfg.retention.module_hours_days)
        lab_cutoff = today - timedelta(days=self.cfg.lab.days)
        # a long lag is closed in bites of max_closed_minutes per flush (the cursor stays at the oldest open minute)
        room = max(0, self.cfg.flush.max_closed_minutes - self._closed)
        upto = min(batch["watermark_ms"] // 60_000, self.minutes.next_to_close + room)
        closed = self.minutes.close_before(upto, self.services)
        self._closed += len(closed)
        for cm in closed:
            for (service, template_id), lines in self._open_samples.pop(cm.minute, {}).items():
                if self.levels[(service, template_id)].upper() in ERROR_LEVELS:
                    self._samples[(service, template_id)].extend((cm.minute, line) for line in lines)
            for (service, key), n in cm.keys.items():
                if key == "*total":  # close_before gives every service a *total key
                    self.service_minutes[service].append((cm.minute, n))
                    self._hours[(service, cm.day, cm.hour)] += 1
                if n:
                    acc = self._sums.setdefault((service, key, cm.day, cm.hour), [0, 0])
                    acc[0] += n
                    acc[1] += n * n
            for (service, template_id, module), n in cm.modules.items():
                if template_id.endswith(":unparsed"):
                    continue
                k = (service, template_id, module, cm.day, cm.hour)
                if cm.day >= mod_cutoff:   # older days have no partition (commit dropped it): keep the pair only
                    self._mod_hours[k] = self._mod_hours.get(k, 0) + n
                pair = self._pairs.setdefault(k[:3], [cm.minute, cm.minute])
                pair[0], pair[1] = min(pair[0], cm.minute), max(pair[1], cm.minute)
            if self.cfg.lab.record and cm.day >= lab_cutoff:
                slot = local(cm.minute).minute
                for (service, key), n in cm.keys.items():
                    if n:
                        self._lab_row(service, key, cm.day, cm.hour)[slot] += n
                    if key == "*total":
                        self._lab_row(service, "*minutes", cm.day, cm.hour)[slot] = 1
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

    def _lab_row(self, service: str, key: str, day, hour: int) -> list[int]:
        return self._lab.setdefault((service, key, day, hour), [0] * 60)

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

    def add_lane(self, run_id: int, hours: list, run_update: dict) -> None:
        """Queue closed re-read hours and the run's pos_ms / steps; the next flush replaces those hours and
        writes the update in its transaction, after every live write. The lane's templates, modules and pairs
        go through the same pending paths as live ones; Owner.services (the zero-fill map) is not touched."""
        self._lane_hours += hours
        self._lane_run = (run_id, run_update)
        for h in hours:
            if h.service not in self.services:
                self._services.setdefault(h.service, zlib.crc32(h.service.encode("utf-8")) % 60)
            for tid, t in h.templates.items():
                key = (h.service, tid)
                if _more_severe(t["level"], self.levels.get(key)):
                    self.levels[key] = t["level"]
                row = self._templates.get(key)
                if row is None:
                    self._templates[key] = {"text": t["text"], "first": t["first"], "last": t["last"]}
                else:
                    row["text"] = t["text"] or row["text"]
                    row["first"], row["last"] = min(row["first"], t["first"]), max(row["last"], t["last"])
            for (tid, module), (a, b) in h.pairs.items():
                if module and (h.service, module) not in self.modules:
                    self.modules.add((h.service, module))
                    self._modules.append((module, h.service))
                pair = self._pairs.setdefault((h.service, tid, module), [a, b])
                pair[0], pair[1] = min(pair[0], a), max(pair[1], b)

    def _flush_lane(self, cur) -> None:
        today = day_hour(int(self.now() // 60))[0]
        mod_cutoff = today - timedelta(days=self.cfg.retention.module_hours_days)
        lab_cutoff = today - timedelta(days=self.cfg.lab.days)
        mod_rows, sample_rows, trims, lab_rows = [], [], set(), []
        for h in self._lane_hours:
            k = (h.service, h.day, h.hour)
            had = cur.execute(SQL["lane_had"], k).fetchone()["had"]
            for table in SQL["lane_delete"]:
                cur.execute(f"DELETE FROM {table} WHERE service = %s AND day = %s AND hour = %s", k)
            cur.execute(SQL["service_hours"], (*k, h.n_minutes))
            cur.executemany(SQL["hourly_counts"], [(h.service, key, h.day, h.hour, s, sq)
                                                   for key, (s, sq) in h.sums.items()])
            if h.day >= mod_cutoff:
                mod_rows += [(h.service, tid, module, h.day, h.hour, n) for (tid, module), n in h.module_hours.items()]
            if self.cfg.lab.record and h.day >= lab_cutoff:
                lab_rows += [(h.service, key, h.day, h.hour, v) for key, v in h.minutes.items() if any(v)]
            if not had:   # an hour that had rows has its samples already
                sample_rows += [(h.service, tid, _ts(m * 60), line) for tid, m, line in h.samples]
                trims |= {(h.service, tid) for tid, _, _ in h.samples}
        copy_upsert(cur, "template_module_hours", mod_rows)
        copy_upsert(cur, "lab_minute_counts", lab_rows)   # live pending rows of these hours were written first and just deleted: the lane wins
        cur.executemany(SQL["error_samples"], sample_rows)
        cur.executemany(SQL["error_samples_trim"], sorted(trims))
        if self._lane_run:
            run_id, update = self._lane_run
            log = update.get("log")   # callers without a log (tests) leave the stored one
            cur.execute(SQL["retrain_run"], (update["pos_ms"], Jsonb(update["steps"]),
                                             None if log is None else Jsonb(log), run_id))

    def flush(self, cursors: dict[str, tuple[int, int]], status: dict) -> None:
        if self.conn.info.transaction_status != TransactionStatus.IDLE:
            # inside an open transaction conn.transaction() is only a savepoint: nothing would commit
            raise RuntimeError("Owner.flush needs an idle connection")
        self._blobs.update(self.miners.dirty())  # kept pending: a failed flush must not lose them
        new_t = [(s, t, self.levels[(s, t)], r["text"], _ts(r["first"] * 60), _ts(r["last"] * 60))
                 for (s, t), r in self._templates.items() if r["text"] is not None]
        seen_t = [(self.levels[(s, t)], _ts(r["last"] * 60), _ts(r["first"] * 60), s, t)
                  for (s, t), r in self._templates.items() if r["text"] is None]
        states = self.detector.states
        touched = list(self.detector.touched)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(SQL["services"], list(self._services.items()))
            cur.executemany(SQL["modules"], self._modules)
            cur.executemany(SQL["index_services"], sorted(self._new_sources))
            cur.executemany(SQL["templates"], new_t)
            cur.executemany(SQL["templates_seen"], seen_t)
            cur.executemany(SQL["drain3_state"], list(self._blobs.items()))
            cur.executemany(SQL["service_hours"], [(*k, n) for k, n in self._hours.items()])
            cur.executemany(SQL["hourly_counts"], [(*k, s, sq) for k, (s, sq) in self._sums.items()])
            copy_upsert(cur, "lab_minute_counts", [(*k, v) for k, v in self._lab.items() if any(v)])
            copy_upsert(cur, "template_module_hours", [(*k, n) for k, n in self._mod_hours.items()])
            copy_upsert(cur, "template_modules", [(*k, _ts(a * 60), _ts(b * 60)) for k, (a, b) in self._pairs.items()])
            cur.executemany(SQL["alert_minutes"], self._alert_minutes)
            cur.executemany(SQL["alert_state"], [
                (r.service, r.key, r.state, _ts(r.since), r.streak, r.score, r.count, r.band_low, r.band_high)
                for r in (states[k] for k in touched)])
            cur.executemany(SQL["alert_changes"], [(c.service, c.key, _ts(c.at), c.from_state, c.to_state,
                                                    c.count, c.band_low, c.band_high) for c in self._changes])
            cur.executemany(SQL["error_samples"], [(s, t, _ts(m * 60), line)
                                                   for (s, t), q in self._samples.items() for m, line in q])
            cur.executemany(SQL["error_samples_trim"], list(self._samples))
            self._flush_lane(cur)
            cur.executemany(SQL["es_cursors"], [(i, since, floor) for i, (since, floor) in cursors.items()])
            cur.execute("DELETE FROM es_cursors WHERE index <> ALL(%s)", (list(cursors),))
            cur.executemany(SQL["worker_status"], [("engine", Jsonb(status))])
        self._source_pairs |= self._new_sources
        self._clear()
        self.detector.touched.clear()

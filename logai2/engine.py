"""Engine: one thread runs commands, reads, owns, flushes and commits.

Two connections. The owner's stays idle between flushes (Owner.flush refuses
an open transaction, and any SELECT opens one); everything else (settings,
commands, cursors, commit()) goes on `db`, each unit of work in its own
transaction.
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections import deque
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import yaml
from psycopg.types.json import Jsonb

from logai2 import commands
from logai2.alerts import alert_rows
from logai2.config import AppConfig
from logai2.db import connect, ensure_partitions, get_setting, migrate, put_setting
from logai2.jobs.commit import commit
from logai2.metrics import Metrics
from logai2.owner.owner import SQL, Owner
from logai2.reader.cache import KnownLines
from logai2.reader.reader import Reader
from logai2.timeutil import TZ, local, minute_of

logger = logging.getLogger("logai2.engine")

AVAILABLE_REFRESH_SECONDS = 30
MISS_WINDOW_SECONDS = 300
STATS_WINDOW_SECONDS = 900   # flush-time max and events/s in the status
MISS_WARN_RATIO = 0.5
MISS_WARN_MIN_EVENTS = 100   # a tiny service's few cold lines are not a warning
MAX_BACKOFF_SECONDS = 30
CONNECTION_FIELDS = ("hosts", "username", "password", "verify_certs")


def _day(seconds: float) -> date:
    return local(int(seconds // 60)).date()


class Engine:
    def __init__(self, cfg: AppConfig, conn, source, now: Callable[[], float] = time.time,
                 metrics: Metrics | None = None):
        self.cfg = cfg
        self.conn = conn
        self.source = source
        self.now = now
        self.metrics = metrics or Metrics(None)
        self.sleep = time.sleep
        # the second connection uses the same schema (tests run in a throwaway one)
        self._search_path = conn.execute("SHOW search_path").fetchone()["search_path"]
        conn.rollback()
        self.db = self._connect()
        self.commit_at = datetime.strptime(cfg.baseline.commit_time, "%H:%M").time()
        self.reader: Reader | None = None
        self.owner: Owner | None = None
        self.bootstrap: dict = {}
        self.last_commit: dict | None = None
        self.resolution: dict[str, list[str]] = {}
        self.available: list[dict] = []
        self._refreshed = self._listed = -math.inf
        self._partitions_day: date | None = None
        self._positions: dict[str, dict] = {}
        self._watermark_ms = 0
        self._pending_events = 0
        self._last_flush = now()
        # ponytail: miss ratio is a 5-min sliding sum of per-tick counts, not "above 50% for 5 min straight"
        self._observing_since: float | None = None
        self._miss_services: set[str] = set()
        self._miss_window: deque[tuple[float, dict[str, list[int]]]] = deque()
        self._totals = {"hits": 0, "misses": 0, "late": 0}
        self._flush_times: deque[tuple[float, float]] = deque()   # (finished at, seconds), last 900 s
        self._event_window: deque[tuple[float, int]] = deque()    # (observed at, events), last 900 s
        self._history: deque[dict] = deque(maxlen=60)             # one point per wall minute

    def _connect(self):
        c = connect(self.cfg.database.dsn)
        c.execute("SELECT set_config('search_path', %s, false)", (self._search_path,))
        c.commit()
        return c

    # --- start -------------------------------------------------------------

    def start(self) -> None:
        migrate(self.conn)
        now = self.now()
        today = _day(now)
        with self.db.transaction():
            self.bootstrap = get_setting(self.db, "bootstrap")
            if self.bootstrap is None:
                self.bootstrap = {"since": now - self.cfg.start.days_back * 86400, "until": now}
                put_setting(self.db, "bootstrap", self.bootstrap)
                logger.info("first start: reading from %s", local(int(self.bootstrap["since"] // 60)))
            self.last_commit = get_setting(self.db, "last_commit")
            self._seed_documents()
        self._apply_connection()
        self.reader = Reader(self.source, self.cfg, KnownLines(self.cfg.reader.cache_size), now=self.now)
        floors, saved = self._wait_for_indices()
        start_ms = min((saved.get(i, f) for i, f in floors.items()), default=int(now * 1000))
        # catch-up (first start, or a restart after downtime) writes counts into every day since start_ms
        with self.db.transaction():
            ensure_partitions(self.db, min(_day(start_ms / 1000), today), today + timedelta(days=2))
        self._partitions_day = today
        self.owner = Owner(self.conn, self.cfg, start_ms // 60_000, now=self.now)
        raised = sum(r.state != "NORMAL" for r in self.owner.detector.states.values())
        if raised:
            logger.warning("loaded %d non-NORMAL alert states; they are exported as they were "
                           "until catch-up re-judges their keys", raised)
        if self._commit_due():
            logger.info("missed daily commit: running it now")
            self._commit()

    def _seed_documents(self) -> None:
        path = self.cfg.documentation.seed_path
        if not path or self.db.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
            return
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw) if path.endswith(".json") else yaml.safe_load(raw)
        # logai-engine's corpus: {"entries": [...]} (JSON) or a bare list (YAML)
        docs = (data.get("entries") or data.get("documents") or []) if isinstance(data, dict) else data or []
        with self.db.cursor() as cur:
            cur.executemany(
                "INSERT INTO documents (doc_id, title, error_code, error, handling, revision) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [(str(d["id"]), d.get("title") or str(d["id"]), d.get("error_code") or "", d.get("error") or "",
                  d.get("handling") or d.get("text") or "", uuid.uuid4().hex) for d in docs])
        logger.info("seeded %d documents from %s", len(docs), path)

    def _apply_connection(self) -> None:
        with self.db.transaction():
            saved = get_setting(self.db, "es_connection") or {}
        self.source.reconnect(replace(self.cfg.elasticsearch,
                                      **{k: saved[k] for k in CONNECTION_FIELDS if k in saved}))

    def _wait_for_indices(self) -> tuple[dict[str, int], dict[str, int]]:
        """ES unreachable at start: retry with backoff instead of crash-looping; the
        web shows the warning meanwhile. Each retry picks up connection settings
        saved from the web since (commands only run once the loop starts)."""
        delay = 1
        while True:
            try:
                return self._select_indices()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Elasticsearch unreachable at start (%s); retrying in %ss", exc, delay)
                status = {"heartbeat": self.now(), "bootstrap": self.bootstrap, "last_commit": self.last_commit,
                          "warnings": [f"Elasticsearch unreachable: {exc}"]}
                with self.db.transaction():
                    self.db.execute(SQL["worker_status"], ("engine", Jsonb(status)))
                self.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                self._apply_connection()

    def _select_indices(self) -> tuple[dict[str, int], dict[str, int]]:
        self._refreshed = self.now()
        with self.db.transaction():
            selection = get_setting(self.db, "index_selection") or {}
            saved = {r["index"]: r["since_ms"] for r in self.db.execute("SELECT index, since_ms FROM es_cursors")}
        entries = selection.get("entries") or [{"pattern": self.cfg.elasticsearch.index, "added_at": 0}]
        self.resolution = self.source.resolve([e["pattern"] for e in entries])
        floors: dict[str, int] = {}
        for e in entries:
            # an index selected before the first start is read from days_back; a later one from when it was added
            floor = self.bootstrap["since"] if e["added_at"] <= self.bootstrap["until"] else e["added_at"]
            for index in self.resolution.get(e["pattern"], []):
                floors[index] = min(floors.get(index, math.inf), int(floor * 1000))
        self.reader.set_indices(floors, saved)
        return floors, saved

    # --- loop --------------------------------------------------------------

    def run_forever(self) -> None:
        self.start()
        while True:
            try:
                if not self.tick():
                    self.sleep(self.cfg.elasticsearch.poll_interval_seconds)
            except Exception:  # noqa: BLE001 — open minutes stay in memory; the next tick retries
                logger.exception("tick failed")
                if self.db.closed:
                    try:
                        self.db = self._connect()
                    except Exception:  # noqa: BLE001
                        logger.warning("database still unreachable")
                self.sleep(MAX_BACKOFF_SECONDS)

    def tick(self) -> bool:
        self._run_commands()
        now = self.now()
        self._refresh(now)
        batch = self.reader.poll()
        self.reader.learn(self.owner.handle(batch))
        self._observe(batch, now)
        if self._pending_events >= self.cfg.flush.max_events or now - self._last_flush >= self.cfg.flush.seconds:
            self._flush()
        if not self.owner.detection_enabled and self._caught_up():
            self._commit()
            with self.db.transaction():
                put_setting(self.db, "detection_enabled", True)
            self.owner.detection_enabled = True
            logger.info("first catch-up done: detection on")
            self._flush()   # status shows detection on
        elif self._commit_due():
            self._commit()
        return batch["events"] > 0

    def _refresh(self, now: float) -> None:
        if now - self._refreshed >= self.cfg.elasticsearch.index_refresh_seconds:
            try:
                self._select_indices()
            except Exception as exc:  # noqa: BLE001 — keep reading the indices already selected
                logger.warning("index refresh failed: %s", exc)
        if now - self._listed >= AVAILABLE_REFRESH_SECONDS:
            self._listed = now
            try:
                self.available = self.source.list_indices()
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_indices failed: %s", exc)
        today = _day(now)
        if today != self._partitions_day:   # local midnight: roll partitions forward
            with self.db.transaction():
                ensure_partitions(self.db, today, today + timedelta(days=2))
            self._partitions_day = today

    def _observe(self, batch, now: float) -> None:
        self._positions = batch["positions"]
        self._watermark_ms = batch["watermark_ms"]
        self._pending_events += batch["events"]
        self._event_window.append((now, batch["events"]))
        while self._event_window[0][0] < now - STATS_WINDOW_SECONDS:
            self._event_window.popleft()
        m = self.metrics
        m.events.inc(batch["events"])
        cache = self.reader.cache
        for name, counter, value in (("hits", m.cache_hits, cache.hits), ("misses", m.cache_misses, cache.misses),
                                     ("late", m.late, self.owner.minutes.late)):
            counter.inc(value - self._totals[name])
            self._totals[name] = value
        per: dict[str, list[int]] = {}
        for c in batch["counts"]:
            acc = per.setdefault(c["service"], [0, 0])
            acc[0] += c["n"] if c["template_id"] is None else 0
            acc[1] += c["n"]
        if self._observing_since is None:
            self._observing_since = now
        self._miss_window.append((now, per))
        while self._miss_window[0][0] < now - MISS_WINDOW_SECONDS:
            self._miss_window.popleft()

    def _caught_up(self) -> bool:
        healthy = [p for p in self._positions.values() if not p["failing"]]
        return bool(healthy) and all(p["caught_up"] for p in healthy)

    # --- flush and commit --------------------------------------------------

    def _flush(self) -> None:
        delay = 1
        while True:
            started = time.monotonic()
            status = self._status()
            try:
                self.owner.flush(self.reader.cursors(self.owner.boundary_ms), status)
                break
            except Exception:  # noqa: BLE001 — Postgres down: keep everything in memory, retry the flush only
                logger.exception("flush failed; retrying in %ss without reading", delay)
                if self.conn.closed:
                    try:
                        self.conn = self.owner.conn = self._connect()
                    except Exception:  # noqa: BLE001
                        logger.warning("database still unreachable")
                self.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
        self.reader.forget_before(self.owner.boundary_ms)
        self._pending_events = 0
        self._last_flush = self.now()
        m = self.metrics
        took = time.monotonic() - started
        self._flush_times.append((self._last_flush, took))
        m.flush_seconds.set(took)
        m.heartbeat.set(status["heartbeat"])
        m.lag.set(status["lag_seconds"])
        for service, ratio in status["miss_ratio"].items():
            m.miss_ratio.labels(service).set(ratio)
        for service in self._miss_services - set(status["miss_ratio"]):   # left the window
            m.miss_ratio.remove(service)
        self._miss_services = set(status["miss_ratio"])
        m.export_rows(alert_rows([asdict(r) for r in self.owner.detector.states.values()],
                                 self.owner.detector.links, {}, {}))

    def _status(self) -> dict:
        now = self.now()
        sums: dict[str, list[int]] = {}
        for _, per in self._miss_window:
            for service, (miss, total) in per.items():
                acc = sums.setdefault(service, [0, 0])
                acc[0] += miss
                acc[1] += total
        ratios = {s: miss / total for s, (miss, total) in sums.items() if total}
        # warn only once the window holds 5 min of data: right after a restart every line misses
        full = self._observing_since is not None and now - self._observing_since >= MISS_WINDOW_SECONDS
        warn = sorted(s for s, r in ratios.items()
                      if full and r > MISS_WARN_RATIO and sums[s][1] >= MISS_WARN_MIN_EVENTS)
        cache = self.reader.cache
        lookups = cache.hits + cache.misses
        elapsed = now - self._last_flush
        lag = max(0.0, now - self._watermark_ms / 1000)
        # the status a flush writes is built before it runs, so it reports the previous completed flush
        while self._flush_times and self._flush_times[0][0] < now - STATS_WINDOW_SECONDS:
            self._flush_times.popleft()
        while self._event_window and self._event_window[0][0] < now - STATS_WINDOW_SECONDS:
            self._event_window.popleft()
        events = sum(n for _, n in self._event_window)
        covered = max(1.0, min(STATS_WINDOW_SECONDS, now - (self._observing_since or now)))
        if not self._history or self._history[-1]["t"] != minute_of(now) * 60:
            last_minute = sum(n for t, n in self._event_window if t >= now - 60)
            self._history.append({"t": minute_of(now) * 60, "lag": lag, "eps": last_minute / 60})
        return {
            "heartbeat": now,
            "lag_seconds": lag,
            "flush_seconds": self._flush_times[-1][1] if self._flush_times else 0.0,
            "flush_seconds_max": max((d for _, d in self._flush_times), default=0.0),
            "late_counts": self.owner.minutes.late,
            "events_per_second_15m": events / covered,
            "history": list(self._history),
            "events_per_second": self._pending_events / elapsed if elapsed > 0 else 0.0,
            "cache_hit_ratio": cache.hits / lookups if lookups else None,
            "miss_ratio": ratios,
            "detection_enabled": self.owner.detection_enabled,
            "bootstrap": self.bootstrap,
            "last_commit": self.last_commit,
            "indices": {i: {**self._positions.get(i, {}), "floor": f} for i, f in self.reader.floors.items()},
            "available_indices": self.available,
            "resolution": self.resolution,
            "warnings": [f"high miss ratio: {s} {ratios[s]:.0%}" for s in warn],
        }

    def _owner_day(self) -> date:
        # the day of the oldest open minute, not the wall date: a lagging engine never commits an open day
        return local(self.owner.minutes.next_to_close).date()

    def _commit_due(self) -> bool:
        if not self.owner.detection_enabled:
            return False   # the first catch-up runs the first commit
        day = self._owner_day()
        done = date.fromisoformat(self.last_commit["today"]) if self.last_commit else date.min
        return done < day and self.now() >= datetime.combine(day, self.commit_at, tzinfo=TZ).timestamp()

    def _commit(self) -> dict:
        self._flush()
        day = self._owner_day()
        result = commit(self.db, self.cfg, day)
        with self.db.transaction():
            self.last_commit = get_setting(self.db, "last_commit")
        self.owner.reload_baseline()
        logger.info("commit for %s: %s", day, result)
        return result

    # --- commands ----------------------------------------------------------

    def _run_commands(self) -> None:
        with self.db.transaction():
            todo = commands.take(self.db)
        for c in todo:
            try:
                result = self._run(c["kind"], c["payload"])
            except Exception as exc:  # noqa: BLE001 — a bad command must not stop the engine
                logger.exception("command %s (%s) failed", c["id"], c["kind"])
                self.db.rollback()
                result = {"error": f"{type(exc).__name__}: {exc}"}
            commands.finish(self.db, c["id"], result)

    def _run(self, kind: str, payload: dict) -> dict:
        if kind == "recompute_baseline":
            return self._commit()
        if kind == "reload_links":
            self.owner.reload_links()
            return {}
        if kind == "reload_sources":
            self._apply_connection()
            self._select_indices()
            return {"resolution": self.resolution}
        raise ValueError(f"unknown command kind: {kind}")

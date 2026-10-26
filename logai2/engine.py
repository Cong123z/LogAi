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
from logai2.jobs import runs
from logai2.jobs.commit import commit
from logai2.jobs.reread import Lane, Scope, plan as plan_reread
from logai2.jobs.runs import RunLog
from logai2.metrics import Metrics
from logai2.owner.owner import SQL, Owner
from logai2.reader.cache import KnownLines
from logai2.reader.reader import Reader
from logai2.timeutil import TZ, local, minute_of

logger = logging.getLogger("logai2.engine")

AVAILABLE_REFRESH_SECONDS = 30
ES_OLDEST_REFRESH_SECONDS = 600
MISS_WINDOW_SECONDS = 300
STATS_WINDOW_SECONDS = 900   # flush-time max and events/s in the status
MISS_WARN_RATIO = 0.5
MISS_WARN_MIN_EVENTS = 100   # a tiny service's few cold lines are not a warning
MAX_BACKOFF_SECONDS = 30
CONNECTION_FIELDS = ("hosts", "username", "password", "verify_certs")


def _day(seconds: float) -> date:
    return local(int(seconds // 60)).date()


FAILED_LABELS = {"mark": "Mark finished days", "rebuild": "Rebuild baseline",
                 "housekeeping": "Partitions and retention"}


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
        self.schedule = self._parse_schedule(None)
        self._schedule_warned = None
        self.reader: Reader | None = None
        self.owner: Owner | None = None
        self.bootstrap: dict = {}
        self.last_commit: dict | None = None
        self.resolution: dict[str, list[str]] = {}
        self.available: list[dict] = []
        self._refreshed = self._listed = self._oldest_at = -math.inf
        self.es_oldest: dict[str, int | None] = {}
        self._targets: dict[str, str] = {}
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
        self.reread: dict | None = None                           # the active re-read run (one at a time)
        self._resume: dict | None = None                          # a running re-read waiting for a plan (ES error)
        self._resume_at = -math.inf
        self._last_rate = 0.0                                     # docs/s of the last lane, for the estimate

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
        self._load_schedule()
        self._apply_connection()
        self.reader = Reader(self.source, self.cfg, KnownLines(self.cfg.reader.cache_size), now=self.now)
        floors, starts = self._wait_for_indices()
        start_ms = min(starts.values(), default=int(now * 1000))
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
            self._commit("startup", self._rebuild_today())
        self._reread_resume()

    def _parse_schedule(self, raw) -> dict:
        """Effective schedule: each missing or invalid field falls back to its default."""
        raw = raw if isinstance(raw, dict) else {}
        out = {"enabled": True, "time": self.cfg.baseline.commit_time, "weekdays": list(range(7))}
        if isinstance(raw.get("enabled"), bool):
            out["enabled"] = raw["enabled"]
        try:
            out["time"] = datetime.strptime(raw["time"], "%H:%M").strftime("%H:%M")
        except (KeyError, TypeError, ValueError):
            pass
        days = raw.get("weekdays")
        if isinstance(days, list) and all(type(d) is int and 0 <= d <= 6 for d in days):
            out["weekdays"] = sorted(set(days))
        return out

    def _load_schedule(self) -> None:
        with self.db.transaction():
            raw = get_setting(self.db, "retrain_schedule")
        self.schedule = self._parse_schedule(raw)
        valid = isinstance(raw, dict) and all(
            k in ("enabled", "time", "weekdays") and self.schedule[k] == v for k, v in raw.items())
        if raw is not None and not valid and raw != self._schedule_warned:   # once per distinct bad value
            self._schedule_warned = raw
            logger.warning("invalid retrain_schedule %r: using defaults for the bad fields", raw)

    def _rebuild_today(self) -> bool:
        return self.schedule["enabled"] and self._owner_day().weekday() in self.schedule["weekdays"]

    def _next_commit_at(self) -> float | None:
        if not self.owner.detection_enabled:
            return None
        day = self._owner_day()
        done = date.fromisoformat(self.last_commit["today"]) if self.last_commit else date.min
        if done >= day:
            day += timedelta(days=1)
        at = datetime.strptime(self.schedule["time"], "%H:%M").time()
        return datetime.combine(day, at, tzinfo=TZ).timestamp()

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
        targets: dict[str, str] = {}
        for e in entries:
            if e["pattern"] in floors:
                continue   # duplicate pattern: the first entry wins
            # a pattern selected before the first start is read from days_back; a later one from when it was added
            floor = self.bootstrap["since"] if e["added_at"] <= self.bootstrap["until"] else e["added_at"]
            floors[e["pattern"]] = int(floor * 1000)
            # an index matched by two patterns is read once, by the earlier one
            targets[e["pattern"]] = ",".join([e["pattern"]] + [f"-{p}" for p in floors if p != e["pattern"]])
        # upgrade: rows keyed by concrete index all hold the minute boundary; a pattern without a row starts there
        oldest = min(saved.values(), default=None)
        starts = {p: saved.get(p, f if oldest is None else max(f, oldest)) for p, f in floors.items()}
        self.reader.set_sources(floors, starts, targets)
        self._targets = targets
        return floors, starts

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
            self._commit("first_start")
            with self.db.transaction():
                put_setting(self.db, "detection_enabled", True)
            self.owner.detection_enabled = True
            logger.info("first catch-up done: detection on")
            self._flush()   # status shows detection on
        elif self._commit_due():
            self._commit("schedule", self._rebuild_today())
        if self._resume and now - self._resume_at >= self.cfg.elasticsearch.index_refresh_seconds:
            self._reread_resume()
        self._lane_tick(now)
        return batch["events"] > 0

    def _refresh(self, now: float) -> None:
        if now - self._refreshed >= self.cfg.elasticsearch.index_refresh_seconds:
            try:
                self._load_schedule()
                self._select_indices()
            except Exception as exc:  # noqa: BLE001 — keep reading the indices already selected
                logger.warning("index refresh failed: %s", exc)
        if now - self._listed >= AVAILABLE_REFRESH_SECONDS:
            self._listed = now
            try:
                self.available = self.source.list_indices()
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_indices failed: %s", exc)
        if now - self._oldest_at >= ES_OLDEST_REFRESH_SECONDS:
            self._oldest_at = now
            try:
                self.es_oldest = {p: self.source.oldest_ms(t) for p, t in self._targets.items()}
            except Exception as exc:  # noqa: BLE001 — keep the previous values
                logger.warning("es_oldest refresh failed: %s", exc)
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
            "service_minutes": {s: [[m * 60, n] for m, n in d] for s, d in self.owner.service_minutes.items()},
            "detection_enabled": self.owner.detection_enabled,
            "bootstrap": self.bootstrap,
            "last_commit": self.last_commit,
            "schedule": self.schedule,
            "next_commit_at": self._next_commit_at(),
            "indices": {i: {**self._positions.get(i, {}), "floor": f} for i, f in self.reader.floors.items()},
            "available_indices": self.available,
            "resolution": self.resolution,
            "es_oldest": self.es_oldest,
            "warnings": [f"no index matches {p}" for p in self.reader.floors if not self.resolution.get(p)]
                        + [f"high miss ratio: {s} {ratios[s]:.0%}" for s in warn],
        }

    def _owner_day(self) -> date:
        # the day of the oldest open minute, not the wall date: a lagging engine never commits an open day
        return local(self.owner.minutes.next_to_close).date()

    def _commit_due(self) -> bool:
        if not self.owner.detection_enabled:
            return False   # the first catch-up runs the first commit
        day = self._owner_day()
        done = date.fromisoformat(self.last_commit["today"]) if self.last_commit else date.min
        return done < day and self.now() >= self._next_commit_at()

    def _record(self, fn, *args, **kw):
        """Run bookkeeping must never change the commit's outcome: log and carry on. The runs helpers
        use conn.transaction(), so a failure rolls back and leaves self.db idle."""
        try:
            return fn(self.db, *args, **kw)
        except Exception:  # noqa: BLE001
            logger.exception("recording retrain run failed")
            return None

    def _commit(self, trigger: str, rebuild: bool = True, run: tuple | None = None) -> dict:
        """`run` = (run id, RunLog, result(commit result) -> dict) makes this the rebuild of a re-read: its steps
        and outcome go on that run, and its log is the run's."""
        self._flush()
        day = self._owner_day()
        if run:
            run_id, rlog, to_result = run
        else:
            run_id = self._record(runs.start, "rebuild", trigger) if rebuild else None
            rlog, to_result = RunLog(), lambda r: r
            rlog.attach()

        def on_step(*a):
            if run_id is not None:
                self._record(runs.step, run_id, *a)
        try:
            result = commit(self.db, self.cfg, day, rebuild=rebuild, on_step=on_step)
        except Exception as exc:
            if run_id is not None:
                failed = getattr(exc, "step", None)
                for d in getattr(exc, "steps", []):
                    on_step(*d)
                if failed:
                    on_step(failed, FAILED_LABELS.get(failed, failed), {}, time.time(), time.time())
                self._record(runs.finish, run_id, "failed", to_result({}), error=str(exc),
                             error_type=type(exc).__name__, failed_step=failed, log=rlog.lines)
            raise
        else:
            if run_id is not None:
                self._record(runs.finish, run_id, "succeeded", to_result(result), log=rlog.lines)
        finally:
            if not run:
                rlog.detach()
        with self.db.transaction():
            self.last_commit = get_setting(self.db, "last_commit")
        if rebuild:
            self.owner.reload_baseline()
        logger.info("commit for %s: %s", day, result)
        return {**result, "run_id": run_id}

    # --- re-read -----------------------------------------------------------

    def _plan_reread(self, scope: Scope):
        """Plan from the selected patterns' services; `oldest_ms` is asked fresh (es_oldest can be 10 min old)."""
        with self.db.transaction():
            pairs = self.db.execute("SELECT pattern, service FROM index_services").fetchall()
        holding: dict[str, list[str]] = {}
        for r in pairs:
            if r["pattern"] in self.reader.floors:
                holding.setdefault(r["pattern"], []).append(r["service"])
        chosen = None if scope.services is None else set(scope.services)
        holding = {p: ss for p, ss in holding.items() if chosen is None or chosen & set(ss)}
        oldest = {p: self.source.oldest_ms(self._targets[p]) for p in holding}
        hour = self._owner_hour_ms()
        return plan_reread(scope, holding, oldest, hour)

    def _owner_hour_ms(self) -> int:
        t = local(self.owner.minutes.next_to_close).replace(minute=0, second=0, microsecond=0)
        return int(t.timestamp() * 1000)

    def _docs_in(self, windows) -> int:
        """Docs of the windows: one `_count` per pattern per contiguous span of its windows."""
        spans: dict[str, list[list[int]]] = {}
        for w in windows:
            for p in w.patterns:
                s = spans.setdefault(p, [])
                if s and s[-1][1] == w.start_ms:
                    s[-1][1] = w.end_ms
                else:
                    s.append([w.start_ms, w.end_ms])
        timeout = self.cfg.retrain.es_timeout_seconds
        return sum(self.source.count(self._targets[p], a, b - 1, timeout=timeout)
                   for p, ranges in spans.items() for a, b in ranges)

    @staticmethod
    def _scope_detail(p) -> dict:
        return {"services": len(p.services), "patterns": len(p.patterns), "hours_chosen": p.hours_chosen,
                "hours_not_in_es": p.hours_not_in_es, "hours_live": p.hours_live, "hours_to_read": p.hours_to_read}

    def _estimate(self, scope: Scope) -> dict:
        p = self._plan_reread(scope)
        docs = self._docs_in(p.windows)
        rate = self._last_rate or self.cfg.retrain.default_rate
        return {"services": len(p.services), "patterns": len(p.patterns), "hours_to_read": p.hours_to_read,
                "hours_not_in_es": p.hours_not_in_es, "hours_live": p.hours_live, "docs": docs,
                "eta_s": docs / rate, "indices": len({i for x in p.patterns for i in self.resolution.get(x, [])})}

    def _reread_run(self, scope: Scope) -> dict:
        if self.reread or self._resume:
            return {"error": "a retrain is running"}
        p = self._plan_reread(scope)
        if p.hours_to_read == 0:
            return {"error": "nothing to re-read", **self._scope_detail(p)}
        now = self.now()
        with self.db.transaction():
            ensure_partitions(self.db, min(scope.days), max(_day(now), max(scope.days)))
        run_id = runs.start(self.db, "reread", "manual", scope.to_json())
        steps = {}
        for key, label, detail, end in (("scope", "Scope", self._scope_detail(p), now),
                                        ("partitions", "Partitions", {"first_day": min(scope.days).isoformat()}, now),
                                        ("read", "Read from Elasticsearch", {}, None)):
            steps[key] = {"key": key, "label": label, "started_at": now, "finished_at": end, "detail": detail}
            self._record(runs.step, run_id, key, label, detail, now, end)
        self._reread_begin(p, run_id, 0, steps, {})
        return {"run_id": run_id}

    def _reread_resume(self) -> None:
        """A run left 'running' by a stopped engine continues from its saved position. When the plan cannot
        be made (ES briefly down) the run stays running and the resume is retried, once per
        index_refresh_seconds."""
        if self._resume is None:
            with self.db.transaction():
                self._resume = self.db.execute(
                    "SELECT id, scope, pos_ms, steps FROM retrain_runs "
                    "WHERE kind = 'reread' AND state = 'running' ORDER BY id DESC LIMIT 1").fetchone()
        row = self._resume
        if row is None:
            return
        self._resume_at = self.now()
        steps = {s["key"]: s for s in row["steps"]}
        saved = steps.get("read", {}).get("detail", {})
        try:
            p = self._plan_reread(Scope.from_json(row["scope"]))
            self._reread_begin(p, row["id"], row["pos_ms"] or 0, steps,
                               {**saved, "docs_read": saved.get("docs_committed", saved.get("docs_read", 0))},
                               resumed=True)
        except Exception as exc:  # noqa: BLE001 — ES blip: keep the run, try again later
            logger.warning("could not resume re-read %s yet (%s); will retry", row["id"], exc)
            return
        self._resume = None

    def _reread_begin(self, p, run_id: int, pos_ms: int, steps: dict, saved: dict, resumed: bool = False) -> None:
        with self.db.transaction():
            first = {r["service"]: int(r["first"].timestamp()) // 60 for r in self.db.execute(
                "SELECT service, min(first_seen) AS first FROM templates GROUP BY service") if r["first"]}
        lane = Lane(p, run_id, pos_ms, self.source, self.owner.miners, self.reader.cache, first, self.cfg,
                    self._targets)
        base = {k: saved.get(k, 0) for k in ("docs_read", "hours_done", "new_templates")}
        if not resumed:
            base = dict.fromkeys(base, 0)
        try:
            target = base["docs_read"] + self._docs_in(lane.windows)
        except Exception as exc:  # noqa: BLE001 — the bar is only an estimate
            logger.warning("could not count the docs to re-read: %s", exc)
            target = None
        rlog = RunLog()
        rlog.attach()
        self.reread = {"run_id": run_id, "lane": lane, "plan": p, "steps": steps, "base": base, "target": target,
                       "log": rlog, "errors": 0, "paused": saved.get("paused", 0), "lag": 0.0, "read": {}}
        if resumed:
            self.reread["resumed_at"] = self.now()
        self._reread_publish([])

    def _read_detail(self) -> dict:
        rr = self.reread
        lane, base = rr["lane"], rr["base"]
        prog = lane.progress()
        docs = base["docs_read"] + prog["docs_read"]
        done = base["hours_done"] + prog["hours_done"]
        total = base["hours_done"] + prog["hours_total"]
        target = rr["target"]
        pct = 100 if lane.done else min(99, round(100 * (docs / target if target else done / max(total, 1))))
        eta = max(0, target - docs) / prog["rate"] if target and prog["rate"] > 0 else None
        detail = {"docs_read": docs, "docs_committed": base["docs_read"] + prog["docs_committed"],
                  "docs_target": target, "hours_done": done, "hours_total": total,
                  "new_templates": base["new_templates"] + prog["new_templates"], "rate": prog["rate"],
                  "position_ms": prog["position_ms"], "live_lag_s": rr["lag"], "paused": rr["paused"],
                  "pct": pct, "eta_s": eta}
        if "resumed_at" in rr:
            detail["resumed_at"] = rr["resumed_at"]
        return detail

    def _steps_json(self) -> list[dict]:
        return list(self.reread["steps"].values())

    def _reread_publish(self, hours: list) -> None:
        """Refresh the read step and queue the finished hours with the run's position for the next flush."""
        rr = self.reread
        rr["read"] = self._read_detail()
        read = rr["steps"].setdefault("read", {"key": "read", "label": "Read from Elasticsearch",
                                               "started_at": self.now(), "finished_at": None})
        read["detail"] = rr["read"]
        self.owner.add_lane(rr["run_id"], hours, {"pos_ms": rr["lane"].pos_ms, "steps": self._steps_json(),
                                                 "log": rr["log"].lines})

    def _lane_tick(self, now: float) -> None:
        rr = self.reread
        if rr is None:
            return
        lane = rr["lane"]
        rr["lag"] = lag = max(0.0, now - self._watermark_ms / 1000)
        hours: list = []
        if not (self._caught_up() and lag <= self.cfg.retrain.max_live_lag_seconds):
            rr["paused"] += 1   # live first: the lane never runs while it lags or catches up
        else:
            try:
                hours = lane.step(self.cfg.retrain.tick_seconds, now)
                rr["errors"] = 0
            except Exception as exc:  # noqa: BLE001
                hours = lane.take_ready()   # windows closed before the failure are queued, never lost
                rr["errors"] += 1
                logger.warning("re-read page failed (%d in a row): %s", rr["errors"], exc)
                if rr["errors"] >= 3:
                    self._reread_publish(hours)
                    self._reread_end("failed", exc)
                    return
        self._reread_publish(hours)
        if lane.done:
            self._reread_end("succeeded")

    def _reread_result(self) -> dict:
        rr = self.reread
        read, lane = rr["read"], rr["lane"]
        return {"docs_read": read["docs_read"], "hours_replaced": read["hours_done"] - lane.skipped,
                "hours_not_in_es": rr["plan"].hours_not_in_es + lane.skipped, "new_templates": read["new_templates"]}

    def _reread_end(self, state: str, exc: Exception | None = None) -> None:
        """succeeded: rebuild the baseline as steps of this run; failed / cancelled: the hours already flushed
        stay, no rebuild."""
        rr = self.reread
        try:
            read = rr["steps"]["read"]
            read["finished_at"] = self.now()
            self._last_rate = rr["lane"].progress()["rate"] or self._last_rate
            if state == "succeeded":
                rr["read"]["pct"] = 100
                self._reread_publish([])
                self._commit("manual", True, run=(rr["run_id"], rr["log"],
                                                  lambda c: {**self._reread_result(), "baseline_rows": c.get("baseline_rows")}))
                return
            if exc is not None:
                read["detail"] = {**rr["read"], "error": str(exc)}
            self._flush()   # hours the lane already finished are written with their position
            self._record(runs.step, rr["run_id"], "read", read["label"], read["detail"], read["started_at"],
                         read["finished_at"])
            self._record(runs.finish, rr["run_id"], state, self._reread_result(),
                         error=str(exc) if exc else None, error_type=type(exc).__name__ if exc else None,
                         failed_step="read" if exc else None, log=rr["log"].lines)
        finally:
            rr["log"].detach()
            self.reread = None

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
            return self._commit("manual")
        if kind == "retrain_run":
            return self._reread_run(Scope.from_json(payload))
        if kind == "retrain_estimate":
            return self._estimate(Scope.from_json(payload))
        if kind == "retrain_cancel":
            if self._resume and self._resume["id"] == payload.get("run_id") and self.reread is None:
                self._record(runs.finish, self._resume["id"], "cancelled")
                self._resume = None
                return {}
            if self.reread is None or self.reread["run_id"] != payload.get("run_id"):
                return {"error": "no such running re-read"}
            self._reread_end("cancelled")
            return {}
        if kind == "reload_links":
            self.owner.reload_links()
            return {}
        if kind == "reload_sources":
            self._apply_connection()
            self._select_indices()
            return {"resolution": self.resolution}
        raise ValueError(f"unknown command kind: {kind}")

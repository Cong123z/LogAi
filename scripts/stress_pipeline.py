"""Whole-pipeline stress test, normal operation and incidents (docs/perf/2026-10-24-pipeline-stress.md):
python scripts/stress_pipeline.py --dsn <dsn> [--scenarios N1,N1-10k,N2,I1,I2,I3,I4,I5,I6,I7,E1|all]
       [--scale tiny] [--services 10 --templates 300 --eps 2000] [--es-ms 2.5 --es-us-per-hit 10]
       [--es-host http://localhost:9200] [--max-bytes N] [--json out.json]

The real Engine (Reader, KnownLines, Owner with Drain3, detection, flush, commit) runs in-process with an
injected clock against a synthetic source (one index per service, hits generated lazily per time range,
never stored) and a throwaway schema; nothing is written to Elasticsearch except by E1.

Setup per scenario: Drain3 is pretrained on every generated template (state saved as on a running engine),
five past weekdays of hourly_counts/service_hours are written from the generator's rates (Poisson: mean and
variance lambda per minute), `commit` builds a full baseline and detection is switched on as the engine does
after its first catch-up.

Simulated time advances by the engine's own wall time per tick (generator time excluded), a modelled
Elasticsearch latency per page (--es-ms + --es-us-per-hit x hits, calibrated by E1) and every engine sleep.
Before each scenario its writes (rows x 150 B x 2 for WAL) are compared with half the free space on / (or
--max-bytes); larger is refused (exit 2). A scenario also stops when / falls under 600 MB free.
"""
from __future__ import annotations

import argparse
import bisect
import fnmatch
import heapq
import json
import logging
import math
import random
import shutil
import sys
import time
import traceback
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import logai2.engine as engine_mod  # noqa: E402
from logai2.config import load_config  # noqa: E402
from logai2.db import connect, ensure_partitions, migrate, put_setting  # noqa: E402
from logai2.engine import MAX_BACKOFF_SECONDS, Engine  # noqa: E402
from logai2.jobs.commit import commit  # noqa: E402
from logai2.owner.counts import ERROR_LEVELS  # noqa: E402
from logai2.owner.miners import Miners  # noqa: E402
from logai2.reader.normalize import normalize  # noqa: E402
from logai2.timeutil import TZ, day_hour  # noqa: E402
from scripts.stress_modules import public_is_migrated, schema_dsn  # noqa: E402

DAY0 = date(2026, 10, 21)   # a Wednesday: the five weekdays before it give a full weekday baseline
T0 = datetime(2026, 10, 21, 10, 0, tzinfo=TZ).timestamp()
HISTORY = [DAY0 - timedelta(days=d) for d in (1, 2, 5, 6, 7)]
ROW_BYTES = 150
MIN_FREE = 600 * 2**20
STALL_SECONDS = 600   # sim seconds without a successful flush = the engine is stalled (B1)
MODULE_NAMES = ("CORE", "TOPUP", "PAYMENT", "NOTIFY", "BILLING")
CHANNELS = ("USSD", "SMS", "APP", "WEB", "IVR", "API", "POS", "BANK")
VARS = ("msisdn={msisdn}", "transId={tid}", "amount={amt}", "requestId={rid}", "code={code}", "after {ms}ms")
WORDS = """account active adapter added agent allowed amount apply approved async attempt audit balance bank
basket batch billing blocked bonus booking broker bucket bundle cache callback cancel card cart channel charge
check child circuit claim client closed commit config confirm connect consumer contract convert coupon credit
cron customer daily data debit decline default delete delivery deposit detail device discount dispatch done
draft duplicate enqueue entry event expired export external failed fallback fee fetch file filter finish flag
gateway grant handler header history hold idle import inbound insert invalid invoice issue item job journal
key kyc large ledger limit link load lock login lookup loyalty mapping market merchant merge message mobile
monthly network notify offer open order outbound owner package paid parse partner payment pending plan pool
post postpaid prepaid price process product profile promo provider publish purchase queue quota rate read
receipt recharge record redeem refund register reject release reload remote renew report request reserve
reset resolve response retry reverse reward route rule schedule scheme send session settle shard signal sim
skip slow snapshot source start status stock store submit subscriber success summary suspend switch sync
target task tax tenant ticket timeout token topup trace transfer trigger unlock update upload usage user
validate vendor verify voucher wallet warning window worker write""".split()

M64 = (1 << 64) - 1


def mix(x: int) -> int:
    """splitmix64: event k's dice."""
    x = (x + 0x9E3779B97F4A7C15) & M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & M64
    return x ^ (x >> 31)


def pct(xs: list[float], q: float) -> float:
    return sorted(xs)[max(0, math.ceil(q * len(xs)) - 1)] if xs else 0.0


@dataclass
class Template:
    service: str
    level: str
    fmt: str
    module: str
    alt: str
    share: float              # of the service's events
    tid: str = ""

    def render(self, h: int) -> str:
        return self.fmt.format(msisdn=84900000000 + h % 99999999, tid=f"{h:016x}"[:12], amt=(h >> 20) % 500 * 1000,
                               rid=f"REQ{(h >> 8) % 10**10:010d}", code=(h >> 40) % 100,
                               ch=CHANNELS[(h >> 50) % 8], ms=(h >> 30) % 3000)


def make_fmt(rng: random.Random, words=(4, 8), storm=False) -> str:
    head = rng.sample(WORDS, rng.randint(*words))
    rest = head[2:] + rng.sample(VARS, rng.randint(2, 3)) + (["channel={ch}"] if rng.random() < 0.3 else [])
    rng.shuffle(rest)
    if storm:   # stack-trace-like: a varied exception class and frame, then the words
        exc = "".join(w.capitalize() for w in rng.sample(WORDS, 2))
        frame = ".".join(rng.sample(WORDS, 3))
        return f"java.lang.{exc}Exception: {' '.join(head[:2] + rest)} at com.vt.{frame}(Native Method)"
    return " ".join(head[:2] + rest)


def build_world(a) -> dict[str, list[Template]]:
    """Per service: 10 % ERROR templates (1 % of events), 15 % WARN (4 %), the rest INFO; Zipf within a class."""
    world = {}
    for s in range(a.services):
        svc = f"svc{s:02d}"
        rng = random.Random(f"{a.seed}-{svc}")
        mods = [f"{svc.upper()}_{m}" for m in MODULE_NAMES]
        n_err, n_warn = max(1, a.templates // 10), max(1, a.templates * 15 // 100)
        out = []
        for level, n, share in (("ERROR", n_err, 0.01), ("WARN", n_warn, 0.04),
                                ("INFO", a.templates - n_err - n_warn, 0.95)):
            weights = [1 / (i + 1) ** 1.1 for i in range(n)]
            total = sum(weights)
            for i, w in enumerate(weights):
                j = len(out)
                module = "" if j % 20 == 19 else mods[j % 5]
                out.append(Template(svc, level, make_fmt(rng), module, mods[(j + 1) % 5], share * w / total))
        world[svc] = out
    return world


def storm_templates(a, svc: str) -> list[Template]:
    rng = random.Random(f"{a.seed}-storm")
    return [Template(svc, "ERROR", make_fmt(rng, (5, 10), storm=True), f"{svc.upper()}_CORE", f"{svc.upper()}_CORE",
                     1.0) for _ in range(a.storm_templates)]


class Stream:
    """Events at a fixed rate (per minute) in [a_ms, b_ms): event k at a_ms + k*60000 // rpm, its template,
    module and variables from mix(seed ^ k). `delay_ms`: visible only from ts + delay_ms (late ingest)."""

    def __init__(self, sid: str, service: str, a_ms: int, b_ms: int, rpm: int, templates: list[Template],
                 module_fn=None, delay_ms: int = 0, bad: tuple[str, str] | None = None):
        self.sid, self.service, self.a_ms, self.b_ms, self.rpm = sid, service, a_ms, b_ms, max(rpm, 0)
        self.templates, self.module_fn, self.delay_ms, self.bad = templates, module_fn, delay_ms, bad
        self.seed = mix(zlib.crc32(sid.encode()))
        acc, self.cum = 0.0, []
        for t in templates:
            acc += t.share
            self.cum.append(acc)
        self.total = acc
        self._sec, self._pfx = -1, ""

    def first_k(self, t_ms: int) -> int:
        return max(0, -((-(t_ms - self.a_ms) * self.rpm) // 60000))

    def count(self, a_ms: int, b_ms: int) -> int:
        """Ground truth: events with a_ms <= ts < b_ms."""
        a_ms, b_ms = max(a_ms, self.a_ms), min(b_ms, self.b_ms)
        return max(0, self.first_k(b_ms) - self.first_k(a_ms)) if self.rpm and b_ms > a_ms else 0

    def hits(self, from_ms: int, upper_ms: int, limit: int, now_ms: int) -> list[dict]:
        hi = min(upper_ms, self.b_ms - 1, now_ms - self.delay_ms)
        if not self.rpm or hi < max(from_ms, self.a_ms):
            return []
        out, k, n = [], self.first_k(max(from_ms, self.a_ms)), len(self.templates) - 1
        while len(out) < limit:
            ts = self.a_ms + k * 60000 // self.rpm
            if ts > hi:
                break
            h = mix(self.seed ^ k)
            t = self.templates[min(n, bisect.bisect_right(self.cum, (h & 0xFFFFFFFF) / 4294967296 * self.total))]
            sec = ts // 1000
            if sec != self._sec:
                self._sec, self._pfx = sec, datetime.fromtimestamp(sec, TZ).strftime("%Y-%m-%d %H:%M:%S")
            src = {"@timestamp": ts, "service_code2": self.service,
                   "message": f"{self._pfx},{ts % 1000:03d} {t.level} [pool-{h >> 61}-thread-{(h >> 52) % 64}] "
                              + t.render(h)}
            module = self.module_fn(h) if self.module_fn else t.alt if (h >> 44) % 10 == 0 else t.module
            if module:
                src["moduleCode"] = module
            if self.bad:
                where, char = self.bad
                src[where] = src.get(where, "x")[:20] + char + src.get(where, "x")[20:]
            out.append({"_id": f"{self.sid}-{k}", "sort": [ts, k], "_source": src})
            k += 1
        return out


class GenSource:
    """EsSource interface over Streams (one index per service), timing its own work."""

    def __init__(self, clock, streams: list[Stream], size: int):
        self.clock, self.size = clock, size
        self.indices: dict[str, list[Stream]] = {}
        for s in streams:
            self.indices.setdefault(f"stress-{s.service}", []).append(s)
        self.gen_seconds = 0.0
        self.pages = self.hits = 0

    def _match(self, target: str) -> list[str]:
        first, *excl = target.split(",")
        return sorted(i for i in fnmatch.filter(self.indices, first)
                      if not any(fnmatch.fnmatch(i, e[1:]) for e in excl))

    def page(self, target: str, from_ms: int, upper_ms: int) -> list[dict]:
        started = time.perf_counter()
        now_ms = int(self.clock() * 1000)
        lists = [[{**h, "_index": i} for h in s.hits(from_ms, upper_ms, self.size, now_ms)]
                 for i in self._match(target) for s in self.indices[i]]
        hits = lists[0] if len(lists) == 1 else list(heapq.merge(*lists, key=lambda h: h["sort"][0]))[:self.size]
        self.pages += 1
        self.hits += len(hits)
        self.gen_seconds += time.perf_counter() - started
        return hits

    def resolve(self, patterns: list[str]) -> dict[str, list[str]]:
        return {p: self._match(p) for p in patterns}

    def list_indices(self) -> list[dict]:
        return [{"index": i} for i in sorted(self.indices)]

    def reconnect(self, cfg) -> None:
        pass

    def truth(self, a_ms: int, b_ms: int) -> int:
        return sum(s.count(a_ms, b_ms) for ss in self.indices.values() for s in ss)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Stalled(BaseException):
    """Not an Exception: the engine's own `except Exception` must not swallow it."""


class DiskLow(BaseException):
    pass


def base_streams(world, a, svc: str, a_ms: int, b_ms: int, cuts=()) -> list[Stream]:
    """The service's normal traffic over [a_ms, b_ms); `cuts` = [(from_ms, to_ms, overrides)] replace it there."""
    rpm = a.eps * 60 // a.services
    out, at = [], a_ms
    for i, (c0, c1, over) in enumerate(sorted(cuts, key=lambda c: c[0])):
        out.append(Stream(f"{svc}-b{i}", svc, at, c0, rpm, world[svc]))
        out.append(Stream(f"{svc}-c{i}", svc, c0, c1, over.pop("rpm", rpm), world[svc], **over))
        at = c1
    out.append(Stream(f"{svc}-e", svc, at, b_ms, rpm, world[svc]))
    return out


# --- setup -----------------------------------------------------------------------------------------------

def pretrain(cfg, world) -> tuple[dict[str, bytes], list[tuple]]:
    """Every template (all channel variants) through Drain3 once, as a running engine would have it."""
    miners = Miners(cfg.drain3, {})
    texts = {}
    for svc, templates in world.items():
        for t in templates:
            for v in range(8 if "{ch}" in t.fmt else 1):
                h = mix(zlib.crc32(t.fmt.encode()) + v) & ~(7 << 50) | (v << 50)
                t.tid, text, _ = miners.match(svc, normalize(f"2026-10-21 10:00:00,000 {t.level} [main] " + t.render(h)))
                texts[(svc, t.tid)] = (t.level, text)
    return miners.dirty(), [(s, tid, lv, text) for (s, tid), (lv, text) in texts.items()]


def setup(a, cfg, world, schema: str, hours: list[int], burst: Template, read_from: float) -> None:
    with psycopg.connect(schema_dsn(a.dsn, schema), autocommit=False, row_factory=dict_row) as c:
        migrate(c)
        ensure_partitions(c, DAY0 - timedelta(days=8), DAY0 + timedelta(days=2))
        blobs, templates = pretrain(cfg, world)
        first = datetime.fromtimestamp(T0 - 30 * 86400, TZ)
        rpm = a.eps * 60 / a.services
        counts, service_hours = [], []
        for svc, ts in world.items():
            lam: dict[str, float] = {"*total": rpm}
            for t in ts:
                cls = "*error" if t.level in ERROR_LEVELS else "*warn" if t.level == "WARN" else "*info"
                for key in (t.tid, cls):
                    lam[key] = lam.get(key, 0.0) + rpm * t.share
            for d in HISTORY:
                for hr in hours:
                    service_hours.append((svc, d, hr, 60))
                    # Poisson: mean x, variance x per minute
                    counts += [(svc, key, d, hr, round(60 * x), round(60 * (x + x * x))) for key, x in lam.items()]
        with c.transaction(), c.cursor() as cur:
            cur.executemany("INSERT INTO services (service, partition) VALUES (%s, %s)",
                            [(s, i % 60) for i, s in enumerate(world)])
            cur.executemany("INSERT INTO modules (module_code, service) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            [(m, s) for s, ts in world.items() for t in ts for m in (t.module, t.alt) if m])
            cur.executemany("INSERT INTO templates (service, template_id, level, text, first_seen, last_seen) "
                            "VALUES (%s, %s, %s, %s, %s, %s)", [(*t, first, first) for t in templates])
            cur.executemany("INSERT INTO drain3_state (service, blob) VALUES (%s, %s)", list(blobs.items()))
            cur.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('stress', 'stress', '', 'r')")
            cur.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                        "VALUES (%s, %s, 'stress', 'manual')", (burst.service, burst.tid))
            for table, rows in (("hourly_counts (service, key, day, hour, sum, sumsq)", counts),
                                ("service_hours (service, day, hour, n_minutes)", service_hours)):
                with cur.copy(f"COPY {table} FROM STDIN") as cp:
                    for row in rows:
                        cp.write_row(row)
        commit(c, cfg, DAY0)
        put_setting(c, "detection_enabled", True)   # as the engine does after its first catch-up
        put_setting(c, "bootstrap", {"since": read_from, "until": read_from})


# --- one run ---------------------------------------------------------------------------------------------

@dataclass
class Run:
    a: argparse.Namespace
    engine: Engine
    clock: Clock
    source: GenSource
    read_from: float
    polls: list = field(default_factory=list)
    handles: list = field(default_factory=list)
    flushes: list = field(default_factory=list)     # (seconds, closed a minute)
    lags: list = field(default_factory=list)         # (sim t, lag s), one per sim second
    errors: list = field(default_factory=list)
    tick_failures: int = 0
    flush_failures: int = 0
    engine_seconds: float = 0.0
    last_ok: float = 0.0
    stalled: bool = False

    def wrap(self) -> None:
        e, owner, reader = self.engine, self.engine.owner, self.engine.reader
        poll, handle, flush = reader.poll, owner.handle, owner.flush

        def timed_poll():
            t0, g0 = time.perf_counter(), self.source.gen_seconds
            out = poll()
            self.polls.append(time.perf_counter() - t0 - (self.source.gen_seconds - g0))
            return out

        def timed_handle(batch):
            t0 = time.perf_counter()
            out = handle(batch)
            self.handles.append(time.perf_counter() - t0)
            return out

        def timed_flush(cursors, status):
            t0, closed = time.perf_counter(), bool(owner._sums)
            try:
                flush(cursors, status)
            except Exception as exc:
                self.flush_failures += 1
                self.note(exc)
                raise
            self.flushes.append((time.perf_counter() - t0, closed))
            self.last_ok = self.clock.t

        def sleep(s):
            self.clock.t += s
            if self.clock.t - self.last_ok > STALL_SECONDS:
                self.stalled = True
                raise Stalled()
        reader.poll, owner.handle, owner.flush, e.sleep = timed_poll, timed_handle, timed_flush, sleep
        self.last_ok = self.clock.t

    def note(self, exc: BaseException) -> None:
        where = traceback.extract_tb(exc.__traceback__)[-1]
        msg = f"{type(exc).__name__}: {str(exc)[:120]} ({Path(where.filename).name}:{where.lineno} {where.name})"
        if msg not in self.errors and len(self.errors) < 5:
            self.errors.append(msg)

    def tick(self) -> None:
        e, src = self.engine, self.source
        t0, g0, p0, h0 = time.perf_counter(), src.gen_seconds, src.pages, src.hits
        got = True
        try:
            got = e.tick()
        except Exception as exc:  # noqa: BLE001 — Engine.run_forever's handler
            self.tick_failures += 1
            self.note(exc)
            if e.db.closed:
                try:
                    e.db = e._connect()
                except Exception:  # noqa: BLE001
                    pass
            e.sleep(MAX_BACKOFF_SECONDS)
        took = time.perf_counter() - t0 - (src.gen_seconds - g0)
        self.engine_seconds += took
        self.clock.t += took + (src.pages - p0) * self.a.es_ms / 1000 + (src.hits - h0) * self.a.es_us_per_hit / 1e6
        if not got:
            e.sleep(self.a.poll_interval)
        if not self.lags or int(self.clock.t) != int(self.lags[-1][0]):
            self.lags.append((self.clock.t, max(0.0, self.clock.t - e._watermark_ms / 1000)))

    def until(self, t_end: float, hook=None, stop=None) -> None:
        checked = self.clock.t
        while self.clock.t < t_end:
            if hook:
                hook(self)
            self.tick()
            if stop and stop(self):
                return
            if self.clock.t - checked >= 60:
                checked = self.clock.t
                if shutil.disk_usage("/").free < MIN_FREE:
                    raise DiskLow()


def rss_reset() -> None:
    try:
        Path("/proc/self/clear_refs").write_text("5")
    except OSError:
        pass


def rss_peak_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return 0.0


def pg_snapshot(c, schema: str) -> dict:
    return c.execute(
        "SELECT pg_current_wal_lsn()::text AS lsn, (SELECT coalesce(sum(pg_total_relation_size(r.oid)), 0) "
        "FROM pg_class r JOIN pg_namespace n ON n.oid = r.relnamespace WHERE n.nspname = %s AND r.relkind = 'r') AS size, "
        "(SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) FROM pg_stat_user_tables WHERE schemaname = %s) AS tup",
        (schema, schema)).fetchone()


def rows_per_minute(a, world, eps: float) -> float:
    """hourly_counts + both module tables per closed minute, plus templates_seen per 2 s flush."""
    rpm = eps * 60 / a.services
    touched = sum(1 - math.exp(-rpm * t.share * a.flush_seconds / 60) for ts in world.values() for t in ts)
    return a.services * a.templates * 3.4 + touched * 60 / a.flush_seconds


@dataclass
class Scenario:
    name: str
    minutes: float                      # simulated minutes after T0
    eps: float = 0                      # 0 = --eps
    backlog_minutes: float = 0          # read from T0 - backlog
    extra_rows: float = 0
    streams: object = None              # (a, world) -> {svc: cuts} , [extra streams]
    hook: object = None
    stop: object = None
    measure: object = None


def run_scenario(a, cfg, world, sc: Scenario, admin, bg_wal: float) -> dict:
    eps = sc.eps or a.eps
    a2 = argparse.Namespace(**{**vars(a), "eps": eps})
    # a backlog is flushed every flush.max_events events, not every 2 s: each flush touches at most every template
    backlog = sc.backlog_minutes * (a.services * a.templates * 3.4
                                    + eps * 60 / cfg.flush.max_events * a.services * a.templates)
    est = int((rows_per_minute(a2, world, eps) * sc.minutes + backlog + sc.extra_rows
               + a.services * (a.templates + 4) * 4 * len(HISTORY)) * ROW_BYTES * 2)
    free = shutil.disk_usage("/").free
    limit = min(a.max_bytes, free // 2) if a.max_bytes else free // 2
    print(f"\n## {sc.name}: estimated write {est / 2**20:,.0f} MB, limit {limit / 2**20:,.0f} MB "
          f"(free on / {free / 2**20:,.0f} MB)", flush=True)
    if est > limit:
        return {"refused": f"~{est / 2**20:,.0f} MB over the {limit / 2**20:,.0f} MB limit"}

    read_from = T0 - sc.backlog_minutes * 60
    t_end = T0 + sc.minutes * 60
    span = (int(read_from * 1000), int((t_end + 600) * 1000))
    cuts, extra = sc.streams(a2, world) if sc.streams else ({}, [])
    streams = [s for svc in world for s in base_streams(world, a2, svc, *span, cuts.get(svc, ()))] + extra
    hours = sorted({day_hour(int(m))[1] for m in range(int(read_from // 60), int(t_end // 60) + 61, 30)})
    schema = f"stress_pipe_{uuid.uuid4().hex[:8]}"
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    engine = None
    try:
        burst = world["svc00"][0]
        t_setup = time.perf_counter()
        setup(a2, cfg, world, schema, hours, burst, read_from)
        setup_s = time.perf_counter() - t_setup
        before = pg_snapshot(admin, schema)
        cfg.database.dsn = schema_dsn(a.dsn, schema)
        cfg.elasticsearch.index = "stress-*"
        clock = Clock(T0)
        source = GenSource(clock, streams, cfg.elasticsearch.batch_size)
        engine = Engine(cfg, connect(cfg.database.dsn), source, now=clock)
        rss_reset()
        wall0 = time.perf_counter()
        engine.start()
        run = Run(a2, engine, clock, source, read_from)
        run.wrap()
        try:
            run.until(t_end, sc.hook, sc.stop)
            engine._flush()   # write the minutes closed since the last flush
        except Stalled:
            pass
        except DiskLow:
            run.errors.append("stopped: / under 600 MB free")
        wall = time.perf_counter() - wall0
        rss = rss_peak_mb()
        boundary = engine.owner.boundary_ms
        late = engine.owner.minutes.late
        events = int(engine.metrics.events._value.get())
        for conn in (engine.conn, engine.db):
            conn.close()
        time.sleep(0.5)   # backends' table stats reach pg_stat on exit
        after = pg_snapshot(admin, schema)
        top = admin.execute("SELECT regexp_replace(relname, '_[0-9]{8}$', '') AS t, sum(n_tup_ins) AS ins, "
                            "sum(n_tup_upd) AS upd, sum(n_tup_hot_upd) AS hot FROM pg_stat_user_tables "
                            "WHERE schemaname = %s GROUP BY 1 ORDER BY sum(n_tup_ins + n_tup_upd) DESC LIMIT 4",
                            (schema,)).fetchall()
        wal = admin.execute("SELECT pg_wal_lsn_diff(%s, %s)::float8 AS d", (after["lsn"], before["lsn"])).fetchone()["d"]
        sim_h = (clock.t - T0 + sc.backlog_minutes * 60) / 3600
        with psycopg.connect(schema_dsn(a.dsn, schema), row_factory=dict_row) as c:
            counted = c.execute("SELECT coalesce(sum(sum), 0) AS n FROM hourly_counts WHERE key = '*total' AND day >= %s",
                                (DAY0,)).fetchone()["n"]
            changes = c.execute("SELECT service, key, extract(epoch FROM at) AS at, from_state, to_state "
                                "FROM alert_changes ORDER BY at").fetchall()
            res = {
                "setup_s": setup_s, "wall_s": wall, "sim_min": (clock.t - read_from) / 60, "events": events,
                "engine_s": run.engine_seconds, "gen_s": source.gen_seconds,
                "engine_eps": events / run.engine_seconds if run.engine_seconds else 0,
                "sim_eps": events / max(1e-9, clock.t - T0),
                "poll_ms": [1000 * pct(run.polls, q) for q in (0.5, 0.95, 1)],
                "handle_ms": [1000 * pct(run.handles, q) for q in (0.5, 0.95, 1)],
                "flush_ms": [1000 * pct([f for f, _ in run.flushes], q) for q in (0.5, 0.95, 1)],
                "flush_closed_ms": [1000 * pct([f for f, cl in run.flushes if cl], q) for q in (0.5, 0.95, 1)],
                "flushes": len(run.flushes), "flush_failures": run.flush_failures, "tick_failures": run.tick_failures,
                "lag_s": [pct([x for _, x in run.lags], q) for q in (0.5, 0.95, 1)],
                "rss_mb": rss, "wal_mb": wal / 2**20, "wal_bg_corrected_mb": max(0, wal - bg_wal * wall) / 2**20,
                "table_growth_mb": (after["size"] - before["size"]) / 2**20, "row_writes": after["tup"] - before["tup"],
                "top_writes (ins/upd/hot)": "; ".join(f"{r['t']} {r['ins']}/{r['upd']}/{r['hot']}" for r in top),
                "pg_mb_per_sim_hour": max(0, wal - bg_wal * wall) / 2**20 / sim_h if sim_h else 0,
                "estimate_mb": est / 2**20,
                "truth": source.truth(int(read_from * 1000), boundary), "counted": int(counted), "late": late,
                "stalled": run.stalled, "errors": run.errors,
                "changes": [dict(r) for r in changes],
                "templates": c.execute("SELECT count(*) AS n FROM templates").fetchone()["n"],
            }
            res["lost"] = res["truth"] - res["counted"] - late
            if sc.measure:
                res.update(sc.measure(a2, world, c, run, res))
        return res
    finally:
        if engine is not None:
            for conn in (engine.conn, engine.db):
                if not conn.closed:
                    conn.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.execute("CHECKPOINT")


# --- scenarios -------------------------------------------------------------------------------------------

def svc(a, i: int) -> str:
    return f"svc{i % a.services:02d}"


def ms(minutes: float) -> int:
    return int((T0 + minutes * 60) * 1000)


def first_change(changes, svc, key, to, after: float = 0) -> float | None:
    """Minutes from `after` (sim minutes after T0) to the end of the minute that made the change."""
    for r in changes:
        if r["service"] == svc and r["key"] == key and r["to_state"] == to and float(r["at"]) > T0 + after * 60:
            return (float(r["at"]) - T0) / 60 - after
    return None


def raised(changes, expected=lambda r: False) -> list[str]:
    return sorted({f"{r['service']} {r['key']}" for r in changes
                   if r["to_state"] in ("WARMING", "ALERTING") and not expected(r)})


def burst_streams(a, world):
    t = world["svc00"][0]   # the busiest ERROR template
    rpm = round(19 * a.eps * 60 / a.services * t.share)
    return {}, [Stream("burst", "svc00", ms(5), ms(15), rpm, [t])]


def burst_measure(a, world, c, run, res):
    t, ch = world["svc00"][0], res["changes"]
    keys = {"template": t.tid, "doc": "doc:stress", "*error": "*error"}
    out = {f"{n} {to}": first_change(ch, "svc00", k, to, 5) for n, k in keys.items() for to in ("WARMING", "ALERTING")}
    out.update({f"{n} NORMAL after burst": first_change(ch, "svc00", k, "NORMAL", 15) for n, k in keys.items()})
    out["burst template"] = f"{t.tid} ({60 * a.eps / a.services * t.share:.0f}/min normal)"
    out["alert_changes rows"] = sum(r["service"] == "svc00" and r["key"] in keys.values() for r in ch)
    out["alert_minutes rows"] = c.execute("SELECT count(*) AS n FROM alert_minutes").fetchone()["n"]
    out["false alerts"] = raised(ch, lambda r: r["service"] == "svc00" and r["key"] in keys.values())
    return out


def drop_streams(a, world):
    return {"svc01": [(ms(5), ms(20), {"rpm": a.eps * 60 // a.services // 10})]}, []


def drop_measure(a, world, c, run, res):
    ch = res["changes"]
    return {"*total WARMING": first_change(ch, "svc01", "*total", "WARMING", 5),
            "*total ALERTING": first_change(ch, "svc01", "*total", "ALERTING", 5),
            "*total NORMAL after": first_change(ch, "svc01", "*total", "NORMAL", 20),
            "false alerts": raised(ch, lambda r: r["service"] == "svc01" and r["key"] == "*total")}


def storm_streams(a, world):
    return {}, [Stream("storm", svc(a, 4), ms(5), ms(10), a.storm_templates * 6 // 5, storm_templates(a, svc(a, 4)))]


def storm_measure(a, world, c, run, res):
    s = svc(a, 4)
    return {"templates before": a.services * a.templates,
            "templates of the storm service": c.execute("SELECT count(*) AS n FROM templates WHERE service = %s",
                                                        (s,)).fetchone()["n"],
            "unparsed events": c.execute("SELECT coalesce(sum(sum), 0) AS n FROM hourly_counts "
                                         "WHERE key LIKE '%%:unparsed' AND day >= %s", (DAY0,)).fetchone()["n"],
            "error_samples rows": c.execute("SELECT count(*) AS n FROM error_samples").fetchone()["n"],
            "drain3 blob KB": c.execute("SELECT length(blob) / 1024 AS n FROM drain3_state WHERE service = %s",
                                        (s,)).fetchone()["n"],
            "alerts raised": raised(res["changes"])}


def late_streams(a, world):
    return {svc(a, 2): [(ms(5), ms(15), {"delay_ms": 180_000})]}, []


def late_measure(a, world, c, run, res):
    return {"alerts raised": raised(res["changes"]),
            "*total ALERTING": first_change(res["changes"], svc(a, 2), "*total", "ALERTING", 5)}


def outage_hook(t_from: float, t_to: float):
    state = {"down": False, "done": False}
    real = engine_mod.connect

    def flaky(dsn):
        if state["down"]:
            raise psycopg.OperationalError("stress: database unreachable")
        return real(dsn)

    def hook(run):
        t = run.clock.t
        if not state["done"] and t >= t_from:
            state["done"] = state["down"] = True
            engine_mod.connect = flaky
            run.engine.conn.close()
            run.engine.db.close()
        elif state["down"] and t >= t_to:
            state["down"] = False
        if state["done"] and not state["down"] and not hasattr(run, "recovered") and run.lags and run.lags[-1][1] < 60 \
                and run.last_ok > t_to:
            run.recovered = t
    hook.restore = lambda: setattr(engine_mod, "connect", real)
    return hook


def outage_measure(a, world, c, run, res):
    totals = c.execute("SELECT key, sum(sum) AS n FROM hourly_counts WHERE day >= %s GROUP BY 1", (DAY0,)).fetchall()
    tmpl = sum(r["n"] for r in totals if not r["key"].startswith("*"))
    return {"lag max s": res["lag_s"][2],
            "recovered (lag < 60 s) min after the outage": (getattr(run, "recovered", math.nan) - (T0 + 7 * 60)) / 60,
            "template keys sum = *total": tmpl == res["counted"],
            "double counted": max(0, res["counted"] - res["truth"]), "missing": max(0, res["truth"] - res["counted"])}


def bad_streams(where: str, char: str):
    def streams(a, world):
        return {}, [Stream(f"bad-{where}", "svc00", ms(2), ms(4), 3, world["svc00"][:1], bad=(where, char))]
    return streams


def module_streams(a, world):
    return {svc(a, 3): [(ms(5), ms(10), {"module_fn": lambda h: f"req-{(h >> 16) % a.module_values:05d}"})]}, []


def module_measure(a, world, c, run, res):
    q = lambda s: c.execute(s).fetchone()["n"]  # noqa: E731
    return {"modules rows": q("SELECT count(*) AS n FROM modules"),
            "template_modules rows": q("SELECT count(*) AS n FROM template_modules"),
            "template_module_hours rows": q("SELECT count(*) AS n FROM template_module_hours WHERE day >= '2026-10-21'"),
            "flush with closed minute max ms": res["flush_closed_ms"][2]}


def caught_up(run) -> bool:
    return bool(run.lags) and run.engine._caught_up() and run.lags[-1][1] < 60


def scenarios(a) -> dict[str, Scenario]:
    out = {
        "N1": Scenario("N1", 30),
        "N1-10k": Scenario("N1-10k", 30, eps=a.eps * 5),
        "N2": Scenario("N2", 30, backlog_minutes=a.backlog_minutes, stop=caught_up),
        "I1": Scenario("I1", 30, streams=burst_streams, measure=burst_measure),
        "I2": Scenario("I2", 30, streams=drop_streams, measure=drop_measure),
        "I3": Scenario("I3", 20, streams=storm_streams, measure=storm_measure, extra_rows=a.storm_templates * 30),
        "I4": Scenario("I4", 25, streams=late_streams, measure=late_measure),
        "I5": Scenario("I5", 15, hook=outage_hook(T0 + 5 * 60, T0 + 7 * 60), measure=outage_measure),
        "I7": Scenario("I7", 20, streams=module_streams, measure=module_measure, extra_rows=a.module_values * 40),
    }
    for where, field_ in (("message", "message"), ("service", "service_code2"), ("module", "moduleCode")):
        for name, char in (("NUL", "\x00"), ("surrogate", "\ud83d")):
            out[f"I6-{name}-{where}"] = Scenario(f"I6-{name}-{where}", 8, streams=bad_streams(field_, char))
    return out


# --- E1: real Elasticsearch ------------------------------------------------------------------------------

def run_e1(a, cfg, world) -> dict:
    from elasticsearch import Elasticsearch, helpers

    from logai2.reader.cache import KnownLines
    from logai2.reader.es_source import EsSource
    from logai2.reader.reader import Reader

    free = shutil.disk_usage("/").free
    if free < 1.5 * 2**30:
        return {"skipped": f"free on / {free / 2**20:,.0f} MB < 1.5 GB"}
    es = Elasticsearch(a.es_host, request_timeout=60)
    index = f"stress-pipeline-{uuid.uuid4().hex[:8]}"
    now = time.time()
    clock = Clock(now)
    # the docs spread over the last 10 minutes; 250k docs = 32 MB, so the cap keeps the index under ~100 MB
    rpm = max(1, min(a.e1_docs, 750_000) // 10 // a.services)
    streams = [Stream(f"e1-{svc}", svc, int((now - 660) * 1000), int((now - 60) * 1000), rpm, world[svc])
               for svc in world]
    res: dict = {"index": index}
    try:
        es.indices.create(index=index, settings={"number_of_shards": 1, "number_of_replicas": 0},
                          mappings={"properties": {"@timestamp": {"type": "date"}, "service_code2": {"type": "keyword"},
                                                   "moduleCode": {"type": "keyword"}, "message": {"type": "text"}}})
        t0 = time.perf_counter()
        docs = ({"_index": index, "_id": h["_id"], "_source": h["_source"]}
                for s in streams for h in s.hits(0, 2**62, 10**9, 2**62))
        ok, _ = helpers.bulk(es.options(request_timeout=120), docs, chunk_size=5000)
        es.indices.refresh(index=index)
        res.update(docs=ok, bulk_s=time.perf_counter() - t0,
                   index_mb=es.indices.stats(index=index)["_all"]["primaries"]["store"]["size_in_bytes"] / 2**20)
        cfg.elasticsearch.hosts = [a.es_host]
        source = EsSource(cfg.elasticsearch, cfg.reader)
        pages = []
        page = source.page

        def timed(index_, from_ms, upper_ms):
            t = time.perf_counter()
            hits = page(index_, from_ms, upper_ms)
            pages.append((time.perf_counter() - t, len(hits), hits[-1]["sort"][0] if hits else None))
            return hits
        source.page = timed
        reader = Reader(source, cfg, KnownLines(cfg.reader.cache_size), now=time.time)
        reader.set_sources({index: int((now - 700) * 1000)}, {})
        t0, events = time.perf_counter(), 0
        while True:
            batch = reader.poll()
            events += batch["events"]
            if batch["positions"][index]["caught_up"] and batch["events"] == 0:
                break
        read_s = time.perf_counter() - t0
        full = [p for p in pages if p[1] >= 5000]
        res.update(read_events=events, read_s=read_s, read_eps=events / read_s,
                   page_ms_full=1000 * sum(p[0] for p in full) / max(1, len(full)), full_pages=len(full),
                   page_ms_small=1000 * pct([p[0] for p in pages if p[1] < 100], 0.5))
        # live: docs stamped now, bulk-indexed every 0.5 s while the reader polls
        live = Stream("e1-live", "svc00", int(time.time() * 1000), int((time.time() + 60) * 1000), 30_000,
                      world["svc00"])
        cfg.reader.upper_bound_seconds = 0   # measure ES visibility; the engine adds upper_bound_seconds (30 s)
        reader = Reader(source, cfg, KnownLines(cfg.reader.cache_size), now=time.time)
        reader.set_sources({index: int(time.time() * 1000)}, {})
        delays, sent_to, stop_at = [], int(time.time() * 1000), time.time() + 30
        pages.clear()
        while time.time() < stop_at:
            t_ms = int(time.time() * 1000)
            helpers.bulk(es, ({"_index": index, "_id": h["_id"], "_source": h["_source"]}
                              for h in live.hits(sent_to, t_ms - 1, 10**9, 2**62)))
            sent_to = t_ms
            n0 = len(pages)
            reader.poll()
            for took, n, last in pages[n0:]:
                if last is not None:
                    delays.append(time.time() - last / 1000)
            time.sleep(0.2)
        res.update(live_eps=30_000 / 60, live_delay_s=[pct(delays, q) for q in (0.5, 0.95, 1)])
    finally:
        es.indices.delete(index=index, ignore_unavailable=True)
        res["deleted"] = not es.indices.exists(index=index)
    return res


# --- main ------------------------------------------------------------------------------------------------

TINY = dict(services=2, templates=20, eps=20, storm_templates=60, module_values=100, backlog_minutes=20,
            es_ms=500.0, e1_docs=2000)


def fmt(v) -> str:
    if isinstance(v, float):
        return "-" if math.isnan(v) else f"{v:,.2f}"
    if isinstance(v, list) and v and isinstance(v[0], float):
        return " / ".join(f"{x:,.1f}" for x in v)
    return str(v) if v is not None else "never"


def run(argv: list[str] | None = None) -> tuple[int, dict]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--scenarios", default="all")
    ap.add_argument("--scale", choices=("default", "tiny"), default="default")
    ap.add_argument("--services", type=int, default=10)
    ap.add_argument("--templates", type=int, default=300)
    ap.add_argument("--eps", type=int, default=2000)
    ap.add_argument("--storm-templates", type=int, default=5000)
    ap.add_argument("--module-values", type=int, default=10_000)
    ap.add_argument("--backlog-minutes", type=float, default=120)
    ap.add_argument("--es-ms", type=float, default=2.5, help="modelled ES latency per page (E1: 2.4 ms small page)")
    ap.add_argument("--es-us-per-hit", type=float, default=10.0, help="modelled ES latency per returned hit (E1: 10k page 104 ms)")
    ap.add_argument("--es-host", default="http://localhost:9200")
    ap.add_argument("--e1-docs", type=int, default=250_000)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--max-bytes", type=int)
    ap.add_argument("--json")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.scale == "tiny":
        vars(a).update(TINY)
    a.poll_interval, a.flush_seconds = 1.0, 2.0
    logging.basicConfig(level=logging.WARNING if a.verbose else logging.CRITICAL)
    if public_is_migrated(a.dsn):
        print("REFUSED: this database has a migrated public schema (real data); use an empty database", file=sys.stderr)
        return 3, {}
    cfg = load_config(None)
    a.flush_seconds = cfg.flush.seconds
    world = build_world(a)
    table = scenarios(a)
    names = [n for n in table] + ["E1"] if a.scenarios == "all" else a.scenarios.split(",")
    names = [n2 for n in names for n2 in ([k for k in table if k.startswith("I6")] if n == "I6" else [n])]
    results: dict[str, dict] = {}
    with psycopg.connect(a.dsn, autocommit=True, row_factory=dict_row) as admin:
        lsn = admin.execute("SELECT pg_current_wal_lsn()::text AS l").fetchone()["l"]
        time.sleep(5)
        bg_wal = admin.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)::float8 AS d", (lsn,)).fetchone()["d"] / 5
        print(f"background WAL (other databases): {bg_wal / 1024:,.1f} KB/s")
        refused = False
        for name in names:
            if name == "E1":
                results[name] = run_e1(a, load_config(None), world)
            else:
                sc = table[name]
                try:
                    results[name] = run_scenario(a, load_config(None), world, sc, admin, bg_wal)
                finally:
                    if hasattr(sc.hook, "restore"):
                        sc.hook.restore()
            r = results[name]
            refused |= "refused" in r
            print(f"\n| {name} | value |\n|---|---|")
            for k, v in r.items():
                if k != "changes":
                    print(f"| {k} | {fmt(v)} |")
            print(flush=True)
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1, default=str))
    return (2 if refused else 0), results


def main(argv: list[str] | None = None) -> int:
    return run(argv)[0]


if __name__ == "__main__":
    sys.exit(main())

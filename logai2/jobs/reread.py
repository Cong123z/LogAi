"""Re-read: the plan (pure) and the lane that reads chosen service-hours again from Elasticsearch.

The lane never touches the database: it reads one whole local hour at a time into its own OpenMinutes
and hands the closed hours (LaneHour) to the owner, whose flush replaces the stored hour and records the
run's position in the same transaction.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from logai2.config import AppConfig
from logai2.owner.counts import ERROR_LEVELS, SEVERITY, OpenMinutes
from logai2.reader.es_source import hit_to_line
from logai2.reader.normalize import normalize
from logai2.timeutil import TZ, local, minute_of

logger = logging.getLogger("logai2.reread")

HOUR_MS = 3_600_000
SAMPLES_KEPT = 20
RATE_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Scope:
    services: list[str] | None
    days: list[date]            # local dates
    hour_from: int
    hour_to: int                # 0 <= hour_from < hour_to <= 24

    @classmethod
    def from_json(cls, d: dict) -> "Scope":
        return cls(d.get("services"), [date.fromisoformat(x) for x in d["days"]], int(d["hour_from"]),
                   int(d["hour_to"]))

    def to_json(self) -> dict:
        return {"services": self.services, "days": [d.isoformat() for d in self.days],
                "hour_from": self.hour_from, "hour_to": self.hour_to}


@dataclass(frozen=True)
class Window:
    start_ms: int
    end_ms: int
    patterns: tuple[str, ...]   # patterns to read
    services: tuple[str, ...]   # services whose hour is replaced
    by_pattern: tuple[tuple[str, tuple[str, ...]], ...] = ()   # (pattern, the window's services it holds)


@dataclass
class Plan:
    windows: list[Window]
    services: list[str]
    patterns: list[str]
    hours_chosen: int = 0       # services x hours
    hours_not_in_es: int = 0
    hours_live: int = 0         # at or after the owner's hour
    hours_to_read: int = 0      # service-hours in windows


def plan(scope: Scope, patterns: dict[str, list[str]], es_oldest: dict[str, int | None],
         owner_hour_ms: int) -> Plan:
    holding: dict[str, list[str]] = defaultdict(list)   # service -> selected patterns holding it
    for pattern, services in patterns.items():
        for s in services:
            holding[s].append(pattern)
    services = sorted(holding if scope.services is None else set(scope.services) & set(holding))

    def oldest(s: str) -> int | None:
        values = [es_oldest.get(p) for p in holding[s]]
        return None if None in values else max(values)

    out = Plan([], services, sorted({p for s in services for p in holding[s]}))
    for day in sorted(scope.days):
        for hour in range(scope.hour_from, scope.hour_to):
            start = int(datetime(day.year, day.month, day.day, hour, tzinfo=TZ).timestamp() * 1000)
            end = start + HOUR_MS
            out.hours_chosen += len(services)
            if end > owner_hour_ms:   # live still writes this hour
                out.hours_live += len(services)
                continue
            ok = [s for s in services if (o := oldest(s)) is not None and start >= o]
            out.hours_not_in_es += len(services) - len(ok)
            if ok:
                pats = sorted({p for s in ok for p in holding[s]})
                out.windows.append(Window(start, end, tuple(pats), tuple(ok), tuple(
                    (p, tuple(s for s in ok if p in holding[s])) for p in pats)))
                out.hours_to_read += len(ok)
    return out


@dataclass
class LaneHour:
    service: str
    day: date
    hour: int
    n_minutes: int = 0
    sums: dict[str, tuple[int, int]] = field(default_factory=dict)                       # key -> (sum, sumsq)
    module_hours: dict[tuple[str, str], int] = field(default_factory=dict)               # (template_id, module) -> n
    pairs: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)          # -> (first, last) minute
    samples: list[tuple[str, int, str]] = field(default_factory=list)                    # (template_id, minute, line)
    templates: dict[str, dict] = field(default_factory=dict)                             # id -> text/level/first/last
    minutes: dict[str, list[int]] = field(default_factory=dict)                          # key -> 60 counts (slot = local minute)


class Lane:
    """Reads the plan's windows in time order, one whole hour at a time. `pos_ms` is the end of the last
    completed window (the resume point); windows that start before the given pos_ms are done."""

    def __init__(self, plan: Plan, run_id: int, pos_ms: int, source, miners, cache, first: dict[str, int],
                 cfg: AppConfig, targets: dict[str, str] | None = None):
        self.run_id = run_id
        self.windows = [w for w in plan.windows if w.start_ms >= (pos_ms or 0)]
        self.pos_ms = pos_ms or 0
        self.source, self.miners, self.cache, self.cfg = source, miners, cache, cfg
        self.targets = targets or {}
        self.first = first
        self.page_size = cfg.elasticsearch.batch_size
        self.hours_total = sum(len(w.services) for w in self.windows)
        self.hours_done = 0     # of the windows handed out by take_ready
        self.docs_read = 0
        self.docs_committed = 0  # docs of the windows handed out (a resume re-reads the rest)
        self._ready: list[tuple] = []   # closed windows not yet handed out: (end_ms, n services, docs, hours, learned)
        self.new_templates = 0
        self.skipped = 0   # service-hours whose index was deleted by retention during the run
        self.seen: dict[tuple, int] = {}   # (_index, _id) -> ms, only ids at the current position ms
        self._i = 0
        self._seen_first: dict[str, int] = {}   # service -> first minute any doc was seen
        self._rate: deque[tuple[float, int]] = deque()
        self._window: Window | None = None

    @property
    def done(self) -> bool:
        return self._i >= len(self.windows) and not self._ready

    def take_ready(self) -> list[LaneHour]:
        """Hand out the hours of the windows closed so far. Only now do pos_ms, hours_done and the shared cache
        move: they never run ahead of what the owner was given, so a failing page cannot lose a closed hour
        (the engine calls this also on its error path)."""
        out: list[LaneHour] = []
        for end_ms, n, docs, hours, learned in self._ready:
            for (service, text), tid in learned.items():
                self.cache.put(service, text, tid)
            self.pos_ms = end_ms
            self.hours_done += n
            self.docs_committed += docs
            out += hours
        self._ready = []
        return out

    # --- reading -----------------------------------------------------------

    def step(self, budget_s: float, now: float) -> list[LaneHour]:
        """Read pages until the budget is used (at least one page); returns the windows completed."""
        deadline = time.monotonic() + budget_s
        while self._i < len(self.windows):
            w = self.windows[self._i]
            if self._window is not w:
                self._begin(w)
            while self._p < len(w.patterns):
                self._page(w, w.patterns[self._p])
                if time.monotonic() >= deadline:
                    break
            if self._p >= len(w.patterns):
                self._ready.append(self._close(w))
                self._window = None
                self._i += 1
            if time.monotonic() >= deadline:
                break
        self._rate.append((now, self.docs_read))
        while len(self._rate) > 1 and self._rate[0][0] < now - RATE_WINDOW_SECONDS:
            self._rate.popleft()
        return self.take_ready()

    def _begin(self, w: Window) -> None:
        self._window = w
        self._p = 0                       # index of the pattern being read
        self._pos = w.start_ms
        self._got = False                 # the current pattern returned a doc for this window
        self.seen = {}
        self._agg: dict[tuple, list] = {}  # (service, template_id, level, module, minute) -> [n, sample]
        self._tmpl: dict[tuple[str, str], dict] = {}
        self._learned: dict[tuple[str, str], str] = {}
        self._gone: set[str] = set()      # patterns that lost this window to retention
        self._docs0 = self.docs_read
        self._unparsed: set[str] = set()

    def _target(self, pattern: str) -> str:
        return self.targets.get(pattern, pattern)

    def _page(self, w: Window, pattern: str) -> None:
        hits = self.source.page(self._target(pattern), self._pos, w.end_ms - 1,
                                timeout=self.cfg.retrain.es_timeout_seconds)
        services = set(w.services)
        new = 0
        for h in hits:
            h = h if isinstance(h, dict) else {}
            key = (h.get("_index"), h.get("_id"))
            if key in self.seen:
                continue
            try:
                ts_ms = int((h.get("sort") or [self._pos])[0])
            except (TypeError, ValueError):
                continue
            self._pos = max(self._pos, ts_ms)
            if key[1] is None:
                continue
            self.seen[key] = ts_ms
            new += 1
            self.docs_read += 1
            line = hit_to_line(h, self.cfg.reader.module_field, self.cfg.reader.max_line_bytes)
            if line is not None and line.service in services:
                self._add(line)
        self._got = self._got or bool(hits)
        # only the ids at the position ms can come back on the next page
        self.seen = {k: t for k, t in self.seen.items() if t >= self._pos}
        if len(hits) < self.page_size or new == 0:   # reached the window's end (or the reader's stall guard)
            if not self._got:
                self.note_es_gone(w, pattern)
            self._p += 1
            self._pos = w.start_ms
            self._got = False
            self.seen = {}

    def note_es_gone(self, window: Window, pattern: str | None = None) -> None:
        """A pattern returned nothing for this window: when ES no longer holds the window (retention deleted its
        index since the plan), its services are skipped: zeros would replace real counts."""
        patterns = window.patterns if pattern is None else (pattern,)
        try:
            oldest = min((o for p in patterns if (o := self.source.oldest_ms(self._target(p))) is not None),
                         default=None)
            gone = oldest is None or window.start_ms < oldest
        except Exception as exc:  # noqa: BLE001 — cannot tell: an empty hour is a quiet hour
            logger.warning("could not re-check the oldest doc of %s: %s", ", ".join(patterns), exc)
            return
        if gone:
            logger.warning("window %s of %s is no longer in Elasticsearch; its services are skipped, not zeroed",
                           datetime.fromtimestamp(window.start_ms / 1000, TZ).strftime("%Y-%m-%d %H:00"),
                           ", ".join(patterns))
            self._gone |= set(patterns)

    def _add(self, line) -> None:
        service = line.service
        try:
            text = normalize(line.message)
        except Exception:  # noqa: BLE001 — counted as <service>:unparsed like the owner does
            text = None
        template_id = text_out = None
        if text is not None:
            template_id = self.cache.get(service, text) or self._learned.get((service, text))
        if template_id is None:
            try:
                if text is None:
                    raise ValueError("the line could not be normalized")
                template_id, text_out, is_new = self.miners.match(service, text)
                self._learned[(service, text)] = template_id
                self.new_templates += is_new
            except Exception:  # noqa: BLE001 — one bad line must not stop the lane
                template_id, text_out = f"{service}:unparsed", "(unparsed)"
                if service not in self._unparsed:
                    self._unparsed.add(service)
                    logger.exception("could not template a %s line; counted as %s", service, template_id)
        minute = minute_of(line.ts)
        self._seen_first[service] = min(self._seen_first.get(service, minute), minute)
        t = self._tmpl.get((service, template_id))
        if t is None:
            self._tmpl[(service, template_id)] = {"text": text_out, "level": line.level, "first": minute,
                                                  "last": minute}
        else:
            t["text"] = text_out or t["text"]
            if SEVERITY.get(line.level.upper(), 0) > SEVERITY.get(t["level"].upper(), 0):
                t["level"] = line.level
            t["first"], t["last"] = min(t["first"], minute), max(t["last"], minute)
        key = (service, template_id, line.level, line.module, minute)
        acc = self._agg.get(key)
        if acc is None:
            self._agg[key] = [1, line.message]
        else:
            acc[0] += 1

    # --- closing -----------------------------------------------------------

    def _close(self, w: Window) -> tuple:
        start_min, end_min = w.start_ms // 60_000, w.end_ms // 60_000
        # a service is skipped when ANY pattern that feeds it lost the window: a partial read is never written
        holders = w.by_pattern or tuple((p, w.services) for p in w.patterns)
        gone = {s for p, ss in holders if p in self._gone for s in ss}
        om = OpenMinutes(start_min)
        for (service, tid, level, module, minute), (n, _) in self._agg.items():
            om.add(service, tid, level, module, minute, n)
        keep = [s for s in w.services if s not in gone]
        self.skipped += len(w.services) - len(keep)
        first_map = {}
        for s in keep:   # a service seen nowhere (no DB first, no doc) is not written
            known = min(self.first.get(s, end_min), self._seen_first.get(s, end_min))
            if known < end_min:
                first_map[s] = max(start_min, known)
        hours: dict[str, LaneHour] = {}
        for cm in om.close_before(end_min, first_map):
            for (service, key), n in cm.keys.items():
                if service in gone:
                    continue
                h = hours.get(service) or hours.setdefault(service, LaneHour(service, cm.day, cm.hour))
                slot = local(cm.minute).minute
                if key == "*total":
                    h.n_minutes += 1
                    h.minutes.setdefault("*minutes", [0] * 60)[slot] = 1
                if n:
                    s, sq = h.sums.get(key, (0, 0))
                    h.sums[key] = (s + n, sq + n * n)
                    h.minutes.setdefault(key, [0] * 60)[slot] += n
            for (service, tid, module), n in cm.modules.items():
                if tid.endswith(":unparsed") or service in gone:
                    continue
                h = hours[service]
                h.module_hours[(tid, module)] = h.module_hours.get((tid, module), 0) + n
                a, b = h.pairs.get((tid, module), (cm.minute, cm.minute))
                h.pairs[(tid, module)] = (min(a, cm.minute), max(b, cm.minute))
        samples: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=SAMPLES_KEPT))
        for (service, tid, level, module, minute), (_, sample) in sorted(self._agg.items(), key=lambda kv: kv[0][4]):
            if level.upper() in ERROR_LEVELS and service in hours:
                samples[(service, tid)].append((tid, minute, sample))
        for (service, tid), t in self._tmpl.items():
            if service in hours:
                hours[service].templates[tid] = t
        for (service, _), q in samples.items():
            hours[service].samples += q
        # shared with the cache only once handed out (take_ready), and only for services that are written
        learned = {k: t for k, t in self._learned.items() if k[0] in hours}
        return (w.end_ms, len(w.services), self.docs_read - self._docs0,
                [h for h in hours.values() if h.n_minutes], learned)

    def progress(self) -> dict:
        rate = 0.0
        if len(self._rate) > 1 and self._rate[-1][0] > self._rate[0][0]:
            rate = (self._rate[-1][1] - self._rate[0][1]) / (self._rate[-1][0] - self._rate[0][0])
        return {"docs_read": self.docs_read, "docs_committed": self.docs_committed, "hours_done": self.hours_done, "hours_total": self.hours_total,
                "new_templates": self.new_templates, "rate": rate,
                "position_ms": self._pos if self._window is not None else self.pos_ms}

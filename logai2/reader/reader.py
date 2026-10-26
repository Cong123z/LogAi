"""Reader: polls each selected index pattern from its position and hands the
owner a MinuteBatch, a plain dict (a Kafka message in v2).

Each poll reads one page per pattern (one search over all its indices)
starting at its position's millisecond (inclusive), so docs sharing that
millisecond are re-read and dropped by (_index, _id). The watermark says
every doc before it has been read: a pattern whose page
did not reach upper_ms holds it at its position. When every pattern fails (ES
down) the watermark is held too: closing the outage minutes would write them
empty and drop their docs as late on recovery.
"""
from __future__ import annotations

import logging
import time
from types import MappingProxyType
from typing import Callable, Mapping, TypedDict

from logai2.config import AppConfig
from logai2.reader.cache import KnownLines
from logai2.reader.es_source import hit_to_line
from logai2.reader.normalize import normalize
from logai2.timeutil import minute_of

logger = logging.getLogger("logai2.reader")

# Consecutive page() failures after which a pattern stops holding the
# watermark: a dead pattern must not freeze minute closing for every other one.
FAILING_AFTER = 3


class Count(TypedDict):
    service: str
    template_id: str | None
    text: str | None
    level: str
    module: str
    minute: int
    n: int
    sample: str


class MinuteBatch(TypedDict):
    counts: list[Count]
    watermark_ms: int
    events: int
    positions: dict[str, dict]


class Reader:
    def __init__(self, source, cfg: AppConfig, cache: KnownLines,
                 now: Callable[[], float] = time.time):
        self.source = source
        self.cfg = cfg.reader
        self.page_size = cfg.elasticsearch.batch_size
        self.cache = cache
        self.now = now
        self.malformed = 0
        self._floors: dict[str, int] = {}
        self._pos: dict[str, int] = {}
        self._caught_up: dict[str, bool] = {}
        self._errors: dict[str, int] = {}
        # ponytail: dedup ids kept for every open minute in memory; bounded by
        # one or two minutes of docs per pattern, move to a ts-bucketed set if
        # catch-up memory shows up.
        self._seen: dict[str, dict[tuple, int]] = {}
        self._targets: dict[str, str] = {}

    @property
    def floors(self) -> Mapping[str, int]:
        return MappingProxyType(self._floors)

    def set_sources(self, floors: dict[str, int], saved: dict[str, int],
                    targets: dict[str, str] | None = None) -> None:
        """Keys are patterns; targets[pattern] is the search expression (default the pattern)."""
        for key in set(self._floors) - set(floors):
            for state in (self._pos, self._caught_up, self._errors, self._seen):
                state.pop(key, None)
        # A tracked pattern keeps its position, caught_up and dedup ids: the
        # periodic selection refresh must not rewind running patterns.
        for key in set(floors) - set(self._floors):
            self._pos[key] = saved.get(key, floors[key])
            self._caught_up[key] = False
            self._errors[key] = 0
            self._seen[key] = {}
        self._floors = dict(floors)
        self._targets = {k: (targets or {}).get(k, k) for k in floors}

    def poll(self) -> MinuteBatch:
        upper_ms = int((self.now() - self.cfg.upper_bound_seconds) * 1000)
        aggregates: dict[tuple, Count] = {}
        events = 0
        for index in self._floors:
            try:
                hits = self.source.page(self._targets[index], self._pos[index], upper_ms)
            except Exception as exc:  # noqa: BLE001 — one pattern must not stop the others
                self._errors[index] += 1
                self._caught_up[index] = False
                logger.warning("page(%s) failed (%d in a row): %s", index, self._errors[index], exc)
                continue
            self._errors[index] = 0
            seen = self._seen[index]
            new = 0
            for h in hits:
                h = h if isinstance(h, dict) else {}
                doc_id = h.get("_id")
                key = (h.get("_index"), doc_id)
                if key in seen:
                    continue
                try:
                    ts_ms = int((h.get("sort") or [self._pos[index]])[0])
                except (TypeError, ValueError):
                    self.malformed += 1
                    continue
                # Advancing on every hit (malformed too) so a junk doc never
                # pins the position; a stuck millisecond is the stall below.
                self._pos[index] = max(self._pos[index], ts_ms)
                if doc_id is None:
                    self.malformed += 1
                    continue
                seen[key] = ts_ms
                new += 1
                line = hit_to_line(h, self.cfg.module_field, self.cfg.max_line_bytes)
                if line is None:
                    self.malformed += 1
                    continue
                events += 1
                self._add(aggregates, line)
            # A page shorter than the page size reached upper_ms, even under a
            # continuous stream. Stall guard: a page of only already-seen ids
            # is caught up for this poll; the next poll tries again.
            self._caught_up[index] = len(hits) < self.page_size or new == 0
        return {
            "counts": list(aggregates.values()),
            "watermark_ms": self._watermark(upper_ms),
            "events": events,
            "positions": {i: {"pos_ms": self._pos[i], "caught_up": self._caught_up[i],
                              "failing": self._errors[i] >= FAILING_AFTER} for i in self._floors},
        }

    def _add(self, aggregates: dict[tuple, Count], line) -> None:
        try:
            text = normalize(line.message)
        except Exception:  # noqa: BLE001 — text None: the owner counts (and logs) it as <service>:unparsed
            text = None
        template_id = self.cache.get(line.service, text) if text is not None else None
        minute = minute_of(line.ts)
        key = (line.service, template_id or text, template_id is None, line.level, line.module, minute)
        count = aggregates.get(key)
        if count is None:
            aggregates[key] = {
                "service": line.service, "template_id": template_id,
                "text": text if template_id is None else None,
                "level": line.level, "module": line.module, "minute": minute,
                "n": 1, "sample": line.message,
            }
        else:
            count["n"] += 1

    def _watermark(self, upper_ms: int) -> int:
        marks = {i: upper_ms if self._caught_up[i] else self._pos[i] for i in self._floors}
        healthy = [m for i, m in marks.items() if self._errors[i] < FAILING_AFTER]
        # no healthy pattern: hold at the positions (a failing pattern is never caught up)
        return min(healthy or marks.values(), default=upper_ms)

    def learn(self, resolved: dict[tuple[str, str], str]) -> None:
        for (service, text), template_id in resolved.items():
            self.cache.put(service, text, template_id)

    def cursors(self, boundary_ms: int) -> dict[str, tuple[int, int]]:
        # ponytail: a failing pattern gets the boundary too, so its docs from
        # minutes closed while it failed are lost (they would be late counts
        # anyway); saving its older position would re-open closed minutes on
        # restart.
        return {i: (max(boundary_ms, floor), floor) for i, floor in self._floors.items()}

    def forget_before(self, boundary_ms: int) -> None:
        # Never forget ids at or after the position: the next poll re-reads
        # the position's millisecond and would count them again.
        for index, seen in self._seen.items():
            cut = min(boundary_ms, self._pos[index])
            for key in [k for k, ts in seen.items() if ts < cut]:
                del seen[key]

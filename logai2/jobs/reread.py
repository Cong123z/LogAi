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
from logai2.timeutil import TZ, minute_of

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
                out.windows.append(Window(start, end, tuple(sorted({p for s in ok for p in holding[s]})),
                                          tuple(ok)))
                out.hours_to_read += len(ok)
    return out

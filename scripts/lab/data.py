"""The lab's in-memory model of one service's recorded counts."""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from logai2.timeutil import TZ, local

Key = tuple[str, str]
UNOBSERVED = -1
MIX = "*mix"        # the M7 pseudo-key (one per service)


def slot_of(minute: int) -> tuple[date, int]:
    t = local(minute)
    return t.date(), t.hour * 60 + t.minute


def minute_at(day: date, slot: int) -> int:
    return int((datetime.combine(day, time(), TZ) + timedelta(minutes=slot)).timestamp() // 60)


@dataclass
class ServiceData:
    service: str
    days: list[date]                                  # contiguous local days of the per-minute window
    minutes: dict[str, dict[date, array]]             # key -> day -> array('i', 1440); UNOBSERVED where not observed
    observed: dict[date, array]                       # 1 observed / 0 not, from '*minutes'
    hourly: dict[tuple[str, date, int], tuple[int, int]]   # (key, day, hour) -> (sum, sumsq)
    n_minutes: dict[tuple[date, int], int]            # service_hours
    committed: set[date]
    excluded: set[tuple[date, int]]                   # Review 'exclude' hours (day, hour)
    levels: dict[str, str]                            # template_id -> level
    first_seen: dict[str, int]                        # template_id -> minute
    docs: dict[str, list[str]]                        # 'doc:<id>' -> linked template ids

    def count(self, key: str, minute: int) -> int:
        day, slot = slot_of(minute)
        a = self.minutes.get(key, {}).get(day)
        return UNOBSERVED if a is None else a[slot]

    def keys(self) -> list[str]:
        return list(self.minutes)

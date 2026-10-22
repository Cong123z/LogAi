"""Open minutes: per-minute counts held until the watermark passes, then closed.

Closed minutes include silent ones, because the baseline's n counts every observed minute.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping

from logai2.links import level_class
from logai2.timeutil import day_hour

ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL"}
SEVERITY = {"FATAL": 4, "CRITICAL": 4, "ERROR": 3, "WARN": 2, "WARNING": 2, "INFO": 1}


@dataclass
class ClosedMinute:
    minute: int
    day: date
    hour: int
    keys: dict[tuple[str, str], int] = field(default_factory=dict)
    modules: dict[tuple[str, str, str], int] = field(default_factory=dict)
    levels: dict[tuple[str, str], str] = field(default_factory=dict)


class OpenMinutes:
    def __init__(self, start_minute: int):
        self.next_to_close = start_minute
        self.late = 0  # counts that arrived for an already-closed minute (should stay 0)
        self._open: dict[int, ClosedMinute] = {}
        # templates seen in this local hour; they get 0 in later silent minutes of the hour
        self.hour_keys: dict[tuple[date, int], set[tuple[str, str]]] = {}

    def add(self, service: str, template_id: str, level: str, module: str, minute: int, n: int) -> None:
        if minute < self.next_to_close:
            self.late += n
            return
        cm = self._open.get(minute)
        if cm is None:
            day, hour = day_hour(minute)
            cm = self._open[minute] = ClosedMinute(minute, day, hour)
        keys = cm.keys
        tkey = (service, template_id)
        keys[tkey] = keys.get(tkey, 0) + n
        keys[(service, "*total")] = keys.get((service, "*total"), 0) + n
        ckey = (service, "*" + level_class(level).lower())
        keys[ckey] = keys.get(ckey, 0) + n
        mkey = (service, template_id, module)
        cm.modules[mkey] = cm.modules.get(mkey, 0) + n
        old = cm.levels.get(tkey)
        if old is None or SEVERITY.get(level.upper(), 0) > SEVERITY.get(old.upper(), 0):
            cm.levels[tkey] = level

    def close_before(self, minute_excl: int, services: Iterable[str] | Mapping[str, int]) -> list[ClosedMinute]:
        """``services`` may map service -> first minute seen: a service is zero-filled only from then on."""
        first = dict(services) if isinstance(services, Mapping) else dict.fromkeys(services)
        closed = []
        for m in range(self.next_to_close, minute_excl):
            cm = self._open.pop(m, None)
            if cm is None:
                cm = ClosedMinute(m, *day_hour(m))
            dh = (cm.day, cm.hour)
            seen = self.hour_keys.get(dh)
            if seen is None:  # new hour: older hours are never needed again
                self.hour_keys = {dh: set()}
                seen = self.hour_keys[dh]
            keys = {k: 0 for k in seen}
            listed = (s for s, f in first.items() if f is None or f <= m)
            for s in {*listed, *(k[0] for k in cm.keys)}:  # an unlisted service still gets *error
                for k in ("*total", "*error", "*warn", "*info"):
                    keys[(s, k)] = 0
            keys.update(cm.keys)
            seen.update(k for k in cm.keys if not k[1].startswith("*"))
            cm.keys = keys
            closed.append(cm)
        self.next_to_close = max(self.next_to_close, minute_excl)
        return closed

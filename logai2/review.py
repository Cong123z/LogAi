"""Baseline review: the engine suggests the hours a service was not NORMAL, the operator
decides Exclude (real incident) or Keep (normal). Unreviewed hours are kept."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import date, datetime

from logai2.timeutil import TZ


NON_NORMAL = frozenset({"WARMING", "ALERTING", "COOLING"})


def periods(changes: list[dict], end_now: float, states=NON_NORMAL) -> list[tuple[str, float, float]]:
    """alert_changes (ordered by `at`) -> (key, start_s, end_s) per period in `states`;
    a change to a state outside `states` closes it; a period still open ends at `end_now`."""
    opened: dict[str, float] = {}
    out: list[tuple[str, float, float]] = []
    for c in changes:
        at = c["at"].timestamp()
        if c["to_state"] in states:
            opened.setdefault(c["key"], at)   # first change of a key may already be non-NORMAL (retention)
        elif c["key"] in opened:
            out.append((c["key"], opened.pop(c["key"]), at))
    return out + [(k, start, end_now) for k, start in opened.items()]


def _hours(start: float, end: float):
    """Yield (day, hour, hour_start_s) for each local hour a period touches (a zero-length one touches one)."""
    # ponytail: hour steps in epoch seconds match local hours only for a whole-hour offset
    # without DST (Asia/Ho_Chi_Minh, +07); walk local datetimes if TZ ever changes
    h = math.floor(start / 3600) * 3600
    while h < max(end, start + 1):
        t = datetime.fromtimestamp(h, TZ)
        yield t.date(), t.hour, h
        h += 3600


def keys_by_hour(ps: list[tuple[str, float, float]]) -> dict[tuple[date, int], set[str]]:
    """Per local (day, hour): the keys that were not NORMAL at some point in it."""
    cells: dict[tuple[date, int], set[str]] = defaultdict(set)
    for key, start, end in ps:
        for day, hour, _ in _hours(start, end):
            cells[(day, hour)].add(key)
    return cells


def minutes_by_hour(ps: list[tuple[str, float, float]]) -> dict[tuple[date, int], int]:
    """Per local (day, hour): minutes covered by any key's period (a union, not a sum)."""
    merged: list[list[float]] = []
    for _, start, end in sorted(ps, key=lambda p: p[1]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    secs: dict[tuple[date, int], float] = defaultdict(float)
    for start, end in merged:
        for day, hour, h in _hours(start, end):
            secs[(day, hour)] += max(0.0, min(end, h + 3600) - max(start, h))
    return {k: int(v // 60) for k, v in secs.items()}


def suggest(changes: list[dict], decided: set[tuple[str, date, int]], now: datetime | None = None) -> list[dict]:
    """One service's alert_changes (ordered by `at`) -> its non-NORMAL periods as local whole hours,
    split per day and merged when they overlap or touch; hours in `decided` are left out."""
    service = changes[0]["service"] if changes else ""
    cells = keys_by_hour(periods(changes, now.timestamp() if now else time.time()))

    out: list[dict] = []
    for day, hour in sorted(cells):
        if (service, day, hour) in decided:
            continue
        last = out[-1] if out else None
        if last and last["day"] == day and last["hour_to"] == hour:
            last["hour_to"] = hour + 1
            last["keys"] = sorted(set(last["keys"]) | cells[(day, hour)])
        else:
            out.append({"service": service, "day": day, "hour_from": hour, "hour_to": hour + 1,
                        "keys": sorted(cells[(day, hour)])})
    return out

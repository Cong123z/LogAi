"""Baseline review: the engine suggests the hours a service was not NORMAL, the operator
decides Exclude (real incident) or Keep (normal). Unreviewed hours are kept."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import date, datetime

from logai2.timeutil import TZ


def suggest(changes: list[dict], decided: set[tuple[str, date, int]], now: datetime | None = None) -> list[dict]:
    """One service's alert_changes (ordered by `at`) -> its non-NORMAL periods as local whole hours,
    split per day and merged when they overlap or touch; hours in `decided` are left out."""
    end_now = now.timestamp() if now else time.time()   # an open period ends now
    opened: dict[str, float] = {}
    periods: list[tuple[str, float, float]] = []
    for c in changes:
        at = c["at"].timestamp()
        if c["to_state"] == "NORMAL":
            if c["key"] in opened:
                periods.append((c["key"], opened.pop(c["key"]), at))
        else:
            opened.setdefault(c["key"], at)   # first change of a key may already be non-NORMAL (retention)
    periods += [(k, start, end_now) for k, start in opened.items()]

    service = changes[0]["service"] if changes else ""
    cells: dict[tuple[date, int], set[str]] = defaultdict(set)
    for key, start, end in periods:
        # ponytail: hour steps in epoch seconds match local hours only for a whole-hour offset
        # without DST (Asia/Ho_Chi_Minh, +07); walk local datetimes if TZ ever changes
        h = math.floor(start / 3600) * 3600
        while h < max(end, start + 1):
            t = datetime.fromtimestamp(h, TZ)
            cells[(t.date(), t.hour)].add(key)
            h += 3600

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

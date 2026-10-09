"""Recent per-template event counts and each template's normal rate.

All-time template counts say nothing about what a service is doing now, so the
realtime pipeline counts events per (service, template) in two series:
  - 1-minute buckets over the last HORIZON_MINUTES -> count_15m / count_30m;
  - hourly buckets over the last BASELINE_HOURS -> baseline_per_min, the
    template's normal rate (median of closed hours, silent hours as zero),
    and ratio = current rate / normal rate ("how many times normal").
Hours before the engine first observed traffic (`origin_hour`) are unknown, not
zero, so a fresh engine reports no baseline until MIN_BASELINE_HOURS closed.
State is saved to a small JSON file so a restart keeps the baseline.
Written by the poll thread, read and saved by the control thread.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from logai.storage.base import atomic_write_json

HORIZON_MINUTES = 30
WINDOWS_MINUTES = (15, 30)
BASELINE_HOURS = 24
MIN_BASELINE_HOURS = 3
# A normal rate below one event per 10 minutes is treated as this floor, so a
# normally silent template yields a large but finite ratio.
RATE_FLOOR_PER_MIN = 0.1
SCHEMA_VERSION = 1

Key = Tuple[str, str]


def _bump(buckets: Deque[List[int]], index: int, keep: int) -> None:
    if buckets and buckets[-1][0] >= index:
        # ponytail: an out-of-order event counts in the newest bucket; exact
        # only to one bucket, which is all the evidence needs.
        buckets[-1][1] += 1
    else:
        buckets.append([index, 1])
    while buckets and buckets[0][0] <= index - keep:
        buckets.popleft()


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def empty_counts() -> Dict[str, Any]:
    return {
        **{f"count_{w}m": 0 for w in WINDOWS_MINUTES},
        "rate_per_min": 0.0, "baseline_per_min": None, "ratio": None,
    }


class TemplateActivity:
    def __init__(self) -> None:
        self._minutes: Dict[Key, Deque[List[int]]] = {}
        self._hours: Dict[Key, Deque[List[int]]] = {}
        self._origin_hour: Optional[int] = None
        self._lock = threading.Lock()

    def record(self, service: str, template_id: str, timestamp: float) -> None:
        key = (service, template_id)
        minute, hour = int(timestamp // 60), int(timestamp // 3600)
        with self._lock:
            if self._origin_hour is None:
                self._origin_hour = hour
            _bump(self._minutes.setdefault(key, deque()), minute, HORIZON_MINUTES)
            _bump(self._hours.setdefault(key, deque()), hour, BASELINE_HOURS + 1)

    def counts(self, now: float, service: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """template_id -> {count_15m, count_30m, rate_per_min, baseline_per_min,
        ratio} at `now`, for one service or summed over all. Includes every
        template seen in the last BASELINE_HOURS, so a template that went
        silent shows count 0 against its normal rate."""
        current_minute, current_hour = int(now // 60), int(now // 3600)
        with self._lock:
            first_hour = max(
                current_hour - BASELINE_HOURS,
                self._origin_hour if self._origin_hour is not None else current_hour,
            )
            closed_hours = max(0, current_hour - first_hour)
            totals: Dict[str, Dict[str, Any]] = {}
            hourly: Dict[str, Dict[int, int]] = {}
            for (svc, template_id), buckets in self._hours.items():
                if service is not None and svc != service:
                    continue
                per_hour = hourly.setdefault(template_id, {})
                for hour, count in buckets:
                    if first_hour <= hour < current_hour:
                        per_hour[hour] = per_hour.get(hour, 0) + count
                totals.setdefault(template_id, empty_counts())
            for (svc, template_id), buckets in self._minutes.items():
                if service is not None and svc != service:
                    continue
                entry = totals.setdefault(template_id, empty_counts())
                for minute, count in buckets:
                    for window in WINDOWS_MINUTES:
                        if current_minute - window < minute <= current_minute:
                            entry[f"count_{window}m"] += count
        for template_id, entry in totals.items():
            entry["rate_per_min"] = round(entry["count_15m"] / 15.0, 2)
            if closed_hours >= MIN_BASELINE_HOURS:
                per_hour = hourly.get(template_id, {})
                baseline = _median([
                    per_hour.get(hour, 0) / 60.0 for hour in range(first_hour, current_hour)
                ])
                entry["baseline_per_min"] = round(baseline, 3)
                entry["ratio"] = round(entry["rate_per_min"] / max(baseline, RATE_FLOOR_PER_MIN), 1)
        return {
            template_id: entry for template_id, entry in totals.items()
            if entry["count_30m"] or any(hourly.get(template_id, {}).values())
        }

    # -- persistence -----------------------------------------------------------

    def save(self, path: str | Path) -> None:
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "origin_hour": self._origin_hour,
                "minutes": {json.dumps(list(k)): list(v) for k, v in self._minutes.items() if v},
                "hours": {json.dumps(list(k)): list(v) for k, v in self._hours.items() if v},
            }
        atomic_write_json(path, payload)

    @classmethod
    def load(cls, path: str | Path) -> "TemplateActivity":
        """A missing or corrupt file gives an empty counter (baseline rebuilds)."""
        activity = cls()
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            origin = raw.get("origin_hour")
            activity._origin_hour = origin if isinstance(origin, int) else None
            for name, target in (("minutes", activity._minutes), ("hours", activity._hours)):
                for key, buckets in (raw.get(name) or {}).items():
                    service, template_id = json.loads(key)
                    target[(str(service), str(template_id))] = deque(
                        [int(index), int(count)] for index, count in buckets
                    )
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()
        return activity


def recent(activity: Dict[str, Dict[str, Any]], template_id: str) -> Dict[str, Any]:
    """Counts for one template; zeros and unknown baseline when never seen."""
    return activity.get(template_id) or empty_counts()


def is_active(activity: Dict[str, Dict[str, Any]], template_id: str) -> bool:
    """True when the template had events in the last 30 minutes."""
    return bool(recent(activity, template_id)["count_30m"])

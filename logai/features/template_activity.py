"""Recent per-template event counts and each template's normal rate.

All-time template counts say nothing about what a service is doing now, so the
realtime pipeline counts events per (service, template) in two series:
  - 1-minute buckets over the last HORIZON_MINUTES -> count_15m / count_30m;
  - hourly buckets over the last BASELINE_HOURS -> baseline_per_min, the
    template's normal rate (median of closed hours, silent hours as zero),
    and ratio = current rate / normal rate ("how many times normal").
Only hours in which the engine recorded some event (of any service) count:
hours before it started or while it or the ingestion was down are unknown, not
zero, so it reports no baseline until MIN_BASELINE_HOURS such hours closed.
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
        self._lock = threading.Lock()

    def record(self, service: str, template_id: str, timestamp: float) -> None:
        key = (service, template_id)
        minute, hour = int(timestamp // 60), int(timestamp // 3600)
        with self._lock:
            _bump(self._minutes.setdefault(key, deque()), minute, HORIZON_MINUTES)
            _bump(self._hours.setdefault(key, deque()), hour, BASELINE_HOURS + 1)

    def counts(self, now: float, service: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """template_id -> {count_15m, count_30m, rate_per_min, baseline_per_min,
        ratio} at `now`, for one service or summed over all. Includes every
        template seen in the last BASELINE_HOURS, so a template that went
        silent shows count 0 against its normal rate."""
        current_minute, current_hour = int(now // 60), int(now // 3600)
        first_hour = current_hour - BASELINE_HOURS
        with self._lock:
            # Hours the engine was listening; a gap (engine stopped overnight)
            # must not read as a silent service, or every ratio explodes.
            observed = sorted({
                hour for buckets in self._hours.values() for hour, _ in buckets
                if first_hour <= hour < current_hour
            })
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
            if len(observed) >= MIN_BASELINE_HOURS:
                per_hour = hourly.get(template_id, {})
                baseline = _median([per_hour.get(hour, 0) / 60.0 for hour in observed])
                entry["baseline_per_min"] = round(baseline, 3)
                entry["ratio"] = round(entry["rate_per_min"] / max(baseline, RATE_FLOOR_PER_MIN), 1)
        return {
            template_id: entry for template_id, entry in totals.items()
            if entry["count_30m"] or any(hourly.get(template_id, {}).values())
        }

    def series(
        self, now: float, service: str, template_ids: List[str],
        minutes: int = HORIZON_MINUTES,
    ) -> List[int]:
        """Events per minute of these templates of `service` over the last
        `minutes` minutes, oldest first; the last entry is the current minute."""
        current = int(now // 60)
        first = current - minutes + 1
        out = [0] * minutes
        with self._lock:
            for template_id in set(template_ids):
                for minute, count in self._minutes.get((service, template_id), ()):
                    if first <= minute <= current:
                        out[minute - first] += count
        return out

    def rate_1m(self, now: float) -> Dict[str, float]:
        """template_id -> events per minute over the last 60 s, summed over
        services: the current minute plus the unexpired share of the previous
        one (sliding-window estimate), so it moves smoothly second by second."""
        current = int(now // 60)
        previous_weight = 1.0 - (now % 60) / 60.0
        out: Dict[str, float] = {}
        with self._lock:
            for (_svc, template_id), buckets in self._minutes.items():
                total = 0.0
                for minute, count in reversed(buckets):
                    if minute == current:
                        total += count
                    elif minute == current - 1:
                        total += count * previous_weight
                    elif minute < current - 1:
                        break
                if total:
                    out[template_id] = out.get(template_id, 0.0) + total
        return {template_id: round(rate, 2) for template_id, rate in out.items()}

    # -- persistence -----------------------------------------------------------

    def save(self, path: str | Path) -> None:
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
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
            for name, target in (("minutes", activity._minutes), ("hours", activity._hours)):
                for key, buckets in (raw.get(name) or {}).items():
                    service, template_id = json.loads(key)
                    target[(str(service), str(template_id))] = deque(
                        [int(index), int(count)] for index, count in buckets
                    )
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()
        return activity


RATE_KEYS = ("rate_1m", "rate_per_min", "baseline_per_min", "ratio")


def rate_fields(entry: Optional[Dict[str, Any]], rate_1m: float = 0.0) -> Dict[str, Any]:
    """What the web shows of a template's activity: the last minute's rate,
    the 15-minute rate, the normal rate and how many times normal (15-minute
    based). A template silent for 24 h has rate 0."""
    entry = entry or empty_counts()
    return {"rate_1m": entry.get("rate_1m", rate_1m),
            **{k: entry[k] for k in ("rate_per_min", "baseline_per_min", "ratio")}}


def group_rate(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum of member rates against the summed normal rate. A member silent for
    24 h has no baseline and counts as 0; no member with one means unknown."""
    rate = round(sum(e["rate_per_min"] or 0.0 for e in entries), 2)
    rate_1m = round(sum(e.get("rate_1m") or 0.0 for e in entries), 2)
    baselines = [e["baseline_per_min"] for e in entries if e["baseline_per_min"] is not None]
    if not baselines:
        return {"rate_1m": rate_1m, "rate_per_min": rate, "baseline_per_min": None, "ratio": None}
    baseline = sum(baselines)
    return {
        "rate_1m": rate_1m, "rate_per_min": rate, "baseline_per_min": round(baseline, 3),
        "ratio": round(rate / max(baseline, RATE_FLOOR_PER_MIN), 1),
    }


def recent(activity: Dict[str, Dict[str, Any]], template_id: str) -> Dict[str, Any]:
    """Counts for one template; zeros and unknown baseline when never seen."""
    return activity.get(template_id) or empty_counts()


def is_active(activity: Dict[str, Dict[str, Any]], template_id: str) -> bool:
    """True when the template had events in the last 30 minutes."""
    return bool(recent(activity, template_id)["count_30m"])

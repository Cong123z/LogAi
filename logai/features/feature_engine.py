"""Feature generation (plan section 3.8 / 4.6).

Maintains, per (service, group_id) window, bounded deques of recent event
timestamps (current 10s / 1m / 5m rates) plus a **time-based baseline**: the
rates of closed 10-second and 1-minute buckets over the last
``baseline_seconds``. Keying the window by BOTH service and group keeps each
service's rate baseline isolated - a group shared by several services no longer
averages them into one blended baseline. The same `FeatureEngine` is used by:
  - training: fed historical events in timestamp order to build the feature
    history used to fit the global Isolation Forest.
  - realtime: fed one event at a time as it arrives.

This keeps train/serve feature logic identical, which matters for anomaly
detection consistency.

Why buckets instead of a per-event history: a history that grows by one point
per event spans only the last few seconds of a busy window, so any sustained
change becomes the new "normal" almost immediately. Buckets advance with time,
not with traffic, and buckets in which a window received nothing count as zero
(from the engine's ``origin``, the moment it started listening), so a group
that is normally silent keeps a near-zero baseline.
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from logai.config import FeatureConfig
from logai.models import FeatureVector

BUCKET_10S = 10.0
BUCKET_1M = 60.0
# A z-score needs a few closed buckets before mean/std mean anything.
MIN_BASELINE_10S_BUCKETS = 3   # 30s; a median of 3 already ignores one partial bucket
MIN_BASELINE_1M_BUCKETS = 3    # three minutes of 1-minute buckets
# spike_ratio_10s compares the peak of the most recent buckets to the baseline.
SPIKE_RECENT_BUCKETS = 6


class _RollingStats:
    """Bounded series of closed-bucket rates.

    Statistics only change when a bucket closes, so they are computed lazily
    once per change instead of on every event. ``median`` / ``robust_std``
    (scaled MAD) are the deviation baseline: an incident lasting less than
    half of ``baseline_seconds`` cannot drag them towards itself, unlike a
    mean/std baseline. ``mean`` / ``std`` remain for burstiness (CV^2).
    """

    def __init__(self, maxlen: float):
        self.maxlen = max(1, int(maxlen))
        self.values: Deque[float] = deque(maxlen=self.maxlen)
        self._stats: Optional[Tuple[float, float, float, float]] = None

    def append(self, value: float) -> None:
        self.values.append(value)
        self._stats = None

    def extend_zeros(self, count: int) -> None:
        if count > 0:
            self.values.extend([0.0] * min(count, self.maxlen))
            self._stats = None

    def __len__(self) -> int:
        return len(self.values)

    def _compute(self) -> Tuple[float, float, float, float]:
        if self._stats is None:
            n = len(self.values)
            if n == 0:
                self._stats = (0.0, 0.0, 0.0, 0.0)
            else:
                mean = sum(self.values) / n
                std = (
                    math.sqrt(max(0.0, sum(v * v for v in self.values) / n - mean * mean))
                    if n > 1 else 0.0
                )
                ordered = sorted(self.values)
                median = _median(ordered)
                mad = _median(sorted(abs(v - median) for v in ordered))
                self._stats = (mean, std, median, 1.4826 * mad)
        return self._stats

    def mean(self) -> float:
        return self._compute()[0]

    def std(self) -> float:
        return self._compute()[1]

    def median(self) -> float:
        return self._compute()[2]

    def robust_std(self) -> float:
        return self._compute()[3]

    def recent_max(self, count: int) -> float:
        if not self.values:
            return 0.0
        n = len(self.values)
        return max(self.values[i] for i in range(max(0, n - count), n))


def _median(ordered: List[float]) -> float:
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass
class _GroupWindow:
    ts_10s: Deque[float] = field(default_factory=deque)
    ts_1m: Deque[float] = field(default_factory=deque)
    ts_5m: Deque[float] = field(default_factory=deque)
    # Closed-bucket baselines (rates in events/s) and the open bucket of each.
    hist_10s: _RollingStats = field(default_factory=lambda: _RollingStats(180))
    hist_1m: _RollingStats = field(default_factory=lambda: _RollingStats(30))
    bucket_10s: Optional[int] = None
    bucket_10s_count: int = 0
    bucket_1m: Optional[int] = None
    bucket_1m_count: int = 0

    @property
    def timestamps(self) -> Deque[float]:
        """Backward compatibility alias for tests and inspection."""
        return self.ts_5m

    @timestamps.setter
    def timestamps(self, val: Deque[float]) -> None:
        self.ts_5m = val


class FeatureEngine:
    def __init__(self, config: FeatureConfig, origin: Optional[float] = None):
        """``origin`` is when the engine started observing traffic; buckets
        between it and a window's first event count as silent (zero). Training
        passes the earliest training timestamp; realtime leaves it unset and
        the first event received fixes it."""
        self.config = config
        self._origin = origin
        # Window key is (service, group_id). The body is key-agnostic (it only
        # stores/reads by the key), so re-keying per service is a type change.
        self._windows: Dict[Tuple[str, str], _GroupWindow] = defaultdict(self._create_window)
        # The poll thread mutates windows; the control thread reads them via
        # describe() for LLM evidence. Uncontended, so the hot path barely pays.
        self._lock = threading.Lock()

    @property
    def origin(self) -> Optional[float]:
        return self._origin

    def _create_window(self) -> _GroupWindow:
        baseline = float(self.config.baseline_seconds)
        gw = _GroupWindow(
            hist_10s=_RollingStats(baseline / BUCKET_10S),
            hist_1m=_RollingStats(baseline / BUCKET_1M),
        )
        if self._origin is not None:
            gw.bucket_10s = int(self._origin // BUCKET_10S)
            gw.bucket_1m = int(self._origin // BUCKET_1M)
        return gw

    def _prune(self, gw: _GroupWindow, now: float) -> None:
        w10, w1m, w5m = self.config.windows_seconds
        while gw.ts_10s and now - gw.ts_10s[0] > w10:
            gw.ts_10s.popleft()
        while gw.ts_1m and now - gw.ts_1m[0] > w1m:
            gw.ts_1m.popleft()
        while gw.ts_5m and now - gw.ts_5m[0] > w5m:
            gw.ts_5m.popleft()

    @staticmethod
    def _roll(
        index: Optional[int], count: int, hist: _RollingStats, width: float, now: float
    ) -> Tuple[int, int]:
        """Close the open bucket (and any empty ones) up to ``now``.
        Out-of-order events stay in the open bucket."""
        now_index = int(now // width)
        if index is None:
            return now_index, 0
        if now_index <= index:
            return index, count
        hist.append(count / width)
        hist.extend_zeros(now_index - index - 1)
        return now_index, 0

    def _advance(self, gw: _GroupWindow, now: float) -> None:
        gw.bucket_10s, gw.bucket_10s_count = self._roll(
            gw.bucket_10s, gw.bucket_10s_count, gw.hist_10s, BUCKET_10S, now
        )
        gw.bucket_1m, gw.bucket_1m_count = self._roll(
            gw.bucket_1m, gw.bucket_1m_count, gw.hist_1m, BUCKET_1M, now
        )

    @staticmethod
    def _append_sorted(values: Deque[float], timestamp: float) -> Deque[float]:
        values.append(timestamp)
        if len(values) > 1 and values[-1] < values[-2]:
            return deque(sorted(values))
        return values

    def update(self, group_id: Tuple[str, str], timestamp: float) -> FeatureVector:
        """Record one event for window `group_id` = (service, group) at
        `timestamp` and return the freshly computed feature vector for that
        (service, group) cell. The param keeps the name `group_id` because it is
        echoed straight into FeatureVector.group_id; it now carries a tuple."""
        with self._lock:
            if self._origin is None:
                self._origin = timestamp
            gw = self._windows[group_id]
            self._advance(gw, timestamp)
            gw.bucket_10s_count += 1
            gw.bucket_1m_count += 1

            gw.ts_10s = self._append_sorted(gw.ts_10s, timestamp)
            gw.ts_1m = self._append_sorted(gw.ts_1m, timestamp)
            gw.ts_5m = self._append_sorted(gw.ts_5m, timestamp)

            self._prune(gw, timestamp)
            return self._compute(group_id, gw, timestamp)

    def snapshot(self, group_id: Tuple[str, str], timestamp: float) -> FeatureVector:
        """Compute the current feature vector without adding a new event -
        useful for periodic re-evaluation of idle (service, group) cells."""
        with self._lock:
            gw = self._windows[group_id]
            if self._origin is not None and timestamp >= self._origin:
                self._advance(gw, timestamp)
            self._prune(gw, timestamp)
            return self._compute(group_id, gw, timestamp)

    def describe(self, group_id: Tuple[str, str], now: float) -> Optional[Dict[str, object]]:
        """Read-only rate-vs-baseline summary of one window for LLM evidence
        (rates in events/min), or None when the window has no events. Safe to
        call from another thread; never creates or advances a window."""
        with self._lock:
            gw = self._windows.get(group_id)
            if gw is None or not gw.ts_5m:
                return None
            w10, w1m, w5m = self.config.windows_seconds
            count_10s = sum(1 for ts in gw.ts_10s if now - ts <= w10)
            count_1m = sum(1 for ts in gw.ts_1m if now - ts <= w1m)
            count_5m = sum(1 for ts in gw.ts_5m if now - ts <= w5m)
            baseline_minutes = len(gw.hist_1m)
            median_1m = gw.hist_1m.median() * 60.0
            spread_1m = gw.hist_1m.robust_std() * 60.0
            last_event = gw.ts_5m[-1]
        rate_1m = count_1m * 60.0 / w1m
        ratio = rate_1m / median_1m if median_1m > 0 else None
        if baseline_minutes < MIN_BASELINE_1M_BUCKETS:
            summary = (f"1m rate {rate_1m:.1f}/min; baseline not established yet "
                       f"({baseline_minutes} min of history)")
        elif ratio is None:
            summary = (f"1m rate {rate_1m:.1f}/min; this window is normally silent "
                       f"(baseline median 0/min over the last {baseline_minutes} min)")
        else:
            summary = (f"1m rate {rate_1m:.1f}/min is {ratio:.1f}x the baseline median "
                       f"{median_1m:.1f}/min (spread {spread_1m:.1f}/min, "
                       f"last {baseline_minutes} min)")
        return {
            "summary": summary,
            "rate_10s_per_min": round(count_10s * 60.0 / w10, 2),
            "rate_1m_per_min": round(rate_1m, 2),
            "rate_5m_per_min": round(count_5m * 60.0 / w5m, 2),
            "count_1m": count_1m,
            "baseline_1m_median_per_min": round(median_1m, 2),
            "baseline_1m_spread_per_min": round(spread_1m, 2),
            "baseline_minutes": baseline_minutes,
            "ratio_to_baseline": round(ratio, 2) if ratio is not None else None,
            "seconds_since_last_event": round(max(0.0, now - last_event), 1),
        }

    def live_window_keys(self) -> Iterable[Tuple[str, str]]:
        """(service, group) keys that currently have a window (received at least
        one event this process lifetime). Snapshot-safe list copy so the idle
        tick can iterate while events append new keys. Memory-only: empty after a
        restart until traffic repopulates - the idle tick unions this with the
        persisted alert set for that reason."""
        with self._lock:
            return list(self._windows.keys())

    def last_event_ts(self, group_id: Tuple[str, str]) -> Optional[float]:
        """Timestamp of the most recent event recorded for a (service, group)
        cell, or None if no window exists yet. Uses .get() so querying never
        materializes a window via the defaultdict. O(1)."""
        gw = self._windows.get(group_id)
        if gw is None or not gw.ts_5m:
            return None
        return gw.ts_5m[-1]

    def drop_group(self, group_id: str) -> int:
        """Remove every service window for a deleted semantic group."""
        with self._lock:
            keys = [key for key in self._windows.keys() if key[1] == group_id]
            for key in keys:
                self._windows.pop(key, None)
        return len(keys)

    def _compute(
        self, group_id: Tuple[str, str], gw: _GroupWindow, now: float
    ) -> FeatureVector:
        w10, w1m, w5m = self.config.windows_seconds

        count_10s = len(gw.ts_10s)
        count_1m = len(gw.ts_1m)
        count_5m = len(gw.ts_5m)

        # Cold-start: return neutral baseline vector
        if count_5m <= 1:
            return FeatureVector(
                group_id=group_id,
                timestamp=now,
                z_score_10s=0.0,
                z_score_1m=0.0,
                short_growth_rate=1.0,
                growth_rate=1.0,
                burstiness_10s=0.0,
                rate_delta_norm=0.0,
                slope_norm=0.0,
                spike_ratio_10s=1.0,
                count_1m=count_1m,
            )

        rate_10s = count_10s / w10
        rate_1m = count_1m / w1m
        rate_5m = count_5m / w5m

        eps = 1e-9
        rate_floor = max(float(self.config.rate_floor), eps)

        # Baseline from closed buckets only, so the current burst never
        # contaminates the baseline it is compared against. Deviation uses the
        # robust median / scaled MAD; burstiness uses mean / std.
        mu_10 = gw.hist_10s.mean()
        sigma_10 = gw.hist_10s.std()
        med_10 = gw.hist_10s.median()
        med_1m = gw.hist_1m.median()
        # A perfectly flat (often all-zero) baseline has zero spread; floor it
        # so a burst on a normally silent group still yields a large, finite z.
        spread_10 = max(gw.hist_10s.robust_std(), rate_floor)
        spread_1m = max(gw.hist_1m.robust_std(), rate_floor)
        has_10s_baseline = len(gw.hist_10s) >= MIN_BASELINE_10S_BUCKETS
        has_1m_baseline = len(gw.hist_1m) >= MIN_BASELINE_1M_BUCKETS

        # 1. z_score_10s: current 10s rate against the 10s-bucket baseline
        z_score_10s = (rate_10s - med_10) / spread_10 if has_10s_baseline else 0.0
        z_score_10s = max(-10.0, min(10.0, z_score_10s))

        # 2. z_score_1m: current 1m rate against the 1m-bucket baseline
        z_score_1m = (rate_1m - med_1m) / spread_1m if has_1m_baseline else 0.0
        z_score_1m = max(-10.0, min(10.0, z_score_1m))

        # 3. short_growth_rate: instant burst ratio between 10s and 1m (theoretical max ~6.0)
        short_growth_rate = rate_10s / max(rate_1m, rate_floor)
        short_growth_rate = max(0.0, min(6.0, short_growth_rate))

        # 4. growth_rate: medium-term ratio between 1m and 5m (theoretical max ~5.0)
        if rate_5m > eps:
            growth_rate = rate_1m / (rate_5m + eps)
        else:
            growth_rate = 1.0 if rate_1m <= eps else 5.0
        growth_rate = max(0.0, min(5.0, growth_rate))

        # 5. burstiness_10s: relative variance (CV^2) of the 10s-bucket baseline
        if len(gw.hist_10s) > 1:
            burstiness_10s = (sigma_10 ** 2) / max(mu_10, rate_floor) ** 2
        else:
            burstiness_10s = 0.0
        burstiness_10s = max(0.0, min(20.0, burstiness_10s))

        # 6. rate_delta_norm: 1m vs 5m rate, normalised by the 1m-bucket spread
        rate_delta_norm = (rate_1m - rate_5m) / spread_1m if has_1m_baseline else 0.0
        rate_delta_norm = max(-10.0, min(10.0, rate_delta_norm))

        # 7. slope_norm: linear trend of the 1m-bucket baseline normalised by its median
        slope = self._slope(list(gw.hist_1m.values)) if has_1m_baseline else 0.0
        slope_norm = slope / max(med_1m, rate_floor)
        slope_norm = max(-10.0, min(10.0, slope_norm))

        # 8. spike_ratio_10s: peak of the current and most recent 10s buckets vs baseline
        peak_10s = max(rate_10s, gw.hist_10s.recent_max(SPIKE_RECENT_BUCKETS))
        spike_ratio_10s = peak_10s / max(med_10, rate_floor)
        spike_ratio_10s = max(0.0, min(20.0, spike_ratio_10s))

        return FeatureVector(
            group_id=group_id,
            timestamp=now,
            z_score_10s=z_score_10s,
            z_score_1m=z_score_1m,
            short_growth_rate=short_growth_rate,
            growth_rate=growth_rate,
            burstiness_10s=burstiness_10s,
            rate_delta_norm=rate_delta_norm,
            slope_norm=slope_norm,
            spike_ratio_10s=spike_ratio_10s,
            count_1m=count_1m,
        )

    @staticmethod
    def _slope(values: List[float]) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        xs = list(range(n))
        x_mean = sum(xs) / n
        y_mean = sum(values) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
        den = sum((x - x_mean) ** 2 for x in xs)
        return num / den if den > 1e-9 else 0.0

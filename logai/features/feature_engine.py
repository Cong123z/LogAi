"""Feature generation (plan section 3.8 / 4.6).

Maintains, per (service, group_id) window, a bounded deque of recent event
timestamps plus a rolling history of computed 1-minute rates (used for
mean/std/z-score/slope). Keying the window by BOTH service and group keeps each
service's rate baseline isolated - a group shared by several services no longer
averages them into one blended baseline. The same `FeatureEngine` is used by:
  - training: fed historical events in timestamp order to build the feature
    history used to fit the global Isolation Forest.
  - realtime: fed one event at a time as it arrives.

This keeps train/serve feature logic identical, which matters for anomaly
detection consistency.
"""
from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from logai.config import FeatureConfig
from logai.models import FeatureVector


@dataclass
class _GroupWindow:
    ts_10s: Deque[float] = field(default_factory=deque)
    ts_1m: Deque[float] = field(default_factory=deque)
    ts_5m: Deque[float] = field(default_factory=deque)
    rate_10s_history: Deque[float] = field(default_factory=deque)
    rate_1m_history: Deque[float] = field(default_factory=deque)

    @property
    def timestamps(self) -> Deque[float]:
        """Backward compatibility alias for tests and inspection."""
        return self.ts_5m

    @timestamps.setter
    def timestamps(self, val: Deque[float]) -> None:
        self.ts_5m = val


class FeatureEngine:
    def __init__(self, config: FeatureConfig):
        self.config = config
        # Window key is (service, group_id). The body is key-agnostic (it only
        # stores/reads by the key), so re-keying per service is a type change.
        self._windows: Dict[Tuple[str, str], _GroupWindow] = defaultdict(self._create_window)

    def _create_window(self) -> _GroupWindow:
        maxlen = self.config.rolling_window_points
        return _GroupWindow(
            ts_10s=deque(),
            ts_1m=deque(),
            ts_5m=deque(),
            rate_10s_history=deque(maxlen=maxlen),
            rate_1m_history=deque(maxlen=maxlen),
        )

    def _prune(self, gw: _GroupWindow, now: float) -> None:
        w10, w1m, w5m = self.config.windows_seconds
        while gw.ts_10s and now - gw.ts_10s[0] > w10:
            gw.ts_10s.popleft()
        while gw.ts_1m and now - gw.ts_1m[0] > w1m:
            gw.ts_1m.popleft()
        while gw.ts_5m and now - gw.ts_5m[0] > w5m:
            gw.ts_5m.popleft()

    def update(self, group_id: Tuple[str, str], timestamp: float) -> FeatureVector:
        """Record one event for window `group_id` = (service, group) at
        `timestamp` and return the freshly computed feature vector for that
        (service, group) cell. The param keeps the name `group_id` because it is
        echoed straight into FeatureVector.group_id; it now carries a tuple."""
        gw = self._windows[group_id]

        gw.ts_10s.append(timestamp)
        if len(gw.ts_10s) > 1 and gw.ts_10s[-1] < gw.ts_10s[-2]:
            gw.ts_10s = deque(sorted(gw.ts_10s))

        gw.ts_1m.append(timestamp)
        if len(gw.ts_1m) > 1 and gw.ts_1m[-1] < gw.ts_1m[-2]:
            gw.ts_1m = deque(sorted(gw.ts_1m))

        gw.ts_5m.append(timestamp)
        if len(gw.ts_5m) > 1 and gw.ts_5m[-1] < gw.ts_5m[-2]:
            gw.ts_5m = deque(sorted(gw.ts_5m))

        self._prune(gw, timestamp)
        return self._compute(group_id, gw, timestamp)

    def snapshot(self, group_id: Tuple[str, str], timestamp: float) -> FeatureVector:
        """Compute the current feature vector without adding a new event -
        useful for periodic re-evaluation of idle (service, group) cells."""
        gw = self._windows[group_id]
        self._prune(gw, timestamp)
        return self._compute(group_id, gw, timestamp)

    def live_window_keys(self) -> Iterable[Tuple[str, str]]:
        """(service, group) keys that currently have a window (received at least
        one event this process lifetime). Snapshot-safe list copy so the idle
        tick can iterate while events append new keys. Memory-only: empty after a
        restart until traffic repopulates - the idle tick unions this with the
        persisted alert set for that reason."""
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

        rate_10s = count_10s / w10
        rate_1m = count_1m / w1m
        rate_5m = count_5m / w5m

        # 1. Baseline statistics computed strictly from prior history (prior samples)
        # to prevent anomaly from self-inflating the baseline (Baseline Contamination).
        hist_10s = list(gw.rate_10s_history)
        hist_1m = list(gw.rate_1m_history)

        mu_10 = statistics.fmean(hist_10s) if hist_10s else 0.0
        sigma_10 = statistics.pstdev(hist_10s) if len(hist_10s) > 1 else 0.0

        mu_1m = statistics.fmean(hist_1m) if hist_1m else 0.0
        sigma_1m = statistics.pstdev(hist_1m) if len(hist_1m) > 1 else 0.0

        # Append current rates to history after capturing prior baseline
        gw.rate_10s_history.append(rate_10s)
        gw.rate_1m_history.append(rate_1m)

        # Cold-start: return neutral baseline vector
        if len(gw.timestamps) <= 1:
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

        eps = 1e-9
        rate_floor = max(float(self.config.rate_floor), eps)

        # 1. z_score_10s: short-term burst compared to 10s baseline
        z_score_10s = (rate_10s - mu_10) / (sigma_10 + eps) if sigma_10 > eps else 0.0
        z_score_10s = max(-10.0, min(10.0, z_score_10s))

        # 2. z_score_1m: medium-term deviation compared to 1m baseline
        z_score_1m = (rate_1m - mu_1m) / (sigma_1m + eps) if sigma_1m > eps else 0.0
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

        # 5. burstiness_10s: relative variance (CV^2 = sigma^2 / mu^2) on 10s rate history
        if len(hist_10s) > 1:
            burstiness_10s = (sigma_10 ** 2) / max(mu_10, rate_floor) ** 2
        else:
            burstiness_10s = 0.0
        burstiness_10s = max(0.0, min(20.0, burstiness_10s))

        # 6. rate_delta_norm: rate delta normalized by 1m standard deviation
        rate_delta_norm = (rate_1m - rate_5m) / (sigma_1m + eps) if sigma_1m > eps else 0.0
        rate_delta_norm = max(-10.0, min(10.0, rate_delta_norm))

        # 7. slope_norm: linear trend of 1m rate history normalized by mean
        slope = self._slope(list(gw.rate_1m_history))
        slope_norm = slope / (mu_1m + eps) if mu_1m > eps else 0.0
        slope_norm = max(-10.0, min(10.0, slope_norm))

        # 8. spike_ratio_10s: peak recent 10s rate relative to baseline mu_10
        max_recent_r10 = max(gw.rate_10s_history) if gw.rate_10s_history else rate_10s
        spike_ratio_10s = max_recent_r10 / max(mu_10, rate_floor)
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

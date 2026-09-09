"""Feature generation (plan section 3.8 / 4.6).

Maintains, per group_id, a bounded deque of recent event timestamps plus a
rolling history of computed 1-minute rates (used for mean/std/z-score/slope).
The same `FeatureEngine` is used by:
  - training: fed historical events in timestamp order to build the feature
    history used to fit each group's Isolation Forest.
  - realtime: fed one event at a time as it arrives.

This keeps train/serve feature logic identical, which matters for anomaly
detection consistency.
"""
from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

from logai.config import FeatureConfig
from logai.models import FeatureVector


@dataclass
class _GroupWindow:
    timestamps: Deque[float] = field(default_factory=deque)
    rate_1m_history: Deque[float] = field(default_factory=lambda: deque(maxlen=64))


class FeatureEngine:
    def __init__(self, config: FeatureConfig):
        self.config = config
        self._windows: Dict[str, _GroupWindow] = defaultdict(_GroupWindow)

    def _prune(self, gw: _GroupWindow, now: float) -> None:
        retention = self.config.history_retention_seconds
        while gw.timestamps and now - gw.timestamps[0] > retention:
            gw.timestamps.popleft()

    def update(self, group_id: str, timestamp: float) -> FeatureVector:
        """Record one event for `group_id` at `timestamp` and return the
        freshly computed feature vector for that group."""
        gw = self._windows[group_id]
        gw.timestamps.append(timestamp)
        # keep chronological order even if events arrive slightly out of
        # order (small ES/network jitter) - cheap for the volumes involved.
        if len(gw.timestamps) > 1 and gw.timestamps[-1] < gw.timestamps[-2]:
            gw.timestamps = deque(sorted(gw.timestamps))
        self._prune(gw, timestamp)
        return self._compute(group_id, gw, timestamp)

    def snapshot(self, group_id: str, timestamp: float) -> FeatureVector:
        """Compute the current feature vector without adding a new event -
        useful for periodic re-evaluation of idle groups."""
        gw = self._windows[group_id]
        self._prune(gw, timestamp)
        return self._compute(group_id, gw, timestamp)

    def _compute(
        self, group_id: str, gw: _GroupWindow, now: float
    ) -> FeatureVector:
        w10, w1m, w5m = self.config.windows_seconds

        def count_since(seconds: float) -> int:
            cutoff = now - seconds
            return sum(1 for t in gw.timestamps if t >= cutoff)

        count_10s = count_since(w10)
        count_1m = count_since(w1m)
        count_5m = count_since(w5m)

        rate_1m = count_1m / w1m
        rate_5m = count_5m / w5m

        gw.rate_1m_history.append(rate_1m)
        hist = list(gw.rate_1m_history)

        rolling_mean = statistics.fmean(hist) if hist else 0.0
        rolling_std = statistics.pstdev(hist) if len(hist) > 1 else 0.0

        eps = 1e-9

        # 1. z_score: standard score of rate_1m against rolling history
        z_score = (rate_1m - rolling_mean) / rolling_std if rolling_std > eps else 0.0

        # 2. growth_rate: ratio of 1m rate to 5m rate; neutral 1.0 when cold-starting (single event)
        if len(gw.timestamps) <= 1:
            growth_rate = 1.0
        else:
            growth_rate = (rate_1m / rate_5m) if rate_5m > eps else (1.0 if rate_1m > 0 else 0.0)

        # 3. burstiness: Fano factor (variance / mean) over rate history
        if len(hist) > 1 and rolling_mean > eps:
            burstiness = (rolling_std ** 2) / rolling_mean
        else:
            burstiness = 0.0

        # 4. rate_delta_norm: difference between 1m and 5m rates normalized by rolling std
        rate_delta_norm = (rate_1m - rate_5m) / rolling_std if rolling_std > eps else 0.0

        # 5. slope_norm: linear trend of rate history normalized by rolling mean
        slope = self._slope(hist)
        slope_norm = slope / rolling_mean if rolling_mean > eps else 0.0

        # 6. spike_ratio: max recent rate relative to rolling mean; neutral 1.0 when idle/new
        max_recent_rate = max(hist) if hist else 0.0
        spike_ratio = max_recent_rate / rolling_mean if rolling_mean > eps else 1.0

        return FeatureVector(
            group_id=group_id,
            timestamp=now,
            z_score=z_score,
            growth_rate=growth_rate,
            burstiness=burstiness,
            rate_delta_norm=rate_delta_norm,
            slope_norm=slope_norm,
            spike_ratio=spike_ratio,
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

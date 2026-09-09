"""Alert state machine with hysteresis (plan section 4.8):
NORMAL -> WARMING -> ALERTING -> COOLING -> NORMAL

Prevents metric/alert flapping on a single noisy anomaly score by requiring
consecutive high/low scores before switching state.
"""
from __future__ import annotations

from typing import Dict

from logai.config import AlertConfig
from logai.models import AlertStateEnum, AnomalyResult, AnomalyState
from logai.storage.base import JSONStore


class AlertStateMachine:
    def __init__(self, config: AlertConfig, state_store: JSONStore):
        self.config = config
        self._store = state_store

    def _load(self, group_id: str) -> AnomalyState:
        raw = self._store.get(group_id)
        if raw:
            return AnomalyState(**raw)
        return AnomalyState(group_id=group_id, timestamp=0.0)

    def _save(self, state: AnomalyState) -> None:
        self._store.set(group_id_key(state.group_id), state.__dict__)

    def transition(self, result: AnomalyResult) -> AnomalyState:
        """Hysteresis transition.

        `consecutive_anomaly_count` is contextual: while climbing toward an
        alert (NORMAL/WARMING) it counts consecutive *high* scores; while
        recovering (ALERTING/COOLING) it counts consecutive *low* scores.
        Each state resets/reinterprets the counter on entry so the two
        meanings never bleed into each other.
        """
        prev = self._load(result.group_id)
        cfg = self.config
        high = result.anomaly_score >= cfg.score_high
        low = result.anomaly_score <= cfg.score_low

        state = prev.alert_state
        count = prev.consecutive_anomaly_count

        if state == AlertStateEnum.NORMAL.value:
            count = count + 1 if high else 0
            if count >= cfg.warm_consecutive:
                state = AlertStateEnum.WARMING.value
        elif state == AlertStateEnum.WARMING.value:
            if high:
                count += 1
                if count >= cfg.alert_consecutive:
                    state = AlertStateEnum.ALERTING.value
                    count = 0
            else:
                state = AlertStateEnum.NORMAL.value
                count = 0
        elif state == AlertStateEnum.ALERTING.value:
            if low:
                state = AlertStateEnum.COOLING.value
                count = 1
            else:
                count = 0  # stays ALERTING; only a low score starts cooling
        elif state == AlertStateEnum.COOLING.value:
            if high:
                state = AlertStateEnum.ALERTING.value
                count = 0
            elif low:
                count += 1
                if count >= cfg.cool_consecutive:
                    state = AlertStateEnum.NORMAL.value
                    count = 0
            else:
                count = 0  # mid-range score resets the cooling countdown

        consecutive = count
        new_state = AnomalyState(
            group_id=result.group_id,
            timestamp=result.timestamp,
            anomaly_score=result.anomaly_score,
            anomaly=result.anomaly,
            consecutive_anomaly_count=consecutive,
            alert_state=state,
            model_version=result.model_version,
        )
        self._save(new_state)
        return new_state


def group_id_key(group_id: str) -> str:
    return group_id

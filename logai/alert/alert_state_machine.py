"""Alert state machine with hysteresis (plan section 4.8):
NORMAL -> WARMING -> ALERTING -> COOLING -> NORMAL

Prevents metric/alert flapping on a single noisy anomaly score by requiring
consecutive high/low scores before switching state.
"""
from __future__ import annotations

from typing import Dict, List, Set

from logai.config import AlertConfig
from logai.models import AlertStateEnum, AnomalyResult, AnomalyState
from logai.storage.base import JSONStore


class AlertStateMachine:
    def __init__(self, config: AlertConfig, state_store: JSONStore):
        self.config = config
        self._store = state_store
        # In-memory set of group_ids currently in a non-NORMAL alert state,
        # kept in sync by transition(). Lets the realtime idle-tick enumerate
        # groups needing re-evaluation in O(non-normal) instead of scanning /
        # copying the whole store on every tick.
        normal = AlertStateEnum.NORMAL.value
        self._non_normal: Set[str] = {
            gid
            for gid, raw in state_store.all().items()
            if isinstance(raw, dict) and raw.get("alert_state", normal) != normal
        }

    def _load(self, group_id: str) -> AnomalyState:
        raw = self._store.get(group_id)
        if raw:
            return AnomalyState(**raw)
        return AnomalyState(group_id=group_id, timestamp=0.0)

    def transition(self, result: AnomalyResult) -> AnomalyState:
        """Hysteresis transition.

        `consecutive_anomaly_count` is contextual: while climbing toward an
        alert (NORMAL/WARMING) it counts consecutive *high* scores; while
        recovering (ALERTING/COOLING) it counts consecutive *low* scores.
        Each state resets/reinterprets the counter on entry so the two
        meanings never bleed into each other.
        """
        return self.transition_batch([result])[0]

    def transition_batch(
        self, results: List[AnomalyResult]
    ) -> List[AnomalyState]:
        """Apply ordered transitions and persist final group states once.

        Every result is evaluated in input order, including repeated results
        for the same group, so hysteresis is identical to calling transition()
        one event at a time. Intermediate states are returned for metrics, but
        only the final state of each group is written to the JSON store.
        """
        if not results:
            return []

        final_by_group: Dict[str, AnomalyState] = {}
        states: List[AnomalyState] = []
        for result in results:
            prev = final_by_group.get(result.group_id)
            if prev is None:
                prev = self._load(result.group_id)
            state = self._transition(prev, result)
            final_by_group[result.group_id] = state
            states.append(state)

        self._store.bulk_set({
            group_id_key(group_id): state.__dict__
            for group_id, state in final_by_group.items()
        })
        for group_id, state in final_by_group.items():
            if state.alert_state == AlertStateEnum.NORMAL.value:
                self._non_normal.discard(group_id)
            else:
                self._non_normal.add(group_id)
        return states

    def _transition(
        self, prev: AnomalyState, result: AnomalyResult
    ) -> AnomalyState:
        """Compute one transition without performing persistence I/O."""
        cfg = self.config
        has_volume = (
            result.count_1m is None
            or result.count_1m >= cfg.min_events_1m
        )
        high = result.anomaly_score >= cfg.score_high and has_volume
        # Sparse observations are recovery evidence even when the raw model
        # score is high; retain that score for metrics while gating alert state.
        low = result.anomaly_score <= cfg.score_low or not has_volume

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
        return AnomalyState(
            group_id=result.group_id,
            timestamp=result.timestamp,
            anomaly_score=result.anomaly_score,
            anomaly=result.anomaly,
            consecutive_anomaly_count=consecutive,
            alert_state=state,
            model_version=result.model_version,
        )

    def groups_not_normal(self) -> List[str]:
        """group_ids of every group currently in a non-NORMAL alert state
        (WARMING / ALERTING / COOLING).

        O(number of anomalous groups) - reads an in-memory set, never scans or
        copies the whole state store, so it stays cheap even with a very large
        total number of groups.
        """
        return list(self._non_normal)


def group_id_key(group_id: str) -> str:
    return group_id

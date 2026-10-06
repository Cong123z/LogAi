"""Alert state machine with hysteresis (plan section 4.8):
NORMAL -> WARMING -> ALERTING -> COOLING -> NORMAL

Prevents metric/alert flapping on a single noisy anomaly score by requiring
consecutive high/low scores before switching state.

Window/alert identity is now a (service, group_id) tuple. A JSON object key
cannot be a tuple, so the tuple is flattened to a JSON-list string ONLY at the
persistence seam (group_id_key / _parse_group_id_key); everything in memory and
the whole state-machine logic continue to use the tuple.
"""
from __future__ import annotations

import json
from typing import Dict, List, Set, Tuple, Union

from logai.config import AlertConfig
from logai.models import AlertStateEnum, AnomalyResult, AnomalyState
from logai.storage.base import JSONStore


class AlertStateMachine:
    def __init__(self, config: AlertConfig, state_store: JSONStore):
        self.config = config
        self._store = state_store
        # In-memory set of (service, group_id) tuples currently in a non-NORMAL
        # alert state, kept in sync by transition_batch(). Lets the realtime
        # idle-tick enumerate cells needing re-evaluation in O(non-normal) instead
        # of scanning/copying the whole store every tick. The persisted store keys
        # are the flattened JSON-list strings written by group_id_key(), so they
        # are parsed back here: _non_normal MUST always hold tuples, or after a
        # restart groups_not_normal() would return strings that never match the
        # tuple window keys the idle-tick unions against - stranding a stuck alert.
        normal = AlertStateEnum.NORMAL.value
        self._non_normal: Set[Tuple[str, str]] = {
            _parse_group_id_key(gid)
            for gid, raw in state_store.all().items()
            if isinstance(raw, dict) and raw.get("alert_state", normal) != normal
        }

    def _load(self, group_id: Tuple[str, str]) -> AnomalyState:
        raw = self._store.get(group_id_key(group_id))
        if raw:
            state = AnomalyState(**raw)
            # AnomalyState(**raw) yields group_id as a JSON list (tuple was
            # flattened at the seam); the state machine assumes a tuple.
            state.group_id = _coerce_group_id(state.group_id)
            return state
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

        final_by_group: Dict[Tuple[str, str], AnomalyState] = {}
        states: List[AnomalyState] = []
        for result in results:
            prev = final_by_group.get(result.group_id)
            if prev is None:
                prev = self._load(result.group_id)
            state = self._transition(prev, result)
            final_by_group[result.group_id] = state
            states.append(state)

        self._store.bulk_set({
            group_id_key(group_id): _state_payload(state)
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

    def groups_not_normal(self) -> List[Tuple[str, str]]:
        """(service, group_id) tuples of every cell currently in a non-NORMAL
        alert state (WARMING / ALERTING / COOLING).

        O(number of anomalous cells) - reads an in-memory set of tuples, never
        scans or copies the whole state store, so it stays cheap even with a very
        large total number of cells. Always tuples (the persisted flattened keys
        are parsed back in __init__), so callers can union it directly with the
        tuple window keys from FeatureEngine.
        """
        return list(self._non_normal)

    def keys_in_states(self, states: Set[str]) -> Set[Tuple[str, str]]:
        """Persisted (service, group_id) cells whose alert_state is in `states`.
        Legacy plain-string keys are skipped."""
        return {
            identity
            for key, raw in self._store.all().items()
            if isinstance(raw, dict) and raw.get("alert_state") in states
            and isinstance(identity := _parse_group_id_key(key), tuple)
        }

    def drop_group(self, group_id: str) -> int:
        """Delete all persisted service cells for an empty semantic group."""
        keys = []
        for key in self._store.all():
            identity = _parse_group_id_key(key)
            if isinstance(identity, tuple) and len(identity) == 2 and identity[1] == group_id:
                keys.append(key)
            elif identity == group_id:
                keys.append(key)
        for key in keys:
            self._store.delete(key, flush=False)
        if keys:
            self._store.flush()
        self._non_normal = {
            identity
            for identity in self._non_normal
            if not (isinstance(identity, tuple) and len(identity) == 2 and identity[1] == group_id)
        }
        return len(keys)


def _state_payload(state: AnomalyState) -> dict:
    """Serialize an AnomalyState for the JSON store, storing a tuple group_id as a
    JSON array so it round-trips back to a tuple on reload.

    Without this, json.dump(default=str) would stringify the tuple to
    "('hdfs', 'G0001')" - a mangled value that _coerce_group_id cannot parse back.
    The KEY already round-trips via group_id_key/_parse_group_id_key; this keeps the
    VALUE consistent so any future reader of the payload gets a faithful group_id.
    A legacy plain-string group_id is left untouched (not exploded into chars)."""
    payload = dict(state.__dict__)
    gid = payload.get("group_id")
    if isinstance(gid, tuple):
        payload["group_id"] = list(gid)
    return payload


def group_id_key(group_id: Union[Tuple[str, str], str]) -> str:
    """Serialize a window/alert identity to a JSON-object-safe dict key.

    Tuples are flattened to a JSON-list string (e.g. ["hdfs","G0001"]) because
    json.dump rejects tuple keys. A plain str (legacy group_id) is returned
    unchanged so old anomaly_state.json keys keep loading.
    """
    if isinstance(group_id, tuple):
        return json.dumps(list(group_id))
    return group_id


def _coerce_group_id(value: Union[Tuple[str, str], List[str], str]):
    """Normalize a group_id that may arrive as a JSON list (decoded from a
    flattened key) back into a tuple. Plain strings pass through (legacy)."""
    if isinstance(value, list):
        return tuple(value)
    return value


def _parse_group_id_key(key: str) -> Union[Tuple[str, str], str]:
    """Inverse of group_id_key for reading persisted keys back into tuples."""
    return _coerce_group_id(_try_json_list(key))


def _try_json_list(key: str):
    try:
        parsed = json.loads(key)
    except (json.JSONDecodeError, TypeError):
        return key
    return parsed if isinstance(parsed, list) else key

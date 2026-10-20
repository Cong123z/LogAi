"""Per-minute detection: judge each key against its band and step the alert hysteresis."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NamedTuple

from logai2.config import AlertConfig, AppConfig
from logai2.owner.baseline import Baseline, judge
from logai2.owner.counts import SEVERITY, ClosedMinute


@dataclass
class AlertRow:
    service: str
    key: str
    state: str = "NORMAL"
    streak: int = 0
    since: float = 0.0
    score: float = 0.0
    count: int = 0
    band_low: float | None = None
    band_high: float | None = None


class Change(NamedTuple):
    service: str
    key: str
    at: float
    from_state: str
    to_state: str
    count: int
    band_low: float | None
    band_high: float | None


def step(prev: AlertRow, high: bool, cfg: AlertConfig) -> AlertRow:
    """logai-engine AlertStateMachine._transition; floors replace min_events_1m, so low = not high.

    `since` is left to the caller, which knows the minute's time.
    """
    low = not high
    state, count = prev.state, prev.streak
    if state == "NORMAL":
        count = count + 1 if high else 0
        if count >= cfg.warm_consecutive:
            state = "WARMING"
    elif state == "WARMING":
        if high:
            count += 1
            if count >= cfg.alert_consecutive:
                state = "ALERTING"
                count = 0
        else:
            state = "NORMAL"
            count = 0
    elif state == "ALERTING":
        if low:
            state = "COOLING"
            count = 1
        else:
            count = 0  # stays ALERTING; only a low minute starts cooling
    elif state == "COOLING":
        if high:
            state = "ALERTING"
            count = 0
        else:
            count += 1
            if count >= cfg.cool_consecutive:
                state = "NORMAL"
                count = 0
    return replace(prev, state=state, streak=count)


def _worst(levels) -> str:
    return max(levels, key=lambda lv: SEVERITY.get(lv.upper(), 0), default="INFO")


class Detector:
    def __init__(self, cfg: AppConfig, baseline: Baseline, links: dict[tuple[str, str], str],
                 levels: dict[tuple[str, str], str], states: dict[tuple[str, str], AlertRow]):
        self.cfg = cfg
        self.baseline = baseline
        self.links = links
        self.levels = levels
        self.states = states
        self.touched: set[tuple[str, str]] = set()  # keys stepped since the owner last persisted them

    def _level(self, cm: ClosedMinute, k: tuple[str, str]) -> str:
        return self.levels.get(k) or cm.levels.get(k) or "INFO"

    def run(self, cm: ClosedMinute, historic: bool) -> list[Change]:
        if historic:
            return []
        keys = dict(cm.keys)
        doc_levels: dict[tuple[str, str], list[str]] = {}
        doc_tids: dict[tuple[str, str], list[str]] = {}
        for (svc, tid), doc_id in self.links.items():
            dk = (svc, "doc:" + doc_id)
            keys[dk] = keys.get(dk, 0) + cm.keys.get((svc, tid), 0)
            doc_levels.setdefault(dk, []).append(self._level(cm, (svc, tid)))
            doc_tids.setdefault(dk, []).append(tid)
        # ponytail: full scan of states per minute; index non-idle keys if states grow large
        for k, r in self.states.items():
            if r.state != "NORMAL" or r.streak:
                keys.setdefault(k, 0)  # lets an alert recover, or a streak reset, after its key leaves the minute

        at = (cm.minute + 1) * 60.0
        changes = []
        for (svc, key), count in keys.items():
            level = _worst(doc_levels[(svc, key)]) if (svc, key) in doc_levels else self._level(cm, (svc, key))
            lk = (self.baseline.lookup_doc(svc, doc_tids[(svc, key)], cm.day, cm.hour) if (svc, key) in doc_tids
                  else self.baseline.lookup(svc, key, cm.day, cm.hour))
            v = judge(count, key, level, lk, self.cfg)
            prev = self.states.get((svc, key))
            if not v.high and (prev is None or (prev.state == "NORMAL" and not prev.streak)):
                continue
            prev = prev or AlertRow(svc, key)
            row = replace(step(prev, v.high, self.cfg.alert), score=v.score, count=count,
                          band_low=v.band_low, band_high=v.band_high)
            if row.state != prev.state:
                row.since = at
                changes.append(Change(svc, key, at, prev.state, row.state, count, v.band_low, v.band_high))
            self.states[(svc, key)] = row
            self.touched.add((svc, key))
        return changes

    def not_normal(self) -> set[tuple[str, str]]:
        return {k for k, r in self.states.items() if r.state != "NORMAL"}

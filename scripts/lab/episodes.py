"""Episodes (decision D5): minute flags through the live alert state machine."""
from __future__ import annotations

from dataclasses import dataclass

from logai2.config import AlertConfig
from logai2.owner.detect import AlertRow, step


@dataclass(frozen=True)
class Episode:
    start: int          # first high minute of the run that left NORMAL
    end: int            # exclusive: the minute the state returned to NORMAL, or the window end
    peak_score: float
    alerted: int        # minutes in ALERTING or COOLING


class Tracker:
    """One key. Callers skip unobserved minutes. `changes` logs (minute, to_state) for every state change."""

    def __init__(self, alert_cfg: AlertConfig):
        self.cfg = alert_cfg
        self._reset()
        self.changes: list[tuple[int, str]] = []

    def _reset(self) -> None:
        self.row = AlertRow("", "")
        self.start = self.peak = self.alerted = 0
        self.reached = False

    def step(self, minute: int, high: bool, score: float) -> Episode | None:
        prev = self.row
        if prev.state == "NORMAL" and not prev.streak:
            if not high:
                return None
            self.start, self.peak, self.alerted, self.reached = minute, score, 0, False
        self.peak = max(self.peak, score)
        self.row = row = step(prev, high, self.cfg)
        if row.state != prev.state:
            self.changes.append((minute, row.state))
        self.reached |= row.state == "ALERTING"
        self.alerted += row.state in ("ALERTING", "COOLING")
        if row.state == "NORMAL" and prev.state != "NORMAL":
            ep = Episode(self.start, minute, self.peak, self.alerted) if self.reached else None
            self._reset()
            return ep
        return None

    def close(self, end_minute: int) -> Episode | None:
        ep = Episode(self.start, end_minute, self.peak, self.alerted) if self.reached else None
        self._reset()
        return ep


def episodes(flags, alert_cfg: AlertConfig) -> list[Episode]:
    """flags: iterable of (minute, high, score) in order."""
    t, out, last = Tracker(alert_cfg), [], None
    for minute, high, score in flags:
        last = minute
        if (e := t.step(minute, high, score)):
            out.append(e)
    if last is not None and (e := t.close(last + 1)):
        out.append(e)
    return out

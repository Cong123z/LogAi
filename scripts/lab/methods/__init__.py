"""Method contract: what an alerting method implements, the shared D4 rule, and the registry."""
from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple, Protocol

from logai2.config import AppConfig
from logai2.owner.baseline import FLOOR_LEVEL, KEY_LEVEL
from scripts.lab.data import ServiceData


@dataclass(frozen=True)
class Spec:
    name: str
    number: str
    label: str
    kinds: frozenset[str]
    knobs: tuple[float, ...]
    default_knob: float
    band: bool
    heavy: bool
    cost: int
    min_hour_days: int = 0
    min_minute_days: int = 0


class DayOut(NamedTuple):
    expected: array | None      # 'd' arrays of 1440, band methods
    low: array | None
    high: array | None
    score: array                # 'd' 1440
    high_flag: bytearray        # 1440, 1 = high after D4


class Method(Protocol):
    spec: Spec

    def fit(self, sd: ServiceData, day: date, cfg: AppConfig) -> object | None:
        """Fit on data strictly before `day`; None = not enough history."""

    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg: AppConfig) -> dict[float, DayOut]:
        """One key, one day, every knob. prev_model is the previous day's (sigma needs it before the 02:00 commit)."""


class Unavailable(Exception):
    """A heavy method whose package is missing."""


def floor_for(key: str, level: str | None, cfg) -> int | None:
    """The count floor as `judge` maps it: the key's level, else the template's (None = INFO); None = no floor."""
    cls = FLOOR_LEVEL.get(KEY_LEVEL.get(key, (level or "INFO").upper()))
    return cfg.floors[cls] if cls else None


def apply_d4(count, low, high, expected, key, level, cfg, score=None) -> bool:
    """D4 (spec 6.2): the live alert shape of `baseline.judge`. High when the key has a floor (ERROR/WARN, `*error`)
    and the upper test passes and count >= floor; INFO templates and `*total` are never high on the upper side.
    On `*total` only: high when count < low and expected >= cfg.drop.min_mean (low/expected must be known).
    Band methods: upper test is `count > high`. Score methods pass `score` and `high` = threshold: the upper test is
    `score > high`, the floor test still uses the raw `count`. Live also needs a full baseline and not upper_only for
    the drop; those are sigma-specific and bk04's sigma handles them through its own judge call, not here."""
    floor = floor_for(key, level, cfg)
    value = count if score is None else score
    if floor is not None and high is not None and value > high and count >= floor:
        return True
    return (key == "*total" and low is not None and expected is not None
            and count < low and expected >= cfg.drop.min_mean)


METHODS: dict[str, Callable[[], Method]] = {}      # bk04+ register
GROUPS: dict[str, list[str]] = {"core": [], "heavy": []}


def select(arg: str) -> list[str]:
    if arg in ("core", "heavy"):
        return list(GROUPS[arg])
    if arg == "all":
        return GROUPS["core"] + GROUPS["heavy"]
    names = [n.strip() for n in arg.split(",") if n.strip()]
    bad = [n for n in names if n not in METHODS]
    if bad:
        raise ValueError(f"unknown method: {', '.join(bad)}")
    return names


from scripts.lab.methods.sigma import Sigma  # noqa: E402  (registered last: sigma imports Spec from here)

METHODS["sigma"] = Sigma
GROUPS["core"].append("sigma")

"""Monitor kinds M1..M7 (spec 5.3): which key is scored as what."""
from __future__ import annotations

from datetime import date

from logai2.links import level_class
from scripts.lab.data import UNOBSERVED, ServiceData

KINDS = ("M1", "M2", "M3", "M4", "M5", "M6", "M7")
_NOT_SCORED = frozenset({"*warn", "*info", "*minutes"})


def _busy_mean(sd: ServiceData, key: str, tune_days: list[date]) -> float | None:
    """Mean per minute over the hours (observed minutes only) where the key has any count; None = nothing observed."""
    total = minutes = seen = 0
    for day in tune_days:
        a = sd.minutes[key].get(day)
        if a is None:
            continue
        for h in range(24):
            hour = [n for n in a[h * 60:(h + 1) * 60] if n != UNOBSERVED]
            seen += len(hour)
            if sum(hour) > 0:
                total, minutes = total + sum(hour), minutes + len(hour)
    if not seen:
        return None
    return total / minutes if minutes else 0.0


def assign(sd: ServiceData, tune_days: list[date]) -> dict[str, str]:
    kinds = {}
    for key in sd.keys():
        if key == "*total":
            kinds[key] = "M1"
        elif key == "*error":
            kinds[key] = "M2"
        elif key.startswith("doc:"):
            kinds[key] = "M5"
        elif key.startswith("*") or key in _NOT_SCORED or key.endswith(":unparsed"):
            continue
        else:
            mean = _busy_mean(sd, key, tune_days)
            if mean is not None and mean >= 2:
                kinds[key] = "M3"
            elif level_class(sd.levels.get(key)) != "INFO":
                kinds[key] = "M4"
    return kinds


def is_new(sd: ServiceData, key: str, minute: int) -> bool:
    """M6: the template's first 1440 minutes."""
    first = sd.first_seen.get(key)
    return first is not None and 0 <= minute - first < 1440

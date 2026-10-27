"""Vietnamese holidays from `cfg.lab.holidays` (YAML dates or ISO strings) plus a scenario's own days."""
from __future__ import annotations

from datetime import date


def is_holiday(day: date, cfg, extra=()) -> bool:
    return day in {h if isinstance(h, date) else date.fromisoformat(h) for h in (*cfg.lab.holidays, *extra)}

"""The only place local time is computed: every day/hour bucket uses TZ."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def minute_of(ts: float) -> int:
    """UTC epoch seconds -> minute number (epoch seconds // 60)."""
    return int(ts // 60)


def local(minute: int) -> datetime:
    return datetime.fromtimestamp(minute * 60, TZ)


def day_hour(minute: int) -> tuple[date, int]:
    t = local(minute)
    return t.date(), t.hour


def daytype(day: date) -> str:
    return "weekend" if day.weekday() >= 5 else "weekday"

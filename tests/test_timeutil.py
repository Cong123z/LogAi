from datetime import date, datetime, timezone

from logai2.timeutil import day_hour, daytype, minute_of


def test_local_day_hour_crosses_midnight():
    # 2026-10-17 17:30 UTC = 2026-10-18 00:30 local (UTC+7), a Sunday
    m = minute_of(datetime(2026, 10, 17, 17, 30, tzinfo=timezone.utc).timestamp())
    assert day_hour(m) == (date(2026, 10, 18), 0)
    assert daytype(date(2026, 10, 18)) == "weekend"
    assert daytype(date(2026, 10, 19)) == "weekday"

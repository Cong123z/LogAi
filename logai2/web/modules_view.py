"""Template modules over the last 24 h (hourly buckets, local time) for the Templates page."""
from __future__ import annotations

import time
from datetime import date

from logai2.timeutil import day_hour, minute_of

clock = time.time   # tests replace it, so a window never moves under them


def window_hours(now: float) -> list[tuple[date, int]]:
    """The 24 (day, hour) buckets from the current local hour - 23 to the current hour."""
    minute = minute_of(now) // 60 * 60   # whole UTC hours are whole local hours (UTC+7)
    return [day_hour(minute - 60 * back) for back in range(23, -1, -1)]


def window_args(now: float) -> tuple[list[date], list[date], list[int]]:
    """(distinct days, bucket days, bucket hours): the arguments of every window predicate."""
    buckets = window_hours(now)
    return sorted({d for d, _ in buckets}), [d for d, _ in buckets], [h for _, h in buckets]


# a template's window lines in one module; the day filter first so only the window's partitions are read
WINDOW_SQL = ("h.day = ANY(%s) AND (h.day, h.hour) IN (SELECT * FROM unnest(%s::date[], %s::int[]))")


def modules_for(conn, keys: list[tuple[str, str]], now: float) -> dict[tuple[str, str], list[dict]]:
    """Per template: in-window modules by lines desc, then the others by last_seen desc (share None)."""
    out: dict[tuple[str, str], list[dict]] = {k: [] for k in keys}
    if not keys:
        return out
    svcs, tids = [s for s, _ in keys], [t for _, t in keys]
    days, bdays, bhours = window_args(now)
    pair = "(service, template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
    lines = {(r["service"], r["template_id"], r["module_code"]): r["lines"] for r in conn.execute(
        "SELECT service, template_id, module_code, sum(count)::bigint AS lines FROM template_module_hours h "
        f"WHERE {pair} AND {WINDOW_SQL} GROUP BY 1, 2, 3", (svcs, tids, days, bdays, bhours))}
    totals: dict[tuple[str, str], int] = {}
    for (s, t, _), n in lines.items():
        totals[(s, t)] = totals.get((s, t), 0) + n
    rows = conn.execute(f"SELECT service, template_id, module_code, last_seen FROM template_modules WHERE {pair}",
                        (svcs, tids)).fetchall()
    for r in rows:
        key = (r["service"], r["template_id"])
        n = lines.get((*key, r["module_code"]), 0)
        out[key].append({"module": r["module_code"], "lines": n, "share": n / totals[key] if n else None,
                         "last_seen": r["last_seen"].timestamp()})
    for mods in out.values():   # in-window first (lines desc), then last_seen desc; name keeps ties stable
        mods.sort(key=lambda m: (m["lines"] == 0, -m["lines"], -m["last_seen"], m["module"]))
    return out

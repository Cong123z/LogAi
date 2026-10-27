"""Load one service's recorded counts and history into a ServiceData. Reads only."""
from __future__ import annotations

from array import array
from datetime import date, timedelta

from logai2.links import ALERTING_SOURCES
from logai2.timeutil import minute_of
from scripts.lab.data import MIX, UNOBSERVED, Incident, ServiceData, minute_at


def services(conn, window_from: date, window_to: date) -> list[str]:
    """Services with lab rows in the window."""
    return [r["service"] for r in conn.execute(
        "SELECT DISTINCT service FROM lab_minute_counts WHERE day BETWEEN %s AND %s ORDER BY 1",
        (window_from, window_to))]


def load_service(conn, service: str, window_from: date, window_to: date, history_days: int) -> ServiceData:
    days = [window_from + timedelta(n) for n in range((window_to - window_from).days + 1)]
    since = window_from - timedelta(history_days)
    rows = conn.execute("SELECT key, day, hour, counts FROM lab_minute_counts "
                        "WHERE service = %s AND day BETWEEN %s AND %s", (service, window_from, window_to)).fetchall()
    by_key: dict[str, dict[tuple[date, int], list[int]]] = {}
    for r in rows:
        by_key.setdefault(r["key"], {})[(r["day"], r["hour"])] = r["counts"]
    for always in ("*total", "*error"):      # all-zero rows are not stored: a service without errors still has M2
        by_key.setdefault(always, {})
    obs_rows = by_key.get("*minutes", {})
    observed = {d: array("i", [x for h in range(24) for x in obs_rows.get((d, h), [0] * 60)]) for d in days}
    minutes = {}
    for key, hours in by_key.items():
        minutes[key] = per_day = {}
        for d in days:
            a = array("i", [0 if o else UNOBSERVED for o in observed[d]])
            for h in range(24):
                counts = hours.get((d, h))
                if counts:
                    for i, n in enumerate(counts):
                        if observed[d][h * 60 + i]:
                            a[h * 60 + i] = n
            per_day[d] = a
    templates = conn.execute("SELECT template_id, level, first_seen FROM templates WHERE service = %s",
                             (service,)).fetchall()
    docs: dict[str, list[str]] = {}
    for r in conn.execute("SELECT template_id, doc_id FROM template_doc_links WHERE service = %s "
                          "AND source = ANY(%s) AND doc_id IS NOT NULL ORDER BY doc_id, template_id",
                          (service, list(ALERTING_SOURCES))):
        docs.setdefault(f"doc:{r['doc_id']}", []).append(r["template_id"])
    for doc, tids in docs.items():
        minutes[doc] = {}
        for d in days:
            a = array("i", [0 if o else UNOBSERVED for o in observed[d]])
            for t in tids:
                src = minutes.get(t, {}).get(d)
                if src is not None:
                    for i, n in enumerate(src):
                        if n != UNOBSERVED:
                            a[i] += n
            minutes[doc][d] = a
    minutes[MIX] = minutes["*total"]       # M7 judges the mix of a service whose total looks normal: same arrays
    args = (service, since, window_to)
    excluded = set()
    for r in conn.execute("SELECT day, hour_from, hour_to FROM review_exclusions WHERE service = %s "
                          "AND decision = 'exclude' AND day BETWEEN %s AND %s", args):
        excluded |= {(r["day"], h) for h in range(r["hour_from"], r["hour_to"])}
    lo, hi = minute_at(window_from, 0), minute_at(window_to, 0) + 1440      # +-15 min: score.py's margin
    incidents = [Incident(r["key"], r["start_minute"], r["end_minute"], r["source"]) for r in conn.execute(
        "SELECT key, start_minute, end_minute, source FROM lab_incidents WHERE service = %s AND start_minute < %s "
        "AND end_minute > %s ORDER BY start_minute, id", (service, hi + 15, lo - 15))]
    return ServiceData(
        service, days, minutes, observed,
        {(r["key"], r["day"], r["hour"]): (r["sum"], r["sumsq"]) for r in conn.execute(
            "SELECT key, day, hour, sum, sumsq FROM hourly_counts WHERE service = %s AND day BETWEEN %s AND %s", args)},
        {(r["day"], r["hour"]): r["n_minutes"] for r in conn.execute(
            "SELECT day, hour, n_minutes FROM service_hours WHERE service = %s AND day BETWEEN %s AND %s", args)},
        {r["day"] for r in conn.execute(
            "SELECT day FROM day_status WHERE service = %s AND committed AND day BETWEEN %s AND %s", args)},
        excluded,
        {t["template_id"]: t["level"] for t in templates},
        {t["template_id"]: minute_of(t["first_seen"].timestamp()) for t in templates},
        docs, incidents)

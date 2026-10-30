"""Module alert API core: parse the caller's window and list the templates that were WARMING or
ALERTING in it, with one module's counts. alert_changes.at is the END of a minute; everything
here works in minute STARTS (epoch seconds)."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone

from logai2 import links
from logai2.review import periods
from logai2.timeutil import TZ


def _to_seconds(v: str) -> float:
    try:
        x = float(v)
        if not math.isfinite(x):
            raise ValueError
        return x / 1000 if x > 1e11 else x
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"cannot parse time {v!r}") from None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def parse_window(args, now: float, retention_days: int) -> tuple[str, float, float, float, float]:
    module = (args.get("module_code") or "").strip()
    if not module:
        raise ValueError("module_code is required")
    if not args.get("from") or not args.get("to"):
        raise ValueError("from and to are required")
    from_s, to_s = _to_seconds(args["from"]), _to_seconds(args["to"])
    if from_s > to_s:
        raise ValueError("from is after to")
    oldest_day = datetime.fromtimestamp(now, TZ).date() - timedelta(days=retention_days)
    oldest = datetime.combine(oldest_day, dtime.min, TZ).timestamp()
    lo, hi = max(from_s, oldest), min(to_s, now)
    hi = max(hi, lo)
    start = math.floor(lo / 60) * 60.0
    end = math.ceil(hi / 60) * 60.0
    if end == start:
        end = start + 60
    return module, from_s, to_s, start, end


def data_until(conn) -> float | None:
    row = conn.execute("SELECT status FROM worker_status WHERE worker = 'engine'").fetchone()
    if not row:
        return None
    s = row["status"]
    return math.floor((s["heartbeat"] - s["lag_seconds"]) / 60) * 60.0


def _day(ts: float):
    return datetime.fromtimestamp(ts, TZ).date()


def module_alerts(conn, module_code: str, start: float, end: float, warm_consecutive: int = 2) -> list[dict]:
    services = [r["service"] for r in conn.execute("SELECT service FROM modules WHERE module_code = %s "
        "AND service IN (SELECT service FROM services WHERE monitored)", (module_code,))]
    if not services:
        return []
    ts = lambda x: datetime.fromtimestamp(x, timezone.utc)
    rows = conn.execute(
        """SELECT * FROM (
             SELECT DISTINCT ON (service, key) service, key, at, from_state, to_state FROM alert_changes
              WHERE service = ANY(%(s)s) AND key NOT LIKE '*%%' AND key NOT LIKE 'doc:%%' AND at < %(lo)s
              ORDER BY service, key, at DESC) before
           UNION ALL
           SELECT service, key, at, from_state, to_state FROM alert_changes
            WHERE service = ANY(%(s)s) AND key NOT LIKE '*%%' AND key NOT LIKE 'doc:%%'
              AND at >= %(lo)s AND at < %(hi)s
           ORDER BY at""",
        {"s": services, "lo": ts(start + 60), "hi": ts(end + 60 * warm_consecutive)}).fetchall()
    by_service = defaultdict(list)
    for r in rows:
        by_service[r["service"]].append(r)

    # (service, key) -> {"p": [(start, end|None, backdated)], "alerting": bool}
    found: dict[tuple[str, str], dict] = {}
    for svc, changes in by_service.items():
        first = {}                                    # opening change of each key's periods, by minute-end
        for c in changes:
            first.setdefault((c["key"], c["at"].timestamp()), c)
        p = periods(changes, end + 60, {"WARMING", "ALERTING"})
        a = periods(changes, end + 60, {"ALERTING"})
        for key, ps, pe in p:
            opener = first[(key, ps)]
            ps -= 60
            if opener["from_state"] == "NORMAL" and opener["to_state"] == "WARMING":
                ps -= (warm_consecutive - 1) * 60
            pe -= 60
            if ps >= end or pe <= start:
                continue
            f = found.setdefault((svc, key), {"p": [], "alerting": False})
            f["p"].append((ps, pe))
        for key, as_, ae in a:
            if (svc, key) in found and as_ - 60 < end and ae - 60 > start:
                found[(svc, key)]["alerting"] = True
    if not found:
        return []

    def counted(svc_key, m):
        return start <= m < end and any(ps <= m < pe for ps, pe in found[svc_key]["p"])

    svcs, keys = [k[0] for k in found], [k[1] for k in found]
    pairs = "(service, {}) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
    minutes = conn.execute(
        f"""SELECT service, key, module_code, minute, count FROM alert_minutes
            WHERE {pairs.format('key')} AND day BETWEEN %s AND %s AND minute >= %s AND minute < %s""",
        (svcs, keys, _day(start), _day(end - 1), ts(start), ts(end))).fetchall()
    mod: dict = defaultdict(lambda: defaultdict(int))   # (svc, key) -> minute -> module count
    tpl: dict = defaultdict(lambda: defaultdict(int))
    for r in minutes:
        k, m = (r["service"], r["key"]), r["minute"].timestamp()
        if k in found and counted(k, m):
            tpl[k][m] += r["count"]
            if r["module_code"] == module_code:
                mod[k][m] += r["count"]

    meta = {(r["service"], r["template_id"]): r for r in conn.execute(
        f"SELECT service, template_id, level, text FROM templates WHERE {pairs.format('template_id')}",
        (svcs, keys))}
    docs = {(r["service"], r["template_id"]): r for r in conn.execute(
        """SELECT l.service, l.template_id, d.doc_id, d.title FROM template_doc_links l
             JOIN documents d USING (doc_id)
            WHERE (l.service, l.template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))
              AND l.source = ANY(%s)""",
        (svcs, keys, list(links.ALERTING_SOURCES)))}

    def stats(per_minute, n):
        total = sum(per_minute.values())
        return {"total": total, "avg": round(total / n, 2) if n else 0.0, "peak": max(per_minute.values(), default=0)}

    out = []
    for k, f in found.items():
        if not sum(mod[k].values()):
            continue
        svc, key = k
        n = round(sum((min(pe, end) - max(ps, start)) / 60 for ps, pe in f["p"]))
        m_stats = stats(mod[k], n)
        if end - start <= 86400:
            m_stats["series"] = [{"minute": m, "count": c} for m, c in sorted(mod[k].items())]
        else:
            hours: dict = defaultdict(int)
            for m, c in mod[k].items():
                # ponytail: epoch hour floor equals the local hour only for a whole-hour offset without DST (+07)
                hours[math.floor(m / 3600) * 3600.0] += c
            m_stats["series"] = [{"hour": h, "count": c} for h, c in sorted(hours.items())]
        info, doc = meta.get(k), docs.get(k)
        out.append({
            "service": svc, "template_id": key,
            "text": info["text"] if info else "", "level": info["level"] if info else "INFO",
            "doc_id": doc["doc_id"] if doc else None, "doc_title": doc["title"] if doc else None,
            "peak_state": "ALERTING" if f["alerting"] else "WARMING",
            "periods": [{"start": ps, "end": None if pe >= end else pe} for ps, pe in sorted(f["p"])],
            "alerting_minutes": n, "module": m_stats, "template": stats(tpl[k], n),
        })
    out.sort(key=lambda i: (-i["module"]["total"], i["service"], i["template_id"]))
    return out

"""Pipeline page data: per-service throughput, and the status of each pipeline stage."""
from __future__ import annotations

from datetime import date, datetime

from logai2.config import AppConfig

LAG_WARN_SECONDS = 300            # minutes & detection: lag above this is Degraded
FLUSH_WARN_SECONDS = 10           # Postgres flush: the slowest flush of the last 15 min above this is Degraded
COMMIT_OVERDUE_SECONDS = 26 * 3600
READER_IDLE_POINTS = 5            # reader Down: this many history points (minutes) with 0 events/s ...
INDEX_BEHIND_MS = 300_000         # ... while an index is not caught up; an index this far behind is Degraded


def _by_service(conn, sql: str, *args) -> dict[str, int]:
    return {r["service"]: r["n"] for r in conn.execute(sql, args)}


def throughput(engine: dict) -> dict[str, dict]:
    """Per service from the engine's `service_minutes` (events per closed minute, oldest first): the points, the
    last minute, the mean of the last 15 and the change against the first point when that is 55-60 minutes old."""
    out = {}
    for service, pts in (engine.get("service_minutes") or {}).items():
        if not pts:
            continue
        last = pts[-1][1]
        tail = [n for _, n in pts[-15:]]
        first_t, first_n = pts[0]
        delta = last / first_n - 1 if 55 * 60 <= pts[-1][0] - first_t <= 60 * 60 and first_n > 0 else None
        out[service] = {"points": pts, "last": last, "avg15": sum(tail) / len(tail), "delta_1h": delta}
    return out


def services(conn, cfg: AppConfig, engine: dict, day: date, hour: int) -> list[dict]:
    """One row per service, by name."""
    tp = throughput(engine)
    templates = _by_service(conn, "SELECT service, count(*) AS n FROM templates "
                                   "WHERE service IN (SELECT service FROM services WHERE monitored) GROUP BY 1")
    new = _by_service(conn, "SELECT service, count(*) AS n FROM templates "
                            "WHERE first_seen > now() - interval '24 hours' "
                            "AND service IN (SELECT service FROM services WHERE monitored) GROUP BY 1")
    not_normal = _by_service(conn, "SELECT service, count(*) AS n FROM alert_state WHERE state <> 'NORMAL' "
                                       "AND service IN (SELECT service FROM services WHERE monitored) GROUP BY 1")
    unparsed = {r["key"]: r["sum"] for r in conn.execute(
        "SELECT key, sum FROM hourly_counts WHERE day = %s AND hour = %s AND key LIKE '%%:unparsed'", (day, hour))}
    rows = []
    for r in conn.execute("SELECT service FROM services WHERE monitored ORDER BY service").fetchall():
        service, t = r["service"], tp.get(r["service"], {})
        rows.append({"service": service, "last": t.get("last"), "avg15": t.get("avg15"), "delta_1h": t.get("delta_1h"),
                     "miss_ratio": (engine.get("miss_ratio") or {}).get(service),
                     "templates": templates.get(service, 0), "new_24h": new.get(service, 0),
                     "unparsed_1h": unparsed.get(f"{service}:unparsed", 0),
                     "not_normal": not_normal.get(service, 0)})
    return rows


def _age(iso: str, now: float) -> float | None:
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None      # unparsable: unknown, not overdue


def _stage(status: str = "ok", reason: str | None = None) -> dict:
    return {"status": status, "reason": reason}


def stage_states(engine: dict, engine_stale: bool, linker: dict | None, linker_stale: bool,
                 unparsed_1h: int, now: float) -> dict[str, dict]:
    """Status of each pipeline stage from the workers' status JSON alone (no database)."""
    indices = engine.get("indices") or {}
    behind = [i for i, s in indices.items() if not s.get("caught_up")]
    failing = [i for i, s in indices.items() if s.get("failing")]
    slow = [i for i in behind if now * 1000 - (indices[i].get("pos_ms") or now * 1000) > INDEX_BEHIND_MS]
    if engine_stale:
        es = _stage("warn", "position unknown (engine stale)")
    elif indices and len(failing) == len(indices):
        es = _stage("bad", "all patterns failing")
    elif failing or slow:
        es = _stage("warn", f"failing: {', '.join(failing)}" if failing else f"catching up: {', '.join(slow)}")
    else:
        es = _stage()

    history = engine.get("history") or []
    miss = next((w for w in engine.get("warnings") or [] if "miss ratio" in w), None)
    if engine_stale:
        reader = _stage("bad", "engine stale")
    elif behind and len(history) >= READER_IDLE_POINTS and all(p.get("eps") == 0 for p in history[-READER_IDLE_POINTS:]):
        reader = _stage("bad", f"no events read for {READER_IDLE_POINTS} min while {behind[0]} is behind")
    else:
        reader = _stage("warn", miss) if miss else _stage()

    if engine_stale:
        detection = flush = _stage("bad", "engine stale")
    else:
        detection = (_stage("warn", f"lag {engine['lag_seconds']:.0f} s") if (engine.get("lag_seconds") or 0) > LAG_WARN_SECONDS
                     else _stage("warn", "detection off") if not engine.get("detection_enabled") else _stage())
        at = (engine.get("last_commit") or {}).get("at")
        if (engine.get("flush_seconds_max") or 0) > FLUSH_WARN_SECONDS:
            flush = _stage("warn", f"flush up to {engine['flush_seconds_max']:.1f} s")
        elif at and (_age(at, now) or 0) > COMMIT_OVERDUE_SECONDS:
            flush = _stage("warn", "last commit overdue")
        else:
            flush = _stage()

    if linker is None or linker_stale:
        link = _stage("bad", "no linker status" if linker is None else "linker stale")
        llm = _stage("bad", "linker not running")
    else:
        link = _stage("warn", linker["last_error"]) if linker.get("last_error") else _stage()
        profiles = linker.get("llm_profiles") or {}
        down = [p for p, s in profiles.items() if (s.get("down_until") or 0) > now]
        llm = (_stage("bad", "no active LLM profile") if not profiles else
               _stage("bad", "all LLM profiles down") if len(down) == len(profiles) else
               _stage("warn", f"profile down: {', '.join(down)}") if down else _stage())

    return {"elasticsearch": es, "reader": reader,
            "templates": _stage("warn", f"{unparsed_1h} unparsed lines in the last hour") if unparsed_1h else _stage(),
            "detection": detection, "flush": flush, "prometheus": _stage(), "linker": link, "llm": llm}

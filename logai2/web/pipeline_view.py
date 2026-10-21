"""Pipeline page data: per-service rate against the expected band, and the status of each pipeline stage."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from logai2.config import AppConfig
from logai2.owner.baseline import Baseline
from logai2.web.baseline_view import _cells, _lookup

LAG_WARN_SECONDS = 300            # minutes & detection: lag above this is Degraded
FLUSH_WARN_SECONDS = 10           # Postgres flush: the slowest flush of the last 15 min above this is Degraded
COMMIT_OVERDUE_SECONDS = 26 * 3600
READER_IDLE_POINTS = 5            # reader Down: this many history points (minutes) with 0 events/s ...
INDEX_BEHIND_MS = 300_000         # ... while an index is not caught up; an index this far behind is Degraded
TOTAL = "*total"


def _by_service(conn, sql: str, *args) -> dict[str, int]:
    return {r["service"]: r["n"] for r in conn.execute(sql, args)}


def services(conn, cfg: AppConfig, engine: dict, day: date, hour: int) -> list[dict]:
    """One row per service, worst |z| first, services without a baseline last."""
    last = day - timedelta(days=7)
    templates = _by_service(conn, "SELECT service, count(*) AS n FROM templates GROUP BY 1")
    new = _by_service(conn, "SELECT service, count(*) AS n FROM templates "
                            "WHERE first_seen > now() - interval '24 hours' GROUP BY 1")
    not_normal = _by_service(conn, "SELECT service, count(*) AS n FROM alert_state WHERE state <> 'NORMAL' GROUP BY 1")
    unparsed = {r["key"]: r["sum"] for r in conn.execute(
        "SELECT key, sum FROM hourly_counts WHERE day = %s AND hour = %s AND key LIKE '%%:unparsed'", (day, hour))}
    rows = []
    for service in [r["service"] for r in conn.execute("SELECT service FROM services")]:
        cells = _cells(conn, cfg, service, [TOTAL], "total", [day, last])
        now, then = cells.get((day, hour)), cells.get((last, hour))
        rows.append({"service": service, "rate": now and now["rate"], "last_week": then and then["rate"],
                     "z": now and now["z"], "miss_ratio": (engine.get("miss_ratio") or {}).get(service),
                     "templates": templates.get(service, 0), "new_24h": new.get(service, 0),
                     "unparsed_1h": unparsed.get(f"{service}:unparsed", 0),
                     "not_normal": not_normal.get(service, 0)})
    return sorted(rows, key=lambda r: (r["z"] is None, -abs(r["z"] or 0), r["service"]))


def volume(conn, cfg: AppConfig, day: date, hour: int) -> list[dict]:
    """Per service, hours 0..hour of the day: rate (null without a service_hours row), *total band, same hour last week."""
    last = day - timedelta(days=7)
    out = []
    for r in conn.execute("SELECT service FROM services ORDER BY service").fetchall():
        service = r["service"]
        cells = _cells(conn, cfg, service, [TOTAL], "total", [day, last])
        bl = Baseline.load(conn, [(service, TOTAL)])
        hrs = []
        for h in range(hour + 1):
            c, lw = cells.get((day, h)), cells.get((last, h))
            band = c or _lookup(bl, cfg, service, [TOTAL], "total", day, h)[1]._asdict()   # an hour without a row still has its band
            hrs.append({"hour": h, "rate": c and c["rate"], "band_low": band["band_low"],
                        "band_high": band["band_high"], "last_week": lw and lw["rate"]})
        out.append({"service": service, "hours": hrs})
    return out


def _stage(status: str = "ok", reason: str | None = None) -> dict:
    return {"status": status, "reason": reason}


def stage_states(engine: dict, engine_stale: bool, linker: dict | None, linker_stale: bool,
                 unparsed_1h: int, now: float) -> dict[str, dict]:
    """Status of each pipeline stage from the workers' status JSON alone (no database)."""
    indices = engine.get("indices") or {}
    behind = [i for i, s in indices.items() if not s.get("caught_up")]
    failing = [i for i, s in indices.items() if s.get("failing")]
    slow = [i for i in behind if now * 1000 - (indices[i].get("pos_ms") or now * 1000) > INDEX_BEHIND_MS]
    if indices and len(failing) == len(indices):
        es = _stage("bad", "all indices failing")
    elif failing or slow:
        es = _stage("warn", f"failing: {', '.join(failing)}" if failing else f"catching up: {', '.join(slow)}")
    else:
        es = _stage()

    history = engine.get("history") or []
    miss = next((w for w in engine.get("warnings") or [] if "miss ratio" in w), None)
    if behind and len(history) >= READER_IDLE_POINTS and all(p.get("eps") == 0 for p in history[-READER_IDLE_POINTS:]):
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
        elif at and now - datetime.fromisoformat(at).timestamp() > COMMIT_OVERDUE_SECONDS:
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

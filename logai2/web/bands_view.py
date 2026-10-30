"""Evaluation page data: the 4 band methods for one key, computed from Postgres on every request (spec §6)."""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone

from logai2 import db
from logai2.bands import METHODS, SERVICE_KEYS, _lookback, band, best_k, history, in_scope, kind_of, stored_tune, training, tune
from logai2.links import level_class
from logai2.timeutil import TZ
from logai2.web.baseline_view import UnknownKey

log = logging.getLogger(__name__)
NOT_ENOUGH = "not enough days"


def bands_series(conn, cfg, service: str, key: str, ndays: int, today: date, now: float) -> dict:
    when = datetime.fromtimestamp(now, TZ)
    if not in_scope(conn, service, key, when):
        raise UnknownKey(key)
    days = [today - timedelta(days=i) for i in range(ndays - 1, -1, -1)]
    hist = history(conn, service, key, _lookback(conn, service, days), today)
    prior = [r for (d, _), r in hist.items() if d < days[0]] or list(hist.values())
    kind = kind_of(key, sum(r.s for r in prior) / max(1, sum(r.n for r in prior)))
    rates = {(r["day"], r["hour"]): r["s"] / r["n"] for r in conn.execute(
        "SELECT sh.day, sh.hour, sh.n_minutes AS n, coalesce(hc.sum, 0) AS s FROM service_hours sh "
        "LEFT JOIN hourly_counts hc ON hc.service = sh.service AND hc.key = %s AND hc.day = sh.day AND hc.hour = sh.hour "
        "WHERE sh.service = %s AND sh.day BETWEEN %s AND %s AND sh.n_minutes > 0", (key, service, days[0], today))}
    pts = [(d, h) for d in days for h in range(when.hour + 1 if d == today else 24)]
    stored = stored_tune(conn)
    fit = ((stored or {}).get("kinds", {}).get(kind, {}).get("methods", {}))
    methods = []
    for m in METHODS:
        k, tuned = best_k(stored, kind, m)
        bands = []
        for d, h in pts:
            b = band(m, training(hist, d, h, m), k)
            bands.append(b and {"day": d.isoformat(), "hour": h, "low": b.low, "high": b.high})
        outside = sum(1 for (d, h), b in zip(pts, bands)
                      if b and (r := rates.get((d, h))) is not None and (r > b["high"] or (key == "*total" and r < b["low"])))
        methods.append({"id": m, "k": k, "tuned": tuned, "coverage": fit.get(m, {}).get("coverage"),
                        "ratio": fit.get(m, {}).get("ratio"), "bands": bands, "outside": outside,
                        "missing": None if any(bands) else NOT_ENOUGH})
    return {"key": key, "kind": kind, "tuned": stored is not None, "run_at": (stored or {}).get("run_at"),
            "test_days": (stored or {}).get("test_days", []),
            "points": [{"day": d.isoformat(), "hour": h, "rate": rates.get((d, h))} for d, h in pts],
            "methods": methods}


def eval_keys(conn, service: str, now: datetime) -> list[dict]:
    """In-scope keys of a monitored service: service keys first, then templates by level class, then text."""
    if not conn.execute("SELECT 1 FROM services WHERE service = %s AND monitored", (service,)).fetchone():
        return []
    mins = conn.execute("SELECT coalesce(sum(sh.n_minutes), 0) AS n FROM service_hours sh JOIN day_status ds "
                        "ON ds.service = sh.service AND ds.day = sh.day AND ds.committed "
                        "WHERE sh.service = %s AND sh.n_minutes > 0", (service,)).fetchone()["n"]
    sums = {r["key"]: r["s"] for r in conn.execute(
        "SELECT hc.key, sum(hc.sum) AS s FROM hourly_counts hc JOIN day_status ds "
        "ON ds.service = hc.service AND ds.day = hc.day AND ds.committed WHERE hc.service = %s GROUP BY hc.key", (service,))}
    kind = lambda k: kind_of(k, sums.get(k, 0) / max(1, mins))      # noqa: E731
    out = [{"key": k, "label": k, "kind": kind(k)} for k in SERVICE_KEYS]
    tpl = conn.execute("SELECT template_id, level, text FROM templates WHERE service = %s AND first_seen < %s "
                       "AND template_id NOT LIKE 'doc:%%' AND template_id NOT LIKE 'verdict:%%'",
                       (service, now - timedelta(hours=24))).fetchall()
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    tpl.sort(key=lambda t: (order[level_class(t["level"])], t["text"], t["template_id"]))
    out += [{"key": t["template_id"], "label": f"{t['text']} ({t['level'].upper()}, {kind(t['template_id'])})",
             "kind": kind(t["template_id"])} for t in tpl]
    return out


class Retune:
    """One Re-tune at a time, in a web thread with its own connection. The status lives in this process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._s = {"status": "idle", "started_at": None, "error": None, "progress": None}

    def start(self, dsn: str, cfg) -> bool:
        with self._lock:
            if self._s["status"] == "running":
                return False
            self._s = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                       "error": None, "progress": {"done": 0, "total": 0}}
        threading.Thread(target=self._run, args=(dsn, cfg), daemon=True).start()
        return True

    def status(self) -> dict:
        with self._lock:
            return dict(self._s)

    def _tick(self, done: int, total: int) -> None:
        with self._lock:
            self._s["progress"] = {"done": done, "total": total}

    def _run(self, dsn: str, cfg) -> None:
        error = None
        try:
            conn = db.connect(dsn)
            try:
                tune(conn, cfg, datetime.now(TZ).date(), on_key=self._tick)      # commits itself
            finally:
                conn.close()
        except Exception as exc:       # noqa: BLE001 - shown on the page
            log.exception("Re-tune failed")
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._s.update(status="idle", error=error, progress=None)


retune = Retune()

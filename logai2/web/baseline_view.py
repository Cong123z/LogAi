"""Review page data: the days x hours matrix of one key against its baseline, and what the baseline is built from."""
from __future__ import annotations

from datetime import date, timedelta

from logai2.config import AppConfig
from logai2.links import ALERTING_SOURCES
from logai2.owner.baseline import DAY_CAP, Baseline, full_after, judge
from logai2.review import keys_by_hour, minutes_by_hour, periods
from logai2.timeutil import daytype

DROP_KEYS = ("*total", "*error")     # the only keys that alert (and colour) low
SIGMA_FLOOR = 1.0                    # events/min: a flat key (σ 0) still needs a scale


class UnknownKey(Exception):
    pass


def resolve_key(conn, service: str, key: str) -> tuple[str, list[str]]:
    """key -> (kind, template ids whose counts it sums)."""
    if key in DROP_KEYS:
        return key[1:], [key]
    if key.startswith("doc:"):
        ids = [r["template_id"] for r in conn.execute(
            "SELECT template_id FROM template_doc_links WHERE service = %s AND doc_id = %s AND source = ANY(%s) "
            "ORDER BY template_id", (service, key[4:], list(ALERTING_SOURCES)))]
        if ids:
            return "doc", ids
    elif conn.execute("SELECT 1 FROM templates WHERE service = %s AND template_id = %s", (service, key)).fetchone():
        return "template", [key]
    raise UnknownKey(key)


def picked_days(conn, service: str) -> dict[str, list[date]]:
    """Per daytype the committed days the nightly rebuild uses (jobs/commit.py REBUILD), newest first."""
    out: dict[str, list[date]] = {"weekday": [], "weekend": []}
    for r in conn.execute("SELECT day, daytype FROM day_status WHERE service = %s AND committed "
                          "ORDER BY day DESC", (service,)):
        if len(out[r["daytype"]]) < DAY_CAP[r["daytype"]]:
            out[r["daytype"]].append(r["day"])
    return out


def _excluded(conn, service: str, days: list[date]) -> dict[date, list[tuple[int, int]]]:
    out: dict[date, list[tuple[int, int]]] = {}
    for r in conn.execute("SELECT day, hour_from, hour_to FROM review_exclusions "
                          "WHERE service = %s AND decision = 'exclude' AND day = ANY(%s)", (service, days)):
        out.setdefault(r["day"], []).append((r["hour_from"], r["hour_to"]))
    return out


def left_days(conn, service: str, day: date, hour_from: int, hour_to: int, extra_exclude: bool) -> list[dict]:
    """Usable days per hour of [hour_from, hour_to) for the daytype of `day`, optionally with `day` excluded there."""
    kind = daytype(day)
    picked = picked_days(conn, service)[kind]
    excl = _excluded(conn, service, picked)
    if extra_exclude:
        excl.setdefault(day, []).append((hour_from, hour_to))
    return [{"hour": h, "of": DAY_CAP[kind],
             "days": sum(1 for d in picked if not any(a <= h < b for a, b in excl.get(d, [])))}
            for h in range(hour_from, hour_to)]


def _cls(z: float, k: float, down_ok: bool) -> str:
    if abs(z) <= k or (z < 0 and not down_ok):
        return "in"
    m = abs(z)
    return ("up" if z > 0 else "down") + ("1" if m <= 6 else "2" if m <= 10 else "3")


def _cells(conn, cfg: AppConfig, service: str, template_ids: list[str], kind: str,
           days: list[date]) -> dict[tuple[date, int], dict]:
    """Per (day, hour) with minutes read: rate, mean, std, band_low, band_high, z, no_baseline, cls."""
    bl = Baseline.load(conn, [(service, t) for t in template_ids])
    minutes = {(r["day"], r["hour"]): r["n_minutes"] for r in conn.execute(
        "SELECT day, hour, n_minutes FROM service_hours WHERE service = %s AND day = ANY(%s) AND n_minutes > 0",
        (service, days))}
    sums = {(r["day"], r["hour"]): r["s"] for r in conn.execute(
        "SELECT day, hour, sum(sum)::float AS s FROM hourly_counts "
        "WHERE service = %s AND key = ANY(%s) AND day = ANY(%s) GROUP BY day, hour", (service, template_ids, days))}
    key = "doc" if kind == "doc" else template_ids[0]
    out = {}
    for (day, hour), n in minutes.items():
        rate = sums.get((day, hour), 0.0) / n
        lk = bl.lookup_doc(service, template_ids, day, hour) if kind == "doc" else bl.lookup(service, key, day, hour)
        v = judge(0, key, "INFO", lk, cfg)     # count 0: only the band is used
        mean, std = (lk.this.mean, lk.this.std) if lk.this else (None, None)
        none = lk.this is None or (mean == 0 and std == 0)
        z, cls = None, "none"
        if lk.this is not None and none:       # a zero baseline: any event is the top step
            cls = "up3" if rate > 0 else "in"
        if not none:
            z = (rate - mean) / (max(std, SIGMA_FLOOR) if mean > 0 else std)
            full = lk.days >= full_after(cfg, lk.daytype)
            k = cfg.baseline.sigma_multiplier if full else cfg.baseline.young_sigma_multiplier
            cls = _cls(z, k, key in DROP_KEYS and not lk.upper_only)
        out[(day, hour)] = {"rate": rate, "mean": mean, "std": std, "band_low": v.band_low,
                            "band_high": v.band_high, "z": z, "no_baseline": none, "cls": cls}
    return out


def matrix(conn, cfg: AppConfig, service: str, key: str, ndays: int, today: date, now: float) -> dict:
    kind, ids = resolve_key(conn, service, key)
    days = [today - timedelta(days=i) for i in range(ndays)]
    cells = _cells(conn, cfg, service, ids, kind, days)
    picked = picked_days(conn, service)

    changes = conn.execute("SELECT key, at, to_state FROM alert_changes WHERE service = %s ORDER BY at",
                           (service,)).fetchall()
    ps = periods(changes, now)
    not_normal, keys = minutes_by_hour(ps), keys_by_hour(ps)
    decisions: dict[tuple[date, int], str] = {}
    for r in conn.execute("SELECT day, hour_from, hour_to, decision FROM review_exclusions "
                          "WHERE service = %s AND day = ANY(%s) ORDER BY id", (service, days)):
        for h in range(r["hour_from"], r["hour_to"]):
            decisions[(r["day"], h)] = r["decision"]

    rows = []
    for d in days:
        kind_d = daytype(d)
        hrs = [{"hour": h, **c, "not_normal_minutes": not_normal.get((d, h), 0),
                "suggested": (d, h) in keys and (d, h) not in decisions, "decision": decisions.get((d, h))}
               for (cd, h), c in sorted(cells.items()) if cd == d]
        rows.append({"day": d.isoformat(), "daytype": kind_d, "in_baseline": d in picked[kind_d], "hours": hrs})

    excl = _excluded(conn, service, [d for ds in picked.values() for d in ds])
    idle = {(r["daytype"], r["hour"]) for r in conn.execute(
        "SELECT daytype, hour FROM baseline WHERE service = %s AND key = '*total' AND mean = 0", (service,))}
    coverage = {}
    for dt, ds in picked.items():
        coverage[dt] = []
        for h in range(24):
            out = [d for d in ds if any(a <= h < b for a, b in excl.get(d, []))]
            used = [d.isoformat() for d in ds if d not in out]
            coverage[dt].append({"hour": h, "days": len(used), "of": DAY_CAP[dt], "idle": (dt, h) in idle,
                                 "used": used, "excluded": [d.isoformat() for d in out]})

    def profile(dt: str) -> str:
        n = len(picked[dt])
        if dt == "weekend" and n < 2:
            return "borrowed" if picked["weekday"] else "none"
        return "none" if n == 0 else "full" if n >= full_after(cfg, dt) else "young"

    return {"service": service, "key": key,
            "k": {dt: cfg.baseline.sigma_multiplier if len(picked[dt]) >= full_after(cfg, dt)
                  else cfg.baseline.young_sigma_multiplier for dt in DAY_CAP},
            "days": rows, "coverage": coverage, "profiles": {dt: profile(dt) for dt in DAY_CAP}}

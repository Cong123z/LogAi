"""Review page data: the days x hours matrix of one key against its baseline, and what the baseline is built from."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from logai2.config import AppConfig
from logai2.links import ALERTING_SOURCES
from logai2.owner.baseline import DAY_CAP, Baseline, Stat, full_after, judge
from logai2.owner.counts import ERROR_LEVELS
from logai2.review import keys_by_hour, minutes_by_hour, periods
from logai2.timeutil import TZ, daytype

SERVICE_KEYS = ("*total", "*error")  # service-wide keys: they sum a whole service's templates
SIGMA_FLOOR = 1.0                    # events/min: a flat key (σ 0) still needs a scale


class UnknownKey(Exception):
    pass


def alerts_low(key: str, lk, cfg: AppConfig) -> bool:
    """Whether a count below the band is an alert: exactly the detector's drop rule (owner/baseline.py judge)."""
    return (key == "*total" and not lk.upper_only and lk.days >= full_after(cfg, lk.daytype)
            and lk.this is not None and lk.this.mean >= cfg.drop.min_mean)


def resolve_key(conn, service: str, key: str) -> tuple[str, list[str]]:
    """key -> (kind, template ids whose counts it sums)."""
    if key in SERVICE_KEYS:
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


def _lookup(bl: Baseline, cfg: AppConfig, service: str, template_ids: list[str], kind: str, day: date, hour: int):
    key = "doc" if kind == "doc" else template_ids[0]
    lk = bl.lookup_doc(service, template_ids, day, hour) if kind == "doc" else bl.lookup(service, key, day, hour)
    return lk, judge(0, key, "INFO", lk, cfg)     # count 0: only the band is used


def _cells(conn, cfg: AppConfig, service: str, template_ids: list[str], kind: str,
           days: list[date], bl: Baseline | None = None) -> dict[tuple[date, int], dict]:
    """Per (day, hour) with minutes read: rate, mean, std, band_low, band_high, z, no_baseline, cls."""
    bl = bl or Baseline.load(conn, [(service, t) for t in template_ids])
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
        lk, v = _lookup(bl, cfg, service, template_ids, kind, day, hour)
        mean, std = (lk.this.mean, lk.this.std) if lk.this else (None, None)
        none = lk.this is None or (mean == 0 and std == 0)
        z, cls = None, "none"
        if lk.this is not None and none:       # a zero baseline: any event is the top step
            cls = "up3" if rate > 0 else "in"
        if not none:
            z = (rate - mean) / (max(std, SIGMA_FLOOR) if mean > 0 else std)
            full = lk.days >= full_after(cfg, lk.daytype)
            k = cfg.baseline.sigma_multiplier if full else cfg.baseline.young_sigma_multiplier
            cls = _cls(z, k, alerts_low(key, lk, cfg))
        out[(day, hour)] = {"rate": rate, "mean": mean, "std": std, "band_low": v.band_low,
                            "band_high": v.band_high, "z": z, "no_baseline": none, "cls": cls,
                            "upper_only": lk.upper_only, "low_alerts": alerts_low(key, lk, cfg)}
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


def inspect(conn, cfg: AppConfig, service: str, key: str, day: date, hour_from: int, hour_to: int,
            now: float) -> dict:
    """The inspector card of one selection: sentence, the day against its band, contributors, keys, left days."""
    kind, ids = resolve_key(conn, service, key)
    cells = _cells(conn, cfg, service, ids, kind, [day, day - timedelta(days=7)])
    rng = range(hour_from, hour_to)
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    t0, t1 = (start + timedelta(hours=hour_from)).timestamp(), (start + timedelta(hours=hour_to)).timestamp()

    changes = conn.execute("SELECT key, at, to_state FROM alert_changes WHERE service = %s ORDER BY at",
                           (service,)).fetchall()
    ps = periods(changes, now)
    not_normal, keys = minutes_by_hour(ps), keys_by_hour(ps)
    opened = {(c["key"], c["at"].timestamp()): c["to_state"] for c in changes}

    def minutes(h: int, only: str | None = None) -> int:
        if only is None:
            return not_normal.get((day, h), 0)
        return minutes_by_hour([p for p in ps if p[0] == only]).get((day, h), 0)

    bl_key = Baseline.load(conn, [(service, t) for t in ids])
    series = []
    for h in range(24):
        c, lw = cells.get((day, h)), cells.get((day - timedelta(days=7), h))
        band = c or (lambda v: {"band_low": v.band_low, "band_high": v.band_high})(
            _lookup(bl_key, cfg, service, ids, kind, day, h)[1])     # a baseline hour without a row still has its band
        series.append({"hour": h, "rate": c and c["rate"], "band_low": band["band_low"],
                       "band_high": band["band_high"], "last_week": lw and lw["rate"],
                       "not_normal_minutes": minutes(h)})
    seen = [(h, cells[(day, h)]) for h in rng if (day, h) in cells]
    peak = max(seen, key=lambda hc: hc[1]["rate"], default=None)
    decision = conn.execute(
        "SELECT decision FROM review_exclusions WHERE service = %s AND day = %s AND hour_from < %s AND hour_to > %s "
        "ORDER BY id DESC LIMIT 1", (service, day, hour_to, hour_from)).fetchone()
    in_range = {k for h in rng for k in keys.get((day, h), ())}
    sentence = {"keys": len(in_range), "not_normal_minutes": sum(minutes(h) for h in rng),
                "peak": peak and {"hour": peak[0], "rate": peak[1]["rate"], "z": peak[1]["z"]},
                "expected": peak and {"mean": peak[1]["mean"], "std": peak[1]["std"]},
                "in_baseline": day in picked_days(conn, service)[daytype(day)],
                "decision": decision and decision["decision"]}

    # contributors: every template of the service over the selected hours
    mins = {r["hour"]: r["n_minutes"] for r in conn.execute(
        "SELECT hour, n_minutes FROM service_hours WHERE service = %s AND day = %s AND hour = ANY(%s) "
        "AND n_minutes > 0", (service, day, list(rng)))}
    sums = {}
    for r in conn.execute("SELECT key, hour, sum FROM hourly_counts WHERE service = %s AND day = %s "
                          "AND hour = ANY(%s) AND key IN (SELECT template_id FROM templates WHERE service = %s)",
                          (service, day, list(mins), service)):
        sums.setdefault(r["key"], {})[r["hour"]] = r["sum"]
    tpl = {r["template_id"]: r for r in conn.execute(
        "SELECT t.template_id, t.text, t.level, t.first_seen, l.doc_id, d.title AS doc_title FROM templates t "
        "LEFT JOIN template_doc_links l USING (service, template_id) LEFT JOIN documents d USING (doc_id) "
        "WHERE t.service = %s", (service,))}
    bl = Baseline.load(conn, [(service, t) for t in sums])
    n_all = sum(mins.values())
    rows = []
    for tid, by_hour in sums.items():
        rate = sum(by_hour.values()) / n_all
        mean = sum((bl.lookup(service, tid, day, h).this or Stat(0, 0)).mean for h in mins) / len(mins)
        rows.append({"template_id": tid, "text": tpl[tid]["text"], "level": tpl[tid]["level"], "rate": rate,
                     "mean": mean, "excess": rate - mean, "times_normal": round(rate / mean, 1) if mean > 0 else None,
                     "not_normal_minutes": sum(minutes(h, tid) for h in rng),
                     "doc_id": tpl[tid]["doc_id"], "doc_title": tpl[tid]["doc_title"]})
    new = [{"template_id": t, "text": r["text"], "level": r["level"], "first_seen": r["first_seen"].timestamp(),
            "rate": sum(sums.get(t, {}).values()) / n_all if n_all else 0.0, "doc_id": r["doc_id"]}
           for t, r in tpl.items() if t0 - 3600 <= r["first_seen"].timestamp() < t1]
    fresh = {n["template_id"] for n in new}      # new templates have no baseline: listed once, under "new"
    increased = sorted((r for r in rows if r["excess"] > 0 and r["template_id"] not in fresh),
                       key=lambda r: -r["excess"])[:10]

    by_key: dict[str, list[dict]] = {}
    for k, a, b in ps:
        if a < t1 and b > t0:
            by_key.setdefault(k, []).append({"from": a, "to": b, "state": opened.get((k, a), "ALERTING")})
    return {"sentence": sentence, "day": series, "increased": increased, "new": new,
            "keys": [{"key": k, "periods": v} for k, v in sorted(by_key.items())],
            "left_days": left_days(conn, service, day, hour_from, hour_to, True)}


def series(conn, cfg: AppConfig, service: str, key: str, ndays: int, today: date, now: float) -> dict:
    """The key's hourly rate against its band over ndays, with its possible incidents (runs of hours outside the band)."""
    kind, ids = resolve_key(conn, service, key)
    days = [today - timedelta(days=i) for i in range(ndays - 1, -1, -1)]
    cells = _cells(conn, cfg, service, ids, kind, days + [d - timedelta(days=7) for d in days])
    bl = Baseline.load(conn, [(service, t) for t in ids])
    last_hour = datetime.fromtimestamp(now, TZ).hour
    pts = []
    for d in days:
        for h in range(last_hour + 1 if d == today else 24):
            c, lw = cells.get((d, h)), cells.get((d - timedelta(days=7), h))
            if c is None:       # no service_hours row: no rate, but the hour still has its band
                lk, v = _lookup(bl, cfg, service, ids, kind, d, h)
                c = {"rate": None, "mean": lk.this and lk.this.mean, "std": lk.this and lk.this.std,
                     "band_low": v.band_low, "band_high": v.band_high, "z": None, "low_alerts": alerts_low(key, lk, cfg)}
            rate, lo, hi = c["rate"], c["band_low"], c["band_high"]
            out = rate is not None and hi is not None and (rate > hi or (c["low_alerts"] and rate < lo))
            pts.append({"day": d.isoformat(), "hour": h, "rate": rate, "mean": c["mean"], "std": c["std"],
                        "band_low": lo, "band_high": hi, "last_week": lw and lw["rate"], "z": c["z"], "out": out})

    changes = conn.execute("SELECT key, at, to_state FROM alert_changes WHERE service = %s ORDER BY at",
                           (service,)).fetchall()
    not_normal = minutes_by_hour(periods(changes, now))
    decisions = conn.execute("SELECT day, hour_from, hour_to, decision FROM review_exclusions "
                             "WHERE service = %s AND day = ANY(%s) ORDER BY id DESC", (service, days)).fetchall()
    newest = max(((p["day"], p["hour"]) for p in pts if p["rate"] is not None), default=None)
    runs: list[list[dict]] = []
    for p in pts:
        if not p["out"]:
            continue
        if runs and runs[-1][-1]["day"] == p["day"] and runs[-1][-1]["hour"] == p["hour"] - 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    incidents = []
    for run in runs:
        d, a, b = date.fromisoformat(run[0]["day"]), run[0]["hour"], run[-1]["hour"] + 1
        pk = max(run, key=lambda p: abs(p["rate"] - (p["mean"] or 0)))
        decided = next((r["decision"] for r in decisions if r["day"] == d and r["hour_from"] < b and r["hour_to"] > a), None)
        suggested = any(not_normal.get((d, h), 0) for h in range(a, b))
        incidents.append({
            "day": run[0]["day"], "hour_from": a, "hour_to": b, "hours": b - a,
            "ongoing": newest is not None and (run[0]["day"], run[-1]["hour"]) == newest,
            "peak": {k: pk[k] for k in ("hour", "rate", "mean", "band_low", "band_high", "z")},
            "times_normal": round(pk["rate"] / pk["mean"], 1) if pk["mean"] else None,
            "decision": decided or ("review" if suggested else None)})

    prof: dict[str, list[dict]] = {"weekday": [], "weekend": []}
    if kind != "doc":
        for dt in prof:
            k = cfg.baseline.sigma_multiplier if bl.days.get((service, dt), 0) >= full_after(cfg, dt) \
                else cfg.baseline.young_sigma_multiplier
            prof[dt] = [{"hour": h, "mean": s.mean, "band_low": s.mean - k * s.std, "band_high": s.mean + k * s.std}
                        for h in range(24) if (s := bl.stats.get((service, ids[0], dt, h)))]
    lk = bl.lookup_doc(service, ids, today, last_hour) if kind == "doc" else bl.lookup(service, ids[0], today, last_hour)
    state = ("none" if not any(k[:2] == (service, t) for k in bl.stats for t in ids) else
             "borrowed" if lk.upper_only else "full" if lk.days >= full_after(cfg, lk.daytype) else "young")
    return {"key": key, "baseline_state": state, "points": pts, "incidents": incidents,
            "profile": {**prof, "today": [{"hour": p["hour"], "rate": p["rate"]} for p in pts
                                          if p["day"] == today.isoformat() and p["rate"] is not None]}}


def incident(conn, service: str, key: str, day: date, hour_from: int, hour_to: int, now: float) -> dict:
    """Modules the lines came from, and (ongoing only) the newest sample lines."""
    kind, ids = resolve_key(conn, service, key)
    if key in SERVICE_KEYS:
        ids = [r["template_id"] for r in conn.execute("SELECT template_id FROM templates WHERE service = %s", (service,))]
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    t0, t1 = start + timedelta(hours=hour_from), start + timedelta(hours=hour_to)
    modules = [{"module": r["module_code"], "lines": r["n"]} for r in conn.execute(
        "SELECT module_code, sum(count)::int AS n FROM alert_minutes WHERE service = %s AND key = ANY(%s) "
        "AND day = %s AND minute >= %s AND minute < %s GROUP BY module_code ORDER BY n DESC, module_code",
        (service, ids, day, t0, t1))]
    newest = conn.execute("SELECT day, hour FROM service_hours WHERE service = %s AND n_minutes > 0 "
                          "ORDER BY day DESC, hour DESC LIMIT 1", (service,)).fetchone()
    ongoing = bool(newest and newest["day"] == day and hour_from <= newest["hour"] < hour_to)
    samples: list[dict] = []
    if ongoing:
        tids = ids
        if key in SERVICE_KEYS:        # the 3 ERROR+ templates that rose most over their baseline
            tids = _top_excess(conn, service, day, hour_from, hour_to)
        samples = [{"at": r["at"].timestamp(), "template_id": r["template_id"], "line": r["line"]} for r in conn.execute(
            "SELECT template_id, at, line FROM error_samples WHERE service = %s AND template_id = ANY(%s) "
            "ORDER BY at DESC LIMIT 10", (service, tids))]
    return {"ongoing": ongoing, "modules": modules, "samples": samples}


def _top_excess(conn, service: str, day: date, hour_from: int, hour_to: int) -> list[str]:
    mins = {r["hour"]: r["n_minutes"] for r in conn.execute(
        "SELECT hour, n_minutes FROM service_hours WHERE service = %s AND day = %s AND hour >= %s AND hour < %s "
        "AND n_minutes > 0", (service, day, hour_from, hour_to))}
    if not mins:
        return []
    sums: dict[str, int] = {}
    for r in conn.execute("SELECT c.key, sum(c.sum)::float AS s FROM hourly_counts c JOIN templates t "
                          "ON t.service = c.service AND t.template_id = c.key WHERE c.service = %s AND c.day = %s "
                          "AND c.hour = ANY(%s) AND t.level = ANY(%s) GROUP BY c.key",
                          (service, day, list(mins), list(ERROR_LEVELS))):
        sums[r["key"]] = r["s"]
    bl = Baseline.load(conn, [(service, t) for t in sums])
    n = sum(mins.values())
    excess = {t: s / n - sum((bl.lookup(service, t, day, h).this or Stat(0, 0)).mean for h in mins) / len(mins)
              for t, s in sums.items()}
    return sorted(excess, key=lambda t: -excess[t])[:3]

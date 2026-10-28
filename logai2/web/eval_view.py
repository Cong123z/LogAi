"""Evaluation page data (lab_* tables only): runs, scoreboard, drawn series, knob sweeps, calibration choices."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from logai2.timeutil import TZ

KINDS = tuple(f"M{i}" for i in range(1, 8))
SCORE_FIELDS = ("ratio_vs_sigma", "ci90", "gates", "coverage", "interval_score", "sharpness", "mase", "alert_rate",
                "flap_share")
MAX_DAYS = 7


class BadRequest(ValueError):
    pass


class NoRun(LookupError):
    pass


def num(v) -> float | None:
    """NaN/inf/None -> None so the JSON stays valid."""
    return None if v is None or v != v or v in (float("inf"), float("-inf")) else float(v)


def get_run(conn, raw) -> dict:
    if raw is None or raw == "":
        raise BadRequest("run is required")
    try:
        run = conn.execute("SELECT * FROM lab_runs WHERE id = %s", (int(raw),)).fetchone()
    except (ValueError, OverflowError):
        run = None
    if run is None:
        raise NoRun("Unknown run")
    return run


def order(run: dict, names) -> list[str]:
    """sigma first, then the run's method order, then anything else."""
    ms = run["params"].get("methods", [])
    rank = {m: i for i, m in enumerate(["sigma"] + [m for m in ms if m != "sigma"])}
    return sorted(set(names), key=lambda m: (rank.get(m, len(rank)), m))


def runs(conn, iso) -> list[dict]:
    out = []
    for r in conn.execute("SELECT r.*, (SELECT strength FROM lab_results s WHERE s.run_id = r.id AND s.method = '_decision' "
                          "AND s.strength IS NOT NULL LIMIT 1) AS strength FROM lab_runs r ORDER BY id DESC LIMIT 20"):
        out.append({"id": r["id"], "site": r["site"], "test": r["test"], "status": r["status"],
                    "started_at": iso(r["started_at"].timestamp()),
                    "finished_at": iso(r["finished_at"].timestamp()) if r["finished_at"] else None,
                    "window_from": r["window_from"].isoformat(), "window_to": r["window_to"].isoformat(),
                    "tune_to": r["tune_to"].isoformat(), "data_schema": r["data_schema"],
                    "code_version": r["code_version"], "strength": r["strength"], "kinds": r["kinds"], "error": r["error"]})
    return out


def scoreboard(conn, run: dict) -> dict:
    out: dict[str, dict] = {}
    rows = conn.execute("SELECT method, kind, knob, status, metrics, verdict, strength FROM lab_results "
                        "WHERE run_id = %s AND scope = 'all'", (run["id"],)).fetchall()
    for kind in sorted({r["kind"] for r in rows}):
        mine = [r for r in rows if r["kind"] == kind]
        dec = next((r for r in mine if r["method"] == "_decision"), None)
        methods = []
        by = {r["method"]: r for r in mine if r["method"] != "_decision"}
        for m in order(run, by):
            r = by[m]
            row = {"method": m, "knob": r["knob"], "status": r["status"], "verdict": r["verdict"]}
            if run["test"] in ("A", "C"):
                row.update(recall=r["metrics"].get("recall"), delay=r["metrics"].get("delay"))
            else:
                row.update({f: r["metrics"].get(f) for f in SCORE_FIELDS})
            methods.append(row)
        d = dec and {"verdict": dec["verdict"], "strength": dec["strength"], **{
            k: dec["metrics"].get(k) for k in ("leader", "tied", "note", "text", "stability", "sigma_knob", "test_days")}}
        out[kind] = {"decision": d, "methods": methods}
    return out


def key_kind(service_key: str, mean: float) -> str:
    """Runs before bk13 stored no per-key kind; rebuild scripts/lab/kinds.assign from the key name and the drawn mean.
    ponytail: M3 vs M4 uses the mean of the drawn days (not the tune-window busy mean); M6 keys read as M4."""
    if service_key == "*total":
        return "M1"
    if service_key == "*error":
        return "M2"
    if service_key == "*mix":
        return "M7"
    if service_key.startswith("doc:"):
        return "M5"
    return "M3" if mean >= 2 else "M4"


def check_kind(kind) -> str:
    if kind not in KINDS:
        raise BadRequest("kind must be one of M1 to M7")
    return kind


def keys(conn, run: dict, kind: str) -> list[dict]:
    check_kind(kind)
    stored = {(r["method"], r["service"], k) for r in conn.execute(
        "SELECT method, scope AS service, metrics FROM lab_results WHERE run_id = %s AND kind = %s AND scope <> 'all' "
        "AND metrics ? 'drawn'", (run["id"], kind)) for k, kd in r["metrics"]["drawn"].items() if kd == kind}
    new = conn.execute("SELECT 1 FROM lab_results WHERE run_id = %s AND metrics ? 'drawn' LIMIT 1", (run["id"],)).fetchone()
    out: dict[tuple, dict] = {}
    for r in conn.execute("SELECT method, service, key, avg((SELECT avg(c) FROM unnest(counts) c)) AS mean "
                          "FROM lab_bands WHERE run_id = %s GROUP BY method, service, key", (run["id"],)):
        if new and (r["method"], r["service"], r["key"]) not in stored:
            continue
        k = out.setdefault((r["service"], r["key"]), {"mean": r["mean"] or 0.0, "methods": []})
        k["methods"].append(r["method"])
    res = [{"service": s, "key": k, "kind": kind if new else key_kind(k, v["mean"]), "methods": order(run, v["methods"])}
           for (s, k), v in sorted(out.items())]
    return [x for x in res if x["kind"] == kind]


def chosen_knob(conn, run: dict, method: str, kind: str) -> float | None:
    r = conn.execute("SELECT knob FROM lab_results WHERE run_id = %s AND method = %s AND kind = %s AND scope = 'all' "
                     "AND knob IS NOT NULL", (run["id"], method, kind)).fetchone()
    return r["knob"] if r else None


def day_start(d: date) -> int:
    return int(datetime.combine(d, time(), TZ).timestamp() // 60)


def series(conn, run: dict, iso, kind: str, service: str, key: str, methods: list[str] | None, knob: float | None,
           d_from: date, d_to: date) -> dict:
    check_kind(kind)
    if d_to < d_from or (d_to - d_from).days + 1 > MAX_DAYS:
        raise BadRequest(f"the window must be 1 to {MAX_DAYS} days")
    grids = run["params"].get("knobs", {})
    out = {}
    for m in methods or order(run, grids):
        k = knob if knob is not None else chosen_knob(conn, run, m, kind)
        rows = []
        for cand in ([k] if k is not None else grids.get(m, [])):   # no chosen knob: the first grid knob with bands
            rows = conn.execute("SELECT day, step, counts, expected, low, high, flags FROM lab_bands WHERE run_id = %s "
                                "AND method = %s AND knob = %s AND service = %s AND key = %s AND day BETWEEN %s AND %s "
                                "ORDER BY day", (run["id"], m, cand, service, key, d_from, d_to)).fetchall()
            if rows:
                k = cand
                break
        if not rows:
            continue
        pts = []
        for r in rows:
            t0 = day_start(r["day"])
            for i, c in enumerate(r["counts"]):
                at = lambda a: num(a[i]) if a else None
                pts.append({"t": iso((t0 + i * r["step"]) * 60), "count": c, "expected": at(r["expected"]),
                            "low": at(r["low"]), "high": at(r["high"]), "flag": r["flags"][i]})
        lo, hi = day_start(d_from), day_start(d_to + timedelta(days=1))
        eps = [{"start_minute": e["start_minute"], "end_minute": e["end_minute"], "start": iso(e["start_minute"] * 60),
                "end": iso(e["end_minute"] * 60), "peak_score": num(e["peak_score"]),
                "alerted_minutes": e["alerted_minutes"]}
               for e in conn.execute("SELECT * FROM lab_episodes WHERE run_id = %s AND method = %s AND knob = %s "
                                     "AND service = %s AND key = %s AND start_minute >= %s AND start_minute < %s "
                                     "ORDER BY start_minute", (run["id"], m, k, service, key, lo, hi))]
        out[m] = {"knob": k, "points": pts, "episodes": eps}
    return out


def sweep(conn, run: dict, kind: str) -> dict:
    check_kind(kind)
    chosen = {r["method"]: r["knob"] for r in conn.execute(
        "SELECT method, knob FROM lab_results WHERE run_id = %s AND kind = %s AND scope = 'all'", (run["id"], kind))}
    by: dict[str, dict[float, dict]] = {}
    for r in conn.execute("SELECT method, knob, part, metrics FROM lab_sweeps WHERE run_id = %s AND kind = %s",
                          (run["id"], kind)):
        by.setdefault(r["method"], {}).setdefault(r["knob"], {})[r["part"]] = r["metrics"]
    return {m: [{"knob": k, "chosen": chosen.get(m) == k, "tune": p.get("tune"), "test": p.get("test")}
                for k, p in sorted(ks.items())] for m, ks in ((m, by[m]) for m in order(run, by))}


def choices(conn, iso) -> list[dict]:
    return [{"kind": r["kind"], "method": r["method"], "knob": r["knob"], "run_id": r["run_id"], "note": r["note"],
             "chosen_at": iso(r["chosen_at"].timestamp())}
            for r in conn.execute("SELECT * FROM lab_choices ORDER BY kind")]


def choose(conn, payload: dict[str, Any]) -> dict:
    kind, method, note = payload.get("kind"), payload.get("method"), payload.get("note")
    check_kind(kind)
    run = get_run(conn, payload.get("run_id"))
    grid = run["params"].get("knobs", {}).get(method) if isinstance(method, str) else None
    if grid is None:
        raise BadRequest("method is not known in that run")
    if not conn.execute("SELECT 1 FROM lab_results WHERE run_id = %s AND method = %s AND kind = %s AND scope = 'all'",
                        (run["id"], method, kind)).fetchone():
        raise BadRequest("that run has no result for this method and kind")
    knob = payload.get("knob")
    if isinstance(knob, bool) or not isinstance(knob, (int, float)) or float(knob) not in [float(g) for g in grid]:
        raise BadRequest("knob is not in the method's grid")
    if note is not None and not isinstance(note, str):
        raise BadRequest("note must be text")
    if note and len(note) > 500:
        raise BadRequest("note must be at most 500 characters")
    conn.execute("INSERT INTO lab_choices (kind, method, knob, run_id, note) VALUES (%s, %s, %s, %s, %s) "
                 "ON CONFLICT (kind) DO UPDATE SET method = EXCLUDED.method, knob = EXCLUDED.knob, "
                 "run_id = EXCLUDED.run_id, note = EXCLUDED.note, chosen_at = now()",
                 (kind, method, float(knob), run["id"], note))
    conn.commit()
    return {"kind": kind, "method": method, "knob": float(knob), "run_id": run["id"], "note": note}

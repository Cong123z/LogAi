"""The finish hook: tune every method, write the sweeps, compare with sigma, apply the gates and decide per kind
(judging J4, J6, J7; spec 9). Reads the per-service lab_results rows the scoring sink wrote; writes the `scope = 'all'` rows,
the `_decision` rows and lab_sweeps.

Scope: Test B only (`main` passes the hook only for `--test B`; Tests A and C get their own finish). M6 and M7 get
no ranking here: sigma has no band for new templates (M6) and does not judge the mix (M7), so interval score against sigma
means nothing; their `_decision` row comes from the Test A recall (`_detect_decision`), `pending` when no Test A run of this
code_version exists. Score-only methods get no
interval score, so they are never adopt candidates for M1-M5 (they feed E1).

Test A recall is read from the latest ok `lab_runs` row with test = 'A' and this run's code_version: its
lab_results row (method, kind, scope 'all') has metrics {"recall": {"medium": <0..1>, ...}} (the shape bk-test-a writes)."""
from __future__ import annotations

from dataclasses import dataclass, field

import scripts.lab.methods as methods
from scripts.lab import store
from scripts.lab.bootstrap import ci90, point_ratio, sum_days
from scripts.lab.tune import BUDGET, aggregate, tune

SIGMA, DECISION = "sigma", "_decision"
COVERAGE_BAND = (0.990, 0.999)
MAX_FLAP = 0.10
RECALL_SLACK = 0.05
WORSE_SHARE = 0.20
FIT_RATIO = 0.90
BAND_KINDS = {"M1", "M2", "M3", "M4", "M5"}


def strength_of(window_days: int, test_weekends: int) -> str:
    if window_days < 14:
        return "exploratory"
    return "final" if window_days >= 28 and test_weekends >= 2 else "provisional"


def fits_better(ratio, ci) -> bool:
    return ratio is not None and ci is not None and ratio <= FIT_RATIO and ci[1] < 1.0


def method_verdict(gates: dict) -> str:
    return "not suitable" if "fail" in gates.values() else "ok"


@dataclass
class Cand:
    name: str
    cost: int
    heavy: bool
    ratio: float | None
    ci: list | None                      # ci90 of the ratio
    mase: float | None
    gates: dict
    worse_share: float | None = None     # share of services where it is worse than sigma
    extra: dict = field(default_factory=dict)


def _overlap(a, b) -> bool:
    return a is not None and b is not None and a[0] <= b[1] and b[0] <= a[1]


def judge_kind(cands: list[Cand]) -> dict:
    """The decision tree for one kind over the non-sigma methods. -> {verdict, leader, tied, note}."""
    inf = float("inf")
    live = sorted((c for c in cands if "fail" not in c.gates.values()),
                  key=lambda c: (inf if c.ratio is None else c.ratio, c.name))
    if not live:
        return {"verdict": "sigma stays", "leader": None, "tied": [], "note": "no method passed the gates"}
    top = live[0]
    tied = [c for c in live if c is top or _overlap(c.ci, top.ci)]
    lead = min(tied, key=lambda c: (c.heavy, c.cost, inf if c.mase is None else c.mase, c.name))
    out = {"leader": lead.name, "tied": sorted(c.name for c in tied)}
    if not fits_better(lead.ratio, lead.ci):
        why = f"{lead.name} does not fit better than sigma"
        if lead is not top and fits_better(top.ratio, top.ci):
            why += f" ({top.name} does, but ties go to the simpler {lead.name})"
        return {**out, "verdict": "sigma stays", "note": why}
    if lead.worse_share is not None and lead.worse_share > WORSE_SHARE:
        return {**out, "verdict": "mixed",
                "note": f"{lead.name} is worse than sigma on {lead.worse_share:.0%} of services"}
    if any(v != "pass" for v in lead.gates.values()):
        todo = sorted(g for g, v in lead.gates.items() if v != "pass")
        return {**out, "verdict": "sigma stays", "note": f"{lead.name} fits better but is not verified: {', '.join(todo)}"}
    return {**out, "verdict": "adopt candidate", "note": f"{lead.name} fits better than sigma and passed every gate"}


def _spec(name):
    cls = methods.METHODS.get(name)
    if cls is None:
        return None
    try:
        return getattr(cls, "spec", None) or cls().spec
    except methods.Unavailable:
        return None


def _recalls(conn, run_id):
    """{(method, kind): medium recall} of the latest ok Test A run with this run's code version, or None."""
    row = conn.execute("SELECT id FROM lab_runs WHERE test = 'A' AND status = 'ok' AND id <> %s AND code_version = "
                       "(SELECT code_version FROM lab_runs WHERE id = %s) ORDER BY id DESC LIMIT 1",
                       (run_id, run_id)).fetchone()
    if row is None:
        return None
    return {(r["method"], r["kind"]): (r["metrics"].get("recall") or {}).get("medium")
            for r in conn.execute("SELECT method, kind, metrics FROM lab_results WHERE run_id = %s AND scope = 'all'",
                                  (row["id"],))}


def _gates(spec, kind, test, in_budget, recalls, method, sigma_name) -> dict:
    cov, rate, flap = test["coverage"], test["alert_rate"], test["flap_share"]
    g = {"band": "pass" if spec.band or kind not in BAND_KINDS else "fail",
         "cost": "pass",                       # ponytail: no cost budget yet (spec cost is a constant); wire when one exists
         "coverage": "not_checked" if cov is None else "pass" if COVERAGE_BAND[0] <= cov <= COVERAGE_BAND[1] else "fail",
         "alert_rate": "pass" if rate is not None and rate <= BUDGET and in_budget else "fail",
         "flaps": "fail" if flap is not None and flap > MAX_FLAP else "pass"}
    mine, base = (recalls or {}).get((method, kind)), (recalls or {}).get((sigma_name, kind))
    g["detect"] = ("not_checked" if mine is None or base is None
                   else "pass" if mine >= base - RECALL_SLACK else "fail")
    return g


def _test_weekends(days) -> int:
    from datetime import date
    return len({(d.isocalendar()[0], d.isocalendar()[1]) for d in map(date.fromisoformat, days) if d.weekday() >= 5})


def finish_all(results_conn, run_id, *, seed: int, cfg) -> None:
    conn = results_conn
    run = conn.execute("SELECT * FROM lab_runs WHERE id = %s", (run_id,)).fetchone()
    window = (run["window_to"] - run["window_from"]).days + 1
    groups: dict[tuple, list] = {}
    for r in conn.execute("SELECT method, kind, scope, status, metrics FROM lab_results "
                          "WHERE run_id = %s AND scope <> 'all' ORDER BY scope", (run_id,)):
        groups.setdefault((r["method"], r["kind"]), []).append(r)
    recalls = _recalls(conn, run_id)
    prev = conn.execute("SELECT id FROM lab_runs WHERE site = %s AND test = %s AND status = 'ok' AND id < %s "
                        "ORDER BY id DESC LIMIT 1", (run["site"], run["test"], run_id)).fetchone()

    info: dict[tuple, dict] = {}                       # (method, kind) -> aggregated facts
    for (m, kd), rows in groups.items():
        spec, ok = _spec(m), [r for r in groups[m, kd] if r["status"] == "ok"]
        if spec is None:
            continue
        if not ok:
            info[m, kd] = {"spec": spec, "status": "failed" if any(r["status"] == "failed" for r in rows) else "skipped"}
            continue
        key = {float(k): k for r in ok for k in r["metrics"]["knobs"]}      # float -> the key as stored ("3" or "3.0")
        knobs = sorted(key)
        sweep = {k: {p: aggregate([r["metrics"]["knobs"][key[k]][p] for r in ok if key[k] in r["metrics"]["knobs"]], kd)
                     for p in ("tune", "test")} for k in knobs}
        days = {}
        for k in knobs:
            days[k] = sum_days([r["metrics"]["days"].get(key[k], {}) for r in ok])
            d = days[k].values()
            ab = sum(x["abs_naive"] for x in d)
            sweep[k]["test"]["mase"] = sum(x["abs_err"] for x in d) / ab if ab > 0 else None
        knob, in_budget = tune(spec.band, {k: v["tune"] for k, v in sweep.items()})
        info[m, kd] = {"spec": spec, "status": "ok" if knob is not None else "skipped", "ok": ok, "sweep": sweep,
                       "days": days, "key": key, "knob": knob, "in_budget": in_budget}

    conn.commit()           # the store's transactions must be the outermost ones (the runner's connection is not autocommit)
    with conn.transaction():
        for (m, kd), i in sorted(info.items()):
            for k, parts in i.get("sweep", {}).items():
                for p, met in parts.items():
                    store.put_sweep(conn, run_id, m, kd, k, p, met)
        for kd in sorted({kd for _, kd in info}):
            _decide_kind(conn, run_id, run, kd, window, info, recalls, prev, seed)


def _service_ratios(i, sig) -> list[float]:
    """Per service: test interval score of the method at its knob / sigma's at its knob."""
    def per(x):
        return {r["scope"]: r["metrics"]["knobs"].get(x["key"][x["knob"]], {}).get("test", {}).get("interval_score")
                for r in x["ok"]}
    mine, base = per(i), per(sig)
    return [mine[s] / base[s] for s in mine if mine.get(s) is not None and base.get(s)]


RECALL_GAIN, MIX_RECALL = 0.10, 0.5


def _detect_decision(kd, cands: list[Cand], recalls) -> dict:
    """M6 / M7 on Test A recall (sigma's interval score means nothing for them). Pending without a Test A run.
    M6: leader = highest medium burst recall among methods passing the alert-rate and flap gates; adopt candidate if it beats
    sigma's recall by RECALL_GAIN (no sigma recall: sigma stays). M7: sigma does not judge the mix; adopt candidate if the leader
    (no failed gate) has medium mix_swap recall >= MIX_RECALL, else no method. The ranking fields stay empty when nobody leads."""
    if recalls is None:
        return {"verdict": "pending", "leader": None, "tied": [], "note": "decided on Test A detection (bk10)"}
    no = "sigma stays" if kd == "M6" else "no method"
    live = [(recalls.get((c.name, kd)), c) for c in cands
            if (c.gates["alert_rate"] == "pass" and c.gates["flaps"] == "pass" if kd == "M6" else "fail" not in c.gates.values())]
    live = sorted(((r, c) for r, c in live if r is not None), key=lambda x: (-x[0], x[1].heavy, x[1].cost, x[1].name))
    if not live:
        return {"verdict": no, "leader": None, "tied": [], "note": "no method passed the gates with a Test A recall"}
    r, lead = live[0]
    if r == 0:      # bk17: a tie at zero is not a leader
        return {"verdict": no, "leader": None, "tied": [], "note": "no method detects"}
    out = {"leader": lead.name, "tied": [c.name for q, c in live if q == r]}
    if kd == "M7":
        win = r >= MIX_RECALL
        note = f"{lead.name} mix_swap recall {r:.2f} " + (f">= {MIX_RECALL}" if win else f"< {MIX_RECALL}")
    else:
        base = recalls.get((SIGMA, kd))
        win = base is not None and r >= base + RECALL_GAIN - 1e-9
        note = (f"{lead.name} burst recall {r:.2f} vs sigma {base:.2f}" if base is not None
                else f"no sigma recall to compare {lead.name} with")
    return {**out, "verdict": "adopt candidate" if win else no, "note": note}


def _decide_kind(conn, run_id, run, kd, window, info, recalls, prev, seed) -> None:
    sig = info.get((SIGMA, kd))
    sig = sig if sig and sig["status"] == "ok" else None
    ref_days = (sig or next((i for (_, k), i in sorted(info.items()) if k == kd and i["status"] == "ok"), None))
    dates = set(ref_days["days"][ref_days["knob"]]) if ref_days else set()
    strength = strength_of(window, _test_weekends(dates))
    cands = []
    for (m, k), i in sorted(info.items()):
        if k != kd:
            continue
        if i["status"] != "ok":
            store.put_result(conn, run_id, m, kd, "all", None, i["status"], {}, None, strength)
            continue
        knob, test = i["knob"], i["sweep"][i["knob"]]["test"]
        ratio = ci = worse = None
        if sig:
            ratio = point_ratio(i["days"][knob], sig["days"][sig["knob"]])
            ci = ci90(i["days"][knob], sig["days"][sig["knob"]], kd, seed)
            sr = _service_ratios(i, sig)
            worse = sum(x > 1.0 for x in sr) / len(sr) if sr and m != SIGMA else None
        gates = _gates(i["spec"], kd, test, i["in_budget"], recalls, m, SIGMA)
        verdict = method_verdict(gates)
        metrics = {**test, "ratio_vs_sigma": ratio, "ci90": ci, "gates": gates, "worse_share": worse,
                   "tune_in_budget": i["in_budget"]}
        store.put_result(conn, run_id, m, kd, "all", knob, "ok", metrics, verdict, strength)
        if m != SIGMA:
            cands.append(Cand(m, i["spec"].cost, i["spec"].heavy, ratio, ci["ratio"] if ci else None, test["mase"],
                              gates, worse))
    d = judge_kind(cands)
    if kd in ("M6", "M7"):
        d = _detect_decision(kd, cands, recalls)
    text = d["verdict"] + (f": {d['note']}" if d["note"] else "")
    if sig is None:
        text += " (no sigma result)"
    elif d["verdict"] == "sigma stays":
        sk, dk = sig["knob"], sig["spec"].default_knob
        text += f" (sigma k = {sk:g}" + (", recalibrated)" if sk != dk else ")")
    stability = "no previous run"
    if prev:
        stability = "previous run had no leader"
        old = conn.execute("SELECT metrics FROM lab_results WHERE run_id = %s AND method = %s AND kind = %s "
                           "AND scope = 'all'", (prev["id"], DECISION, kd)).fetchone()
        if old and old["metrics"].get("leader") and d["leader"]:
            same = old["metrics"]["leader"] == d["leader"] or old["metrics"]["leader"] in d["tied"]
            stability = "stable" if same else "unstable"
    store.put_result(conn, run_id, DECISION, kd, "all", None, "ok",
                     {"leader": d["leader"], "tied": d["tied"], "text": text, "note": d["note"],
                      "sigma_knob": sig["knob"] if sig else None, "stability": stability, "test_days": len(dates)},
                     d["verdict"], strength)

import random
from datetime import date, timedelta

import pytest

import scripts.lab.methods as methods
from scripts.lab import run as labrun
from scripts.lab import store
from scripts.lab.bootstrap import ci90, sum_days
from scripts.lab.decide import Cand, finish_all, judge_kind, method_verdict, strength_of
from scripts.lab.methods import Spec
from scripts.lab.tune import aggregate, tune
from tests.conftest import TEST_DSN
from test_lab_run import DAYS, seed

D0 = date(2026, 10, 1)


def M(n=1000, cov=0.995, score=10.0, eps=0, kd=10.0, flap=None, sharp=1.0, rate=None):
    return {"n_minutes": n, "coverage": cov, "interval_score": score, "sharpness": sharp, "mase": None,
            "mase_naive": None, "alert_rate": rate, "flap_share": flap, "episodes": eps, "key_days": kd}


def D(n=100, cov=99, wink=1000.0, err=5.0, naive=10.0, eps=0, kd=1.0, flaps=0):
    return {"n": n, "covered": cov, "winkler": wink, "abs_err": err, "abs_naive": naive, "episodes": eps,
            "flaps": flaps, "key_days": kd}


def metrics(knobs, days=None):
    """knobs {k: (tune M, test M)}; days {k: {date: D}}"""
    return {"n_keys": 1,
            "knobs": {str(k): {"tune": t, "test": e} for k, (t, e) in knobs.items()},
            "days": {str(k): {d.isoformat(): x for d, x in v.items()} for k, v in (days or {}).items()}}


def mkspec(name, band=True, cost=1, heavy=False, knobs=(1.0, 2.0, 3.0)):
    return Spec(name, "T", name, frozenset({"M2"}), knobs, knobs[0], band, heavy, cost)


def fake(spec):
    return type("F" + spec.name, (), {"spec": spec})


def new_run(conn, test="B", code="abc", site="laptop", days=28):
    return store.start_run(conn, site=site, test=test, code_version=code, image_tag=None, data_schema="public",
                           window_from=D0, window_to=D0 + timedelta(days - 1), tune_to=D0 + timedelta(days // 2 - 1),
                           seed=7, params={})


def put(conn, rid, method, scope, m, kind="M2"):
    store.put_result(conn, rid, method, kind, scope, None, "ok", m)


def all_rows(conn, rid):
    return {(r["method"], r["kind"]): r for r in conn.execute("SELECT * FROM lab_results WHERE run_id = %s AND scope = 'all'", (rid,))}


# 1 -------------------------------------------------------------------------------------------------------------
def test_tune_band_closest_to_995_tie_goes_wider():
    by = {1.0: {"coverage": 0.980}, 2.0: {"coverage": 0.990}, 3.0: {"coverage": 0.9}}
    assert tune(True, by) == (2.0, True)
    by = {1.0: {"coverage": 0.5}, 2.0: {"coverage": 0.995}}
    assert tune(True, by) == (2.0, True)
    assert tune(True, {1.0: {"coverage": 0.9}, 2.0: {"coverage": 0.996}}) == (2.0, True)
    assert tune(True, {1.0: {"coverage": None}}) == (None, True)


def test_tune_band_tie_is_higher_coverage():
    by = {1.0: {"coverage": 0.9875}, 2.0: {"coverage": 1.0}}             # both 0.0075 from the target (exact in binary)
    assert tune(True, by)[0] == 2.0


def test_tune_follows_the_dict_order_most_sensitive_first():
    """bk17: a tail-probability grid is listed 0.01 .. 1e-5 (sensitive first): numeric order picked the least sensitive."""
    by = {0.01: {"alert_rate": 5.0}, 0.001: {"alert_rate": 0.5}, 1e-05: {"alert_rate": 0.0}}
    assert tune(False, by) == (0.001, True)
    assert tune(False, {0.01: {"alert_rate": 5.0}, 1e-05: {"alert_rate": 3.0}}) == (1e-05, False)


def test_tune_score_most_sensitive_within_budget():
    by = {1.0: {"alert_rate": 5.0}, 2.0: {"alert_rate": 1.0}, 3.0: {"alert_rate": 0.2}}
    assert tune(False, by) == (2.0, True)
    assert tune(False, {1.0: {"alert_rate": 5.0}, 2.0: {"alert_rate": 2.0}}) == (2.0, False)   # none: least sensitive


# 2 -------------------------------------------------------------------------------------------------------------
def test_aggregate_two_services_hand_calculation():
    a = M(n=1000, cov=0.99, score=10.0, eps=2, kd=10.0, flap=0.5, sharp=2.0)
    b = M(n=3000, cov=0.998, score=20.0, eps=1, kd=30.0, flap=0.0, sharp=4.0)
    t = aggregate([a, b], "M2")
    assert t["n_minutes"] == 4000
    assert t["coverage"] == pytest.approx((990 + 2994) / 4000)
    assert t["interval_score"] == pytest.approx((10000 + 60000) / 4000)
    assert t["sharpness"] == pytest.approx((2 * 1000 + 4 * 3000) / 4000)
    assert t["episodes"] == 3 and t["key_days"] == 40.0
    assert t["alert_rate"] == pytest.approx(3 / 40 * 100)
    assert t["flap_share"] == pytest.approx(1 / 3)
    m1 = aggregate([M(eps=2, kd=14.0)], "M1")
    assert m1["alert_rate"] == pytest.approx(2 / 2.0)                    # per service-week


# 3 -------------------------------------------------------------------------------------------------------------
def spread(scale, n=20, seedv=1):
    r = random.Random(seedv)
    return {D0 + timedelta(i): D(wink=1000 * scale * r.uniform(0.5, 1.5), eps=r.choice([0, 0, 1])) for i in range(n)}


def test_sum_days_skips_empty_days():
    s = sum_days([{D0: D(n=10, kd=1.0), D0 + timedelta(1): D(n=0, kd=0.0)}, {D0: D(n=5, kd=1.0)}])
    assert list(s) == [D0] and s[D0]["n"] == 15 and s[D0]["key_days"] == 2.0


def test_bootstrap_seeded_and_identical_method_contains_one():
    m, s = sum_days([spread(0.8)]), sum_days([spread(1.0, seedv=2)])
    a, b = ci90(m, s, "M2", 7), ci90(m, s, "M2", 7)
    assert a == b and a["ratio"][0] <= a["ratio"][1] and a["mase"] and a["alert_rate"]
    assert ci90(m, s, "M2", 8) != a
    same = ci90(s, s, "M2", 7)
    assert same["ratio"][0] <= 1.0 <= same["ratio"][1]


# 4 -------------------------------------------------------------------------------------------------------------
PASS = {"band": "pass", "cost": "pass", "coverage": "pass", "alert_rate": "pass", "flaps": "pass", "detect": "pass"}


def cand(name, ratio, ci, cost=1, heavy=False, mase=0.8, gates=None, worse=0.0):
    return Cand(name, cost, heavy, ratio, ci, mase, gates or dict(PASS), worse)


def test_verdict_adopt_candidate():
    d = judge_kind([cand("a", 0.8, [0.7, 0.9]), cand("b", 1.1, [1.0, 1.2])])
    assert d["verdict"] == "adopt candidate" and d["leader"] == "a"


def test_verdict_sigma_stays_when_gain_is_small_or_ci_crosses_one():
    assert judge_kind([cand("a", 0.95, [0.9, 0.99])])["verdict"] == "sigma stays"
    d = judge_kind([cand("a", 0.85, [0.7, 1.1])])
    assert d["verdict"] == "sigma stays" and d["leader"] == "a"


def test_gate_failure_is_not_suitable_and_cannot_lead():
    bad = dict(PASS, coverage="fail")
    assert method_verdict(bad) == "not suitable" and method_verdict(PASS) == "ok"
    d = judge_kind([cand("a", 0.5, [0.4, 0.6], gates=bad), cand("b", 0.8, [0.7, 0.85])])
    assert d["leader"] == "b" and d["verdict"] == "adopt candidate"
    assert judge_kind([cand("a", 0.5, [0.4, 0.6], gates=bad)])["verdict"] == "sigma stays"


def test_tie_goes_to_cheaper_then_core_then_mase():
    ci = [0.7, 0.8]
    d = judge_kind([cand("pricey", 0.70, ci, cost=5), cand("cheap", 0.72, [0.71, 0.81], cost=1)])
    assert d["leader"] == "cheap" and d["tied"] == ["cheap", "pricey"]
    d = judge_kind([cand("heavy", 0.70, ci, heavy=True), cand("core", 0.72, ci)])
    assert d["leader"] == "core"
    d = judge_kind([cand("x", 0.70, ci, mase=0.9), cand("y", 0.72, ci, mase=0.7)])
    assert d["leader"] == "y"
    d = judge_kind([cand("far", 0.5, [0.45, 0.55], cost=9), cand("near", 0.8, [0.75, 0.85], cost=1)])
    assert d["leader"] == "far"                                           # not overlapping: no tie


def test_service_check_gives_mixed():
    d = judge_kind([cand("a", 0.8, [0.7, 0.85], worse=0.25)])
    assert d["verdict"] == "mixed"
    assert judge_kind([cand("a", 0.8, [0.7, 0.85], worse=0.20)])["verdict"] == "adopt candidate"


def test_unchecked_detect_blocks_adoption():
    d = judge_kind([cand("a", 0.8, [0.7, 0.85], gates=dict(PASS, detect="not_checked"))])
    assert d["verdict"] == "sigma stays" and "detect" in d["note"]


# 5 + db level --------------------------------------------------------------------------------------------------
def board(conn, rid, svc_scores):
    """sigma at k=1/2 plus method 'alt' at k=1/2, two services. svc_scores {(method, service): interval score}."""
    for (m, s), sc in svc_scores.items():
        kn = {k: (M(cov=cv, score=sc, rate=0.5, eps=0), M(cov=0.995, score=sc, rate=0.5, eps=0))
              for k, cv in ((1.0, 0.98), (2.0, 0.995))}
        dy = {k: {D0 + timedelta(14 + i): D(wink=sc * 100, n=100) for i in range(14)} for k in (1.0, 2.0)}
        put(conn, rid, m, s, metrics(kn, dy))


@pytest.fixture
def two(monkeypatch):
    monkeypatch.setattr(methods, "METHODS", {"sigma": fake(mkspec("sigma", cost=0)), "alt": fake(mkspec("alt"))})


def scores(alt1, alt2):
    return {("sigma", "s1"): 10.0, ("sigma", "s2"): 10.0, ("alt", "s1"): alt1, ("alt", "s2"): alt2}


def test_finish_writes_rows_sweeps_and_decision(conn, cfg, two):
    rid = new_run(conn)
    board(conn, rid, scores(5.0, 6.0))
    finish_all(conn, rid, seed=7, cfg=cfg)
    rows = all_rows(conn, rid)
    alt = rows["alt", "M2"]
    assert alt["knob"] == 2.0 and alt["status"] == "ok" and alt["strength"] == "final"
    assert alt["metrics"]["ratio_vs_sigma"] == pytest.approx(0.55)
    assert set(alt["metrics"]["ci90"]) == {"ratio", "mase", "alert_rate"}
    assert alt["metrics"]["gates"]["detect"] == "not_checked" and alt["verdict"] == "ok"
    assert rows["sigma", "M2"]["metrics"]["ratio_vs_sigma"] == pytest.approx(1.0)
    dec = rows["_decision", "M2"]
    assert dec["metrics"]["leader"] == "alt" and dec["verdict"] == "sigma stays"       # detect not checked
    sweeps = conn.execute("SELECT * FROM lab_sweeps WHERE run_id = %s", (rid,)).fetchall()
    assert len(sweeps) == 2 * 2 * 2                                                     # methods x knobs x parts
    assert conn.execute("SELECT count(*) AS n FROM lab_results WHERE run_id = %s AND scope = 's1'", (rid,)).fetchone()["n"] == 2


def test_detect_gate_pass_fail_and_adoption(conn, cfg, two):
    a = new_run(conn, test="A", code="abc")
    for m, r in (("sigma", 0.80), ("alt", 0.78)):
        store.put_result(conn, a, m, "M2", "all", 2.0, "ok", {"recall": {"medium": r}})
    store.finish_run(conn, a, "ok")
    rid = new_run(conn)
    board(conn, rid, scores(5.0, 6.0))
    finish_all(conn, rid, seed=7, cfg=cfg)
    rows = all_rows(conn, rid)
    assert rows["alt", "M2"]["metrics"]["gates"]["detect"] == "pass"
    assert rows["_decision", "M2"]["verdict"] == "adopt candidate"
    conn.execute("UPDATE lab_results SET metrics = '{\"recall\": {\"medium\": 0.5}}' WHERE run_id = %s AND method = 'alt'", (a,))
    rid2 = new_run(conn)
    board(conn, rid2, scores(5.0, 6.0))
    finish_all(conn, rid2, seed=7, cfg=cfg)
    rows = all_rows(conn, rid2)
    assert rows["alt", "M2"]["metrics"]["gates"]["detect"] == "fail" and rows["alt", "M2"]["verdict"] == "not suitable"
    assert rows["_decision", "M2"]["verdict"] == "sigma stays"


def test_detect_gate_ignores_other_code_version_or_failed_run(conn, cfg, two):
    a = new_run(conn, test="A", code="other")
    for m in ("sigma", "alt"):
        store.put_result(conn, a, m, "M2", "all", 2.0, "ok", {"recall": {"medium": 1.0}})
    store.finish_run(conn, a, "ok")
    rid = new_run(conn)
    board(conn, rid, scores(5.0, 6.0))
    finish_all(conn, rid, seed=7, cfg=cfg)
    assert all_rows(conn, rid)["alt", "M2"]["metrics"]["gates"]["detect"] == "not_checked"


def test_service_check_from_per_service_rows(conn, cfg, two):
    rid = new_run(conn)
    board(conn, rid, scores(5.0, 15.0))                  # worse than sigma on 1 of 2 services = 50 %
    finish_all(conn, rid, seed=7, cfg=cfg)
    assert all_rows(conn, rid)["alt", "M2"]["metrics"]["worse_share"] == 0.5


def test_zero_n_days_are_not_data(conn, cfg, two):
    rid = new_run(conn)
    board(conn, rid, scores(5.0, 6.0))
    empty = D(n=0, cov=0, wink=0.0, err=0.0, naive=0.0, kd=0.0)
    row = conn.execute("SELECT metrics FROM lab_results WHERE run_id = %s AND method = 'alt' AND scope = 's1'", (rid,)).fetchone()["metrics"]
    for k in row["days"]:
        row["days"][k]["2026-10-01"] = empty
    store.clear_results(conn, rid, "alt", "s1")
    put(conn, rid, "alt", "s1", row)
    finish_all(conn, rid, seed=7, cfg=cfg)
    assert all_rows(conn, rid)["_decision", "M2"]["metrics"]["test_days"] == 14


def test_stability_note(conn, cfg, two):
    r1 = new_run(conn)
    board(conn, r1, scores(5.0, 6.0))
    finish_all(conn, r1, seed=7, cfg=cfg)
    store.finish_run(conn, r1, "ok")
    r2 = new_run(conn)
    board(conn, r2, scores(5.0, 6.0))
    finish_all(conn, r2, seed=7, cfg=cfg)
    assert all_rows(conn, r2)["_decision", "M2"]["metrics"]["stability"] == "stable"
    conn.execute("UPDATE lab_results SET metrics = jsonb_set(metrics, '{leader}', '\"other\"') WHERE run_id = %s AND method = '_decision'", (r1,))
    r3 = new_run(conn)
    board(conn, r3, scores(5.0, 6.0))
    finish_all(conn, r3, seed=7, cfg=cfg)
    assert all_rows(conn, r3)["_decision", "M2"]["metrics"]["stability"] == "unstable"


def test_int_knobs_keep_stored_keys(conn, cfg, monkeypatch):
    sp = lambda n: fake(mkspec(n, knobs=(1, 2, 3)))        # noqa: E731
    monkeypatch.setattr(methods, "METHODS", {"sigma": sp("sigma"), "alt": sp("alt")})
    rid = new_run(conn)
    for m, sc in (("sigma", 10.0), ("alt", 5.0)):
        kn = {k: (M(cov=0.98 + 0.005 * k, score=sc), M(cov=0.995, score=sc)) for k in (1, 2, 3)}
        dy = {k: {D0 + timedelta(14 + i): D(wink=sc * 100) for i in range(14)} for k in (1, 2, 3)}
        put(conn, rid, m, "s1", metrics(kn, dy))
    finish_all(conn, rid, seed=7, cfg=cfg)
    alt = all_rows(conn, rid)["alt", "M2"]
    assert alt["knob"] == 3.0 and alt["metrics"]["ratio_vs_sigma"] == pytest.approx(0.5) and alt["metrics"]["worse_share"] == 0.0


def test_test_a_run_writes_no_decision_rows(conn, schema):
    seed(conn)
    argv = ["--site", "laptop", "--test", "A", "--methods", "sigma", "--from", DAYS[0].isoformat(),
            "--to", DAYS[-1].isoformat(), "--dsn", TEST_DSN, "--schema", schema, "--results-schema", schema]
    assert labrun.main(argv) == 0
    assert conn.execute("SELECT count(*) AS n FROM lab_results WHERE method = '_decision'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM lab_incidents WHERE source = 'test-a'").fetchone()["n"] > 0


def test_m6_decision_is_pending(conn, cfg, two):
    rid = new_run(conn)
    for (m, s), sc in scores(5.0, 6.0).items():
        kn = {k: (M(cov=0.995, score=sc), M(cov=0.995, score=sc)) for k in (1.0, 2.0)}
        dy = {k: {D0 + timedelta(14 + i): D(wink=sc * 100) for i in range(14)} for k in (1.0, 2.0)}
        put(conn, rid, m, s, metrics(kn, dy), kind="M6")
    finish_all(conn, rid, seed=7, cfg=cfg)
    dec = all_rows(conn, rid)["_decision", "M6"]
    assert dec["verdict"] == "pending" and "bk10" in dec["metrics"]["note"]
    assert dec["metrics"]["leader"] is None and dec["metrics"]["tied"] == []        # no ranking on a pending row


def board_kind(conn, rid, kind, eps=0, kd=10.0, flap=None):
    """sigma and alt, two services, one kind; alt's episodes, key-days and flap share (alert rate = eps * 2 / (2 * kd) * 100)."""
    for (m, s), sc in scores(5.0, 6.0).items():
        e, k_d, f = (eps, kd, flap) if m == "alt" else (0, 10.0, None)
        kn = {k: (M(cov=0.995, score=sc, eps=e, kd=k_d, flap=f), M(cov=0.995, score=sc, eps=e, kd=k_d, flap=f))
              for k in (1.0, 2.0)}
        dy = {k: {D0 + timedelta(14 + i): D(wink=sc * 100) for i in range(14)} for k in (1.0, 2.0)}
        put(conn, rid, m, s, metrics(kn, dy), kind=kind)


def put_test_a(conn, kind, recalls, code="abc"):
    a = new_run(conn, test="A", code=code)
    for m, r in recalls.items():
        store.put_result(conn, a, m, kind, "all", 2.0, "ok", {"recall": {"medium": r}})
    store.finish_run(conn, a, "ok")
    return a


def m6(conn, cfg, recalls, **kw):
    put_test_a(conn, "M6", recalls)
    rid = new_run(conn)
    board_kind(conn, rid, "M6", **kw)
    finish_all(conn, rid, seed=7, cfg=cfg)
    return all_rows(conn, rid)["_decision", "M6"]


def test_m6_adopts_when_recall_beats_sigma_by_ten_points(conn, cfg, two):
    dec = m6(conn, cfg, {"sigma": 0.50, "alt": 0.62})
    assert dec["verdict"] == "adopt candidate" and dec["metrics"]["leader"] == "alt"
    assert "0.62" in dec["metrics"]["text"]


def test_m6_sigma_stays_when_gain_is_small_or_gate_fails(conn, cfg, two):
    assert m6(conn, cfg, {"sigma": 0.50, "alt": 0.55})["verdict"] == "sigma stays"
    dec = m6(conn, cfg, {"sigma": 0.50, "alt": 0.90}, eps=1)                  # alert-rate gate fails: not a leader
    assert dec["verdict"] == "sigma stays" and dec["metrics"]["leader"] is None
    dec = m6(conn, cfg, {"sigma": 0.50, "alt": 0.90}, eps=1, kd=1000.0, flap=0.5)                  # flap gate fails
    assert dec["verdict"] == "sigma stays" and dec["metrics"]["leader"] is None


def test_m7_adopts_at_half_recall_else_no_method(conn, cfg, two):
    for r, want in ((0.5, "adopt candidate"), (0.4, "no method")):
        put_test_a(conn, "M7", {"alt": r})
        rid = new_run(conn)
        board_kind(conn, rid, "M7")
        finish_all(conn, rid, seed=7, cfg=cfg)
        dec = all_rows(conn, rid)["_decision", "M7"]
        assert dec["verdict"] == want, r
    assert dec["metrics"]["leader"] == "alt"


def test_zero_recall_names_no_leader(conn, cfg, two):
    """bk17 R3: a tie at recall 0 is not a leader."""
    dec = m6(conn, cfg, {"sigma": 0.0, "alt": 0.0})
    assert dec["verdict"] == "sigma stays" and dec["metrics"]["leader"] is None and dec["metrics"]["tied"] == []
    assert dec["metrics"]["note"] == "no method detects"
    put_test_a(conn, "M7", {"alt": 0.0})
    rid = new_run(conn)
    board_kind(conn, rid, "M7")
    finish_all(conn, rid, seed=7, cfg=cfg)
    dec = all_rows(conn, rid)["_decision", "M7"]
    assert dec["verdict"] == "no method" and dec["metrics"]["leader"] is None and dec["metrics"]["note"] == "no method detects"


def test_m7_failed_gate_means_no_method(conn, cfg, two):
    put_test_a(conn, "M7", {"alt": 0.9})
    rid = new_run(conn)
    board_kind(conn, rid, "M7", eps=1, kd=1000.0, flap=0.5)
    finish_all(conn, rid, seed=7, cfg=cfg)
    assert all_rows(conn, rid)["_decision", "M7"]["verdict"] == "no method"


def test_m6_m7_stay_pending_without_a_test_a_run_of_this_code_version(conn, cfg, two):
    put_test_a(conn, "M6", {"sigma": 0.5, "alt": 0.9}, code="other")
    rid = new_run(conn)
    board_kind(conn, rid, "M6")
    finish_all(conn, rid, seed=7, cfg=cfg)
    assert all_rows(conn, rid)["_decision", "M6"]["verdict"] == "pending"


def test_note_explains_cheaper_tied_method_and_heavy_loses_to_core():
    d = judge_kind([cand("top", 0.80, [0.70, 0.85], cost=5), cand("cheap", 0.92, [0.80, 0.97], cost=1)])
    assert d["leader"] == "cheap" and "top does" in d["note"]
    d = judge_kind([cand("heavy", 0.70, [0.7, 0.8], heavy=True, cost=0), cand("core", 0.72, [0.7, 0.8], cost=3)])
    assert d["leader"] == "core"


def test_tune_tie_rounds_distance():
    assert tune(True, {1.0: {"coverage": 0.993}, 2.0: {"coverage": 0.997}})[0] == 2.0


# 6 -------------------------------------------------------------------------------------------------------------
def test_strength_labels():
    assert strength_of(7, 1) == "exploratory"
    assert strength_of(14, 1) == "provisional" and strength_of(27, 4) == "provisional"
    assert strength_of(28, 2) == "final" and strength_of(28, 1) == "provisional"


# 7 -------------------------------------------------------------------------------------------------------------
def test_main_end_to_end_writes_all_rows_and_decision(conn, schema):
    seed(conn)
    argv = ["--site", "laptop", "--test", "B", "--methods", "sigma", "--from", DAYS[0].isoformat(),
            "--to", DAYS[-1].isoformat(), "--dsn", TEST_DSN, "--schema", schema, "--results-schema", schema]
    assert labrun.main(argv) == 0
    rows = conn.execute("SELECT method, kind, status FROM lab_results WHERE scope = 'all'").fetchall()
    assert rows and any(r["method"] == "_decision" for r in rows) and any(r["method"] == "sigma" for r in rows)

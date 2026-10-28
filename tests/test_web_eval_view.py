import json
from datetime import date, datetime, time, timedelta

import pytest
from psycopg.types.json import Jsonb

from logai2.timeutil import TZ
from logai2.web.app import create_app, iso
from tests.conftest import TEST_DSN

D1, D2 = date(2026, 10, 10), date(2026, 10, 11)


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def minute(day, slot=0):
    return int((datetime.combine(day, time(), TZ) + timedelta(minutes=slot)).timestamp() // 60)


def run(conn, test="B", status="ok", knobs=None):
    params = {"methods": ["sigma", "ewma"], "knobs": knobs or {"sigma": [2.0, 3.0], "ewma": [0.1, 0.3]}}
    rid = conn.execute(
        "INSERT INTO lab_runs (site, test, status, code_version, window_from, window_to, tune_to, seed, params, kinds) "
        "VALUES ('laptop', %s, %s, 'abc', %s, %s, %s, 1, %s, %s) RETURNING id",
        (test, status, D1, D2, D1, Jsonb(params), Jsonb({"M1": 5}))).fetchone()["id"]
    conn.commit()
    return rid


def result(conn, rid, method, kind, scope, knob, metrics, status="ok", verdict=None, strength=None):
    conn.execute("INSERT INTO lab_results VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (rid, method, kind, scope, knob, status, Jsonb(metrics), verdict, strength))
    conn.commit()


def band(conn, rid, method, knob, day, step, expected=True, svc="pay", key="*total", counts=None):
    n = 1440 // step
    counts = counts or list(range(n))
    exp = [1.0] * n if expected else None
    if exp:
        exp[0] = float("nan")
    conn.execute("INSERT INTO lab_bands VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (rid, method, knob, svc, key, day, step, counts, exp, exp and [0.5] * n, exp and [2.0] * n,
                  [i == 1 for i in range(n)]))
    conn.commit()


def test_runs_newest_first_with_strength(conn, client):
    a, b = run(conn), run(conn, test="A")
    result(conn, a, "_decision", "M1", "all", None, {"leader": None}, verdict="sigma stays", strength="weak")
    r = client.get("/api/eval/runs").get_json()
    assert [x["id"] for x in r] == [b, a]
    assert r[1]["strength"] == "weak" and r[0]["strength"] is None
    assert r[1]["kinds"] == {"M1": 5} and r[1]["window_from"] == "2026-10-10"
    assert r[1]["started_at"].endswith("Z")


def test_scoreboard_test_b(conn, client):
    rid = run(conn)
    result(conn, rid, "_decision", "M1", "all", None,
           {"leader": "ewma", "note": "n", "stability": "stable"}, verdict="ewma leads", strength="ok")
    result(conn, rid, "ewma", "M1", "all", 0.3, {"interval_score": 2.0, "mase": 0.9, "ratio_vs_sigma": 0.8},
           verdict="pass")
    result(conn, rid, "sigma", "M1", "all", 3.0, {"interval_score": 2.5, "coverage": 0.9}, verdict="pass")
    result(conn, rid, "sigma", "M1", "svc", 3.0, {"interval_score": 9})
    sb = client.get(f"/api/eval/scoreboard?run={rid}").get_json()
    m1 = sb["M1"]
    assert m1["decision"]["verdict"] == "ewma leads" and m1["decision"]["leader"] == "ewma"
    assert m1["decision"]["stability"] == "stable" and m1["decision"]["strength"] == "ok"
    assert [m["method"] for m in m1["methods"]] == ["sigma", "ewma"]
    s = m1["methods"][0]
    assert s["knob"] == 3.0 and s["interval_score"] == 2.5 and s["coverage"] == 0.9 and s["mase"] is None


def test_scoreboard_test_a(conn, client):
    rid = run(conn, test="A")
    result(conn, rid, "sigma", "M1", "all", 3.0, {"recall": {"medium": 0.5}, "delay": {"medium": 4}})
    m = client.get(f"/api/eval/scoreboard?run={rid}").get_json()["M1"]["methods"][0]
    assert m["recall"] == {"medium": 0.5} and m["delay"] == {"medium": 4}
    assert "interval_score" not in m and "ratio_vs_sigma" not in m
    rid = run(conn, test="C")
    result(conn, rid, "sigma", "M1", "all", 3.0, {"recall": {"medium": 0.5}, "delay": {"median": 1}})
    assert client.get(f"/api/eval/scoreboard?run={rid}").get_json()["M1"]["methods"][0]["recall"] == {"medium": 0.5}


def seed_series(conn):
    rid = run(conn)
    result(conn, rid, "sigma", "M1", "all", 3.0, {})
    band(conn, rid, "sigma", 3.0, D1, 5)
    band(conn, rid, "sigma", 3.0, D2, 1)
    band(conn, rid, "sigma", 2.0, D1, 5)
    band(conn, rid, "ewma", 0.1, D1, 5, expected=False)
    conn.execute("INSERT INTO lab_episodes VALUES (%s, 'sigma', 3.0, 'pay', '*total', %s, %s, 5, 2)",
                 (rid, minute(D1, 10), minute(D1, 20)))
    conn.execute("INSERT INTO lab_episodes VALUES (%s, 'sigma', 2.0, 'pay', '*total', %s, %s, 5, 2)",
                 (rid, minute(D1, 10), minute(D1, 20)))
    conn.commit()
    return rid


def test_keys(conn, client):
    rid = seed_series(conn)
    r = client.get(f"/api/eval/keys?run={rid}&kind=M1").get_json()
    assert r == [{"service": "pay", "key": "*total", "kind": "M1", "methods": ["sigma", "ewma"]}]


def test_keys_read_the_stored_kinds_and_old_runs_use_the_heuristic(conn, client):
    rid = run(conn)
    for key in ("*total", "tpl", "newt"):
        band(conn, rid, "sigma", 3.0, D1, 5, key=key, counts=[9] * 288)       # tpl: mean 9 would read M3 by heuristic
    band(conn, rid, "ewma", 0.1, D1, 5, key="tpl", counts=[9] * 288)
    result(conn, rid, "sigma", "M4", "pay", None, {"drawn": {"tpl": "M4"}})
    result(conn, rid, "ewma", "M4", "pay", None, {"drawn": {"tpl": "M4"}})
    result(conn, rid, "sigma", "M6", "pay", None, {"drawn": {"newt": "M6"}})
    result(conn, rid, "sigma", "M1", "pay", None, {"drawn": {"*total": "M1"}})
    # bands were written with service "pay"
    q = lambda kind: client.get(f"/api/eval/keys?run={rid}&kind={kind}").get_json()      # noqa: E731
    assert q("M4") == [{"service": "pay", "key": "tpl", "kind": "M4", "methods": ["sigma", "ewma"]}]
    assert q("M6") == [{"service": "pay", "key": "newt", "kind": "M6", "methods": ["sigma"]}]
    assert q("M3") == [] and [k["key"] for k in q("M1")] == ["*total"]


def test_series_steps_default_knob_episodes_nulls(conn, client):
    rid = seed_series(conn)
    r = client.get(f"/api/eval/series?run={rid}&kind=M1&service=pay&key=*total&from=2026-10-10&to=2026-10-11").get_json()
    sg = r["sigma"]
    assert sg["knob"] == 3.0 and len(sg["points"]) == 288 + 1440
    p = sg["points"]
    assert p[0]["t"] == "2026-10-09T17:00:00Z"        # 2026-10-10 00:00 local (Asia/Ho_Chi_Minh, UTC+7)
    assert p[0]["t"] == iso(minute(D1) * 60) and p[1]["t"] == iso(minute(D1, 5) * 60)
    assert p[0]["expected"] is None and p[0]["low"] == 0.5 and p[1]["flag"] is True
    assert p[1]["t"] != p[0]["t"] and p[288]["count"] == 0
    assert (p[289]["t"] > p[288]["t"]) and p[289]["count"] == 1
    assert len(sg["episodes"]) == 1 and sg["episodes"][0]["start_minute"] == minute(D1, 10)
    assert sg["episodes"][0]["start"].endswith("Z")
    # no scope row for ewma: first grid knob with bands; score-less band has null expected
    assert r["ewma"]["knob"] == 0.1 and r["ewma"]["points"][5]["expected"] is None
    one = client.get(f"/api/eval/series?run={rid}&kind=M1&service=pay&key=*total&methods=sigma&knob=2&from=2026-10-10&to=2026-10-10").get_json()
    assert list(one) == ["sigma"] and one["sigma"]["knob"] == 2.0 and len(one["sigma"]["points"]) == 288
    json.dumps(r, allow_nan=False)


def test_series_window_cap_and_missing(conn, client):
    rid = seed_series(conn)
    q = f"/api/eval/series?run={rid}&kind=M1&service=pay&key=*total"
    assert client.get(q + "&from=2026-10-01&to=2026-10-08").status_code == 400
    assert client.get(q + "&from=2026-10-01&to=2026-10-07").status_code == 200
    assert client.get(q + "&from=2026-10-01").status_code == 400
    assert client.get(f"/api/eval/series?run={rid}&from=2026-10-01&to=2026-10-02").status_code == 400


def test_sweep(conn, client):
    rid = run(conn)
    result(conn, rid, "sigma", "M1", "all", 3.0, {})
    for knob, part, v in ((3.0, "test", 3), (2.0, "tune", 1), (3.0, "tune", 2), (2.0, "test", 4)):
        conn.execute("INSERT INTO lab_sweeps VALUES (%s, 'sigma', 'M1', %s, %s, %s)", (rid, knob, part, Jsonb({"v": v})))
    conn.commit()
    r = client.get(f"/api/eval/sweep?run={rid}&kind=M1").get_json()
    assert r["sigma"] == [{"knob": 2.0, "chosen": False, "tune": {"v": 1}, "test": {"v": 4}},
                          {"knob": 3.0, "chosen": True, "tune": {"v": 2}, "test": {"v": 3}}]


def test_choices(conn, client):
    rid = run(conn)
    result(conn, rid, "ewma", "M1", "all", 0.3, {})
    counts = lambda: [conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                      for t in ("alert_state", "baseline", "hourly_counts")]
    before = counts()
    ok = {"kind": "M1", "method": "ewma", "knob": 0.3, "run_id": rid, "note": "x"}
    assert client.post("/api/eval/choices", json=ok).status_code == 200
    assert client.post("/api/eval/choices", json={**ok, "knob": 0.1, "note": "y"}).status_code == 200
    rows = conn.execute("SELECT * FROM lab_choices").fetchall()
    assert len(rows) == 1 and rows[0]["knob"] == 0.1 and rows[0]["note"] == "y" and rows[0]["run_id"] == rid
    for bad in ({"kind": "M9"}, {"method": "nope"}, {"knob": 0.2}, {"kind": None}):
        assert client.post("/api/eval/choices", json={**ok, **bad}).status_code == 400
    assert client.post("/api/eval/choices", json={**ok, "kind": "M2"}).status_code == 400    # no M2 result for ewma
    assert client.post("/api/eval/choices", json={**ok, "note": "x" * 501}).status_code == 400
    assert client.post("/api/eval/choices", json={**ok, "run_id": 999}).status_code == 404
    assert counts() == before


def test_unknown_run_404(client):
    for u in ("scoreboard?run=999", "keys?run=999&kind=M1", "sweep?run=999&kind=M1", "scoreboard?run=x",
              "series?run=999&kind=M1&service=a&key=b&from=2026-10-01&to=2026-10-02"):
        assert client.get("/api/eval/" + u).status_code == 404, u
    assert client.get("/api/eval/scoreboard").status_code == 400


def test_series_chosen_knob_is_per_kind(conn, client):
    rid = run(conn)
    result(conn, rid, "sigma", "M1", "all", 3.0, {})
    result(conn, rid, "sigma", "M2", "all", 2.0, {})
    band(conn, rid, "sigma", 3.0, D1, 5)
    band(conn, rid, "sigma", 2.0, D1, 5)
    q = f"/api/eval/series?run={rid}&service=pay&key=*total&from=2026-10-10&to=2026-10-10&methods=sigma&kind="
    assert client.get(q + "M1").get_json()["sigma"]["knob"] == 3.0
    assert client.get(q + "M2").get_json()["sigma"]["knob"] == 2.0
    assert client.get(q + "M9").status_code == 400
    assert client.get(q[:-5]).status_code == 400
    assert client.get(f"/api/eval/keys?run={rid}&kind=M9").status_code == 400
    assert client.get(f"/api/eval/sweep?run={rid}&kind=M9").status_code == 400

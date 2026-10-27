from array import array
from datetime import date

import pytest

import scripts.lab.methods as methods
from scripts.lab import run as labrun
from scripts.lab.methods import DayOut, Spec, apply_d4, select
from scripts.lab.store import prune, start_run
from tests.conftest import TEST_DSN
from test_lab_load import D1, D2, put

DAYS = [date(2026, 10, d) for d in range(1, 15)]


def count(conn, table="lab_runs"):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


class Const:
    spec = Spec("const", "T1", "constant band", frozenset({"M1", "M3"}), (1.0, 2.0), 1.0, True, False, 1)

    def fit(self, sd, day, cfg):
        return {"day": day}

    def day(self, model, prev, sd, key, day, knobs, cfg):
        flat = array("d", [5.0] * 1440)
        return {k: DayOut(flat, flat, array("d", [5.0 + k] * 1440), flat, bytearray(1440)) for k in knobs}


class Boom(Const):
    spec = Spec("boom", "T2", "raises", frozenset({"M1"}), (1.0,), 1.0, True, False, 2)

    def fit(self, sd, day, cfg):
        raise RuntimeError("nope")


def args(methods="const", to="2026-10-14"):
    return labrun.parse_args(["--site", "laptop", "--test", "B", "--methods", methods, "--from", "2026-10-01",
                              "--to", to])


def seed(conn):
    for d in DAYS:
        for h in range(24):
            put(conn, "*minutes", d, h, [1] * 60)
            put(conn, "*total", d, h, [6] * 60)
            put(conn, "busy", d, h, [3] * 60)
    conn.commit()


def test_company_a_or_c_exits_2_before_connecting(monkeypatch, conn):
    monkeypatch.setattr(labrun, "connect", lambda *a, **k: pytest.fail("connected"))
    for test in "AC":
        assert labrun.main(["--site", "company", "--test", test, "--methods", "core", "--days", "3"]) == 2
    assert count(conn) == 0


def test_check_with_no_methods(monkeypatch, capsys):
    monkeypatch.setattr(labrun, "connect", lambda *a, **k: pytest.fail("connected"))
    assert labrun.main(["--site", "laptop", "--test", "B", "--methods", "all", "--days", "3", "--check"]) == 1
    assert "available" not in capsys.readouterr().out.replace("unavailable", "")
    monkeypatch.setattr(methods, "METHODS", {"const": Const})
    assert labrun.main(["--site", "laptop", "--test", "B", "--methods", "const", "--days", "3", "--check"]) == 0
    assert "const: available" in capsys.readouterr().out


def test_select(monkeypatch):
    monkeypatch.setattr(methods, "METHODS", {"a": Const, "b": Const})
    monkeypatch.setattr(methods, "GROUPS", {"core": ["a"], "heavy": ["b"]})
    assert select("core") == ["a"] and select("heavy") == ["b"] and select("all") == ["a", "b"]
    assert select("b,a") == ["b", "a"]
    with pytest.raises(ValueError):
        select("a,zzz")


def test_run_end_to_end_with_failed_method(monkeypatch, conn, cfg):
    monkeypatch.setattr(methods, "METHODS", {"const": Const, "boom": Boom})
    seed(conn)
    a = args("const,boom")
    assert labrun.run(a, cfg, conn, conn) == 0
    run = conn.execute("SELECT * FROM lab_runs").fetchone()
    assert run["status"] == "ok" and run["finished_at"] and run["params"]["split"] == "days"
    assert run["site"] == "laptop" and run["window_from"] == DAYS[0] and run["tune_to"] == DAYS[6] and run["seed"] == 7
    res = {(r["method"], r["kind"]): r for r in conn.execute("SELECT * FROM lab_results")}
    assert res[("const", "M1")]["status"] == "ok" and res[("const", "M1")]["metrics"]["n_keys"] == 1
    assert res[("const", "M3")]["metrics"]["n_keys"] == 1 and res[("const", "M3")]["metrics"]["n_minutes"] > 0
    assert res[("boom", "M1")]["status"] == "failed" and "nope" in res[("boom", "M1")]["metrics"]["error"]


def test_short_window_splits_keys(monkeypatch, conn, cfg):
    monkeypatch.setattr(methods, "METHODS", {"const": Const})
    seed(conn)
    labrun.run(args(to="2026-10-04"), cfg, conn, conn)
    assert conn.execute("SELECT params->>'split' AS s FROM lab_runs").fetchone()["s"] == "keys"


def test_prune_keeps_five_per_site_and_cascades(conn):
    from scripts.lab.store import put_result
    kw = dict(test="B", code_version="x", image_tag=None, data_schema="public", window_from=D1, window_to=D2,
              tune_to=D1, seed=1, params={})
    ids = [start_run(conn, site="laptop", **kw) for _ in range(7)] + [start_run(conn, site="company", **kw)]
    put_result(conn, ids[0], "m", "M1", "all", None, "ok", {})
    assert prune(conn, "laptop") == 2
    assert [r["id"] for r in conn.execute("SELECT id FROM lab_runs WHERE site = 'laptop' ORDER BY id")] == ids[2:7]
    assert count(conn, "lab_results") == 0 and count(conn) == 6


@pytest.fixture
def d4cfg(cfg):
    return cfg      # floors ERROR 5, WARN 10; drop.min_mean 20


def test_apply_d4_truth_table(d4cfg):
    c = d4cfg
    f = lambda count, low, high, exp, key, level: apply_d4(count, low, high, exp, key, level, c)   # noqa: E731
    assert f(6, 0, 5, 3, "t", "ERROR") and not f(5, 0, 5, 3, "t", "ERROR")          # count > high
    assert not f(4, 0, 3, 3, "t", "ERROR") and f(5, 0, 3, 3, "t", "FATAL")           # error floor 5
    assert not f(9, 0, 3, 3, "t", "WARN") and f(10, 0, 3, 3, "t", "WARNING")          # warn floor 10
    assert f(1, 0, 0, 0, "t", "INFO") and not f(0, 0, 0, 0, "t", "INFO")             # INFO: no floor
    assert not f(4, 0, 3, 3, "*error", "INFO") and f(5, 0, 3, 3, "*error", "INFO")   # key level beats level
    assert f(10, 12, 100, 30, "*total", "INFO")                                      # drop: below low, expected >= 20
    assert not f(10, 12, 100, 19, "*total", "INFO")                                  # expected < 20
    assert not f(10, 12, 100, 30, "t", "INFO")                                       # drop only on *total
    assert not f(13, 12, 100, 30, "*total", "INFO")                                  # not below low
    assert not f(10, None, None, None, "*total", "INFO")                             # no band: nothing

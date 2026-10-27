import gc
import weakref
import zlib
from array import array
from datetime import date, datetime

import psycopg
import pytest

import scripts.lab.methods as methods
from logai2.timeutil import TZ
from scripts.lab import run as labrun
from scripts.lab.data import minute_at  # noqa: F401
from scripts.lab.methods import DayOut, Spec, apply_d4, floor_for, select
from scripts.lab.store import prune, start_run
from tests.conftest import TEST_DSN
from tests.test_links import tmpl
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
    f = lambda count, low, high, exp, key, level, **kw: apply_d4(count, low, high, exp, key, level, c, **kw)   # noqa: E731
    assert f(6, 0, 5, 3, "t", "ERROR") and not f(5, 0, 5, 3, "t", "ERROR")          # count > high
    assert not f(4, 0, 3, 3, "t", "ERROR") and f(5, 0, 3, 3, "t", "FATAL")           # error floor 5
    assert not f(9, 0, 3, 3, "t", "WARN") and f(10, 0, 3, 3, "t", "WARNING")          # warn floor 10
    assert not f(1000, 0, 0, 0, "t", "INFO")                                          # INFO template: never high
    assert not f(1000, 0, 0, 0, "*total", "INFO")                                     # *total: never high on the upper side
    assert not f(4, 0, 3, 3, "*error", "INFO") and f(5, 0, 3, 3, "*error", "INFO")   # key level beats level
    assert f(10, 12, 100, 30, "*total", "INFO")                                      # drop: below low, expected >= 20
    assert not f(10, 12, 100, 19, "*total", "INFO")                                  # expected < 20
    assert not f(10, 12, 100, 30, "t", "INFO")                                       # drop only on *total
    assert not f(13, 12, 100, 30, "*total", "INFO")                                  # not below low
    assert not f(10, None, None, None, "*total", "INFO")                             # no band: nothing
    # score method: score > threshold, floor tested on the raw count
    assert f(4, None, 1.0, None, "t", "ERROR", score=1.5) is False                   # count below floor 5
    assert f(5, None, 1.0, None, "t", "ERROR", score=1.5) and not f(50, None, 1.0, None, "t", "ERROR", score=1.0)
    assert not f(50, None, 1.0, None, "t", "INFO", score=9.0)                        # INFO: never


def test_floor_for_accepts_none(d4cfg):
    assert floor_for("t", None, d4cfg) is None and floor_for("*error", None, d4cfg) == 5
    assert floor_for("*total", None, d4cfg) is None and floor_for("t", "warn", d4cfg) == 10


def test_parse_args_validation():
    base = ["--site", "laptop", "--test", "B", "--methods", "core"]
    for bad in (["--days", "0"], ["--from", "2026-10-05", "--to", "2026-10-04"], [], ["--days", "3", "--from", "2026-10-01"]):
        with pytest.raises(SystemExit):
            labrun.parse_args(base + bad)


def test_part_by_days_and_by_keys():
    days = [date(2026, 10, d) for d in range(1, 15)]
    p = labrun.make_part("s", days, days[:7], True)
    assert p("k", days[6]) == "tune" and p("k", days[7]) == "test"
    short = days[:4]
    p = labrun.make_part("s", short, short, False)
    for key in ("a", "b", "c", "d", "e"):
        want = "tune" if zlib.crc32(("s" + key).encode()) % 2 == 0 else "test"
        assert p(key, short[0]) == want == p(key, short[3])
    assert {p(k, short[0]) for k in "abcdefgh"} == {"tune", "test"}


class Recorder:
    """A sink that records every call and holds weak refs to the arrays it was handed."""
    def __init__(self):
        self.calls, self.refs = [], []

    def add(self, key, day, outs):
        self.calls.append(("add", key, day))
        self.refs += [weakref.ref(o.score) for o in outs.values()]

    def skip(self, day):
        self.calls.append(("skip", day))

    def end(self):
        self.calls.append(("end",))


class Fresh(Const):
    """Fresh arrays every call; fit returns None on day 3; records fit/prev order."""
    spec = Spec("fresh", "T3", "fresh", frozenset({"M1", "M6"}), (1.0, 2.0), 1.0, True, False, 1)
    log: list

    def __init__(self):
        self.log = []

    def fit(self, sd, day, cfg):
        self.log.append(("fit", day))
        return None if day == DAYS[2] else {"day": day}

    def day(self, model, prev, sd, key, day, knobs, cfg):
        self.log.append(("day", key, day, prev))
        return {k: DayOut(None, None, None, array("d", [k] * 1440), bytearray(1440)) for k in knobs}


def test_streaming_order_prev_model_skip_and_no_references(monkeypatch, conn, cfg):
    m, sinks = Fresh(), []
    monkeypatch.setattr(methods, "METHODS", {"fresh": lambda: m})
    seed(conn)
    tmpl(conn, "busy", "INFO")             # 3/min (M3), born inside the window: walked because the method serves M6
    conn.execute("UPDATE templates SET first_seen = %s WHERE template_id = 'busy'", (datetime(2026, 10, 5, tzinfo=TZ),))
    conn.commit()
    labrun.run(args("fresh"), cfg, conn, conn, collect=lambda *a: sinks.append(Recorder()) or sinks[-1])
    (sink,) = sinks
    assert sink.calls[-1] == ("end",) and sink.calls.count(("end",)) == 1
    assert sink.calls.count(("skip", DAYS[2])) == 1 and sum(c[0] == "skip" for c in sink.calls) == 1
    adds = [c for c in sink.calls if c[0] == "add"]
    assert {c[1] for c in adds} == {"*total", "busy"}                  # M2 (*error) is not served, so not walked
    assert [c[2] for c in adds] == sorted(c[2] for c in adds)          # ascending days, nothing out of order
    assert {c[2] for c in adds} == set(DAYS) - {DAYS[2]}
    assert [e[1] for e in m.log if e[0] == "fit"] == DAYS
    for _, key, day, prev in (e for e in m.log if e[0] == "day"):
        i = DAYS.index(day)
        assert prev == (None if i == 0 or i == 3 else {"day": DAYS[i - 1]})   # day 3 follows the refused fit
    gc.collect()
    assert sink.refs and all(r() is None for r in sink.refs)           # the runner kept nothing


def test_old_template_not_walked_for_m6(monkeypatch, conn, cfg):
    m, sinks = Fresh(), []
    monkeypatch.setattr(methods, "METHODS", {"fresh": lambda: m})
    seed(conn)
    tmpl(conn, "busy", "INFO")             # first_seen = now(), after the window: never new inside it
    conn.commit()
    labrun.run(args("fresh"), cfg, conn, conn, collect=lambda *a: sinks.append(Recorder()) or sinks[-1])
    assert {c[1] for c in sinks[0].calls if c[0] == "add"} == {"*total"}


def test_no_fit_means_skipped_per_kind(monkeypatch, conn, cfg):
    class NoFit(Const):
        spec = Spec("nofit", "T4", "never fits", frozenset({"M1", "M4"}), (1.0,), 1.0, True, False, 1)

        def fit(self, sd, day, cfg):
            return None

    monkeypatch.setattr(methods, "METHODS", {"nofit": NoFit})
    seed(conn)
    labrun.run(args("nofit"), cfg, conn, conn)
    res = {r["kind"]: r for r in conn.execute("SELECT * FROM lab_results WHERE method = 'nofit'")}
    assert res["M1"]["status"] == "skipped" and res["M1"]["metrics"] == {"n_keys": 1, "n_minutes": 0}
    assert res["M4"]["status"] == "skipped" and res["M4"]["metrics"]["n_keys"] == 0


def test_main_connections_are_read_only_and_schema_scoped(monkeypatch, conn, schema):
    monkeypatch.setattr(methods, "METHODS", {"const": Const})
    seed(conn)
    real = labrun.run

    def probe(a, cfg, data, results):
        for _ in range(2):                  # still read-only after a rollback
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                data.execute("DELETE FROM lab_minute_counts")
            data.rollback()
        assert data.execute("SHOW search_path").fetchone()["search_path"] == f"{schema}, public"
        assert results.execute("SHOW search_path").fetchone()["search_path"] == schema
        results.commit()                    # the store's own transactions must be the outermost ones
        return real(a, cfg, data, results)

    monkeypatch.setattr(labrun, "run", probe)
    argv = ["--site", "laptop", "--test", "B", "--methods", "const", "--from", "2026-10-01", "--to", "2026-10-14",
            "--dsn", TEST_DSN, "--schema", schema, "--results-schema", schema]
    rc = labrun.main(argv)
    assert rc == 0
    assert count(conn) == 1 and count(conn, "lab_minute_counts") > 0


def test_only_one_service_data_alive(monkeypatch, conn, cfg):
    monkeypatch.setattr(methods, "METHODS", {"fresh": Fresh})
    seed(conn)
    for d in DAYS[:2]:
        put(conn, "*minutes", d, 0, [1] * 60, service="s2")
        put(conn, "*total", d, 0, [6] * 60, service="s2")
    conn.commit()
    refs, alive = [], []

    def collect(c, run_id, service, name, spec, kinds, ctx):
        if refs:
            gc.collect()
            alive.append(refs[0]() is not None)
        refs.append(weakref.ref(ctx.sd))
        return Recorder()

    assert labrun.run(args("fresh"), cfg, conn, conn, collect=collect) == 0
    assert len(refs) == 2 and alive == [False]


def test_failing_sink_leaves_only_failed_rows(monkeypatch, conn, cfg):
    from scripts.lab import store
    monkeypatch.setattr(methods, "METHODS", {"fresh": Fresh})
    seed(conn)

    class Bad(Recorder):
        def __init__(self, c, run_id, service):
            super().__init__()
            self.c, self.run_id, self.service = c, run_id, service

        def end(self):
            store.put_result(self.c, self.run_id, "fresh", "M1", self.service, None, "ok", {})
            raise RuntimeError("late")

    rc = labrun.run(args("fresh"), cfg, conn, conn, collect=lambda c, r, s, *a: Bad(c, r, s))
    assert rc == 0
    rows = conn.execute("SELECT kind, status, metrics FROM lab_results ORDER BY kind").fetchall()
    assert [(r["kind"], r["status"]) for r in rows] == [("M1", "failed"), ("M6", "failed")]
    assert "late" in rows[0]["metrics"]["error"]
    assert conn.execute("SELECT status FROM lab_runs").fetchone()["status"] == "ok"


def test_finish_hook_runs_before_finish_run(monkeypatch, conn, cfg):
    monkeypatch.setattr(methods, "METHODS", {"const": Const})
    seed(conn)
    seen = []

    def hook(c, run_id):
        seen.append((run_id, c.execute("SELECT status FROM lab_runs WHERE id = %s", (run_id,)).fetchone()["status"]))

    assert labrun.run(args(), cfg, conn, conn, finish=hook) == 0
    assert len(seen) == 1 and seen[0][1] == "running"
    assert conn.execute("SELECT status FROM lab_runs").fetchone()["status"] == "ok"

    def boom(c, run_id):
        raise RuntimeError("tune failed")

    assert labrun.run(args(), cfg, conn, conn, finish=boom) == 1
    last = conn.execute("SELECT status, error FROM lab_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert last["status"] == "failed" and "tune failed" in last["error"]

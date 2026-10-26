"""Re-read lane: pure planning, the lane over FakeSource, the owner's whole-hour replace, and the engine's run."""
from __future__ import annotations

import logging
from datetime import date
from unittest import mock

import pytest

from logai2 import commands
from logai2.jobs import runs
from logai2.jobs.reread import Lane, Scope, plan
from logai2.reader.normalize import normalize
from logai2.reader.reader import Reader
from logai2.owner import owner as owner_mod
from logai2.owner.owner import Owner
from logai2.reader.cache import KnownLines
from tests.fakes import FakeSource, hit
from tests.test_engine import NOON, Clock, catch_up, history, make, q  # noqa: F401 — make is a fixture
from tests.test_owner import M, T0, rows, run_minutes

H = 3_600_000
DAY = date(2026, 10, 20)   # T0 is 10:00 local that day
TABLES = ("service_hours", "hourly_counts", "template_module_hours")


# --- pure planning -----------------------------------------------------------

def test_scope_json_roundtrip():
    s = Scope(["a", "b"], [date(2026, 10, 14), date(2026, 10, 15)], 9, 17)
    assert Scope.from_json(s.to_json()) == s
    assert Scope.from_json(Scope(None, [DAY], 0, 24).to_json()).services is None


def test_plan_clamps_not_in_es_and_live_hours():
    p = plan(Scope(None, [DAY], 8, 14), {"a": ["s", "t"]}, {"a": T0}, T0 + 2 * H)
    assert [(w.start_ms, w.end_ms) for w in p.windows] == [(T0, T0 + H), (T0 + H, T0 + 2 * H)]
    assert p.windows[0].services == ("s", "t") and p.windows[0].patterns == ("a",)
    assert (p.hours_chosen, p.hours_not_in_es, p.hours_live, p.hours_to_read) == (12, 4, 4, 4)
    assert (p.services, p.patterns) == (["s", "t"], ["a"])
    # an hour only partly in ES (oldest inside it) is not in ES either
    p = plan(Scope(["s"], [DAY], 10, 12), {"a": ["s"]}, {"a": T0 + 1800_000}, T0 + 5 * H)
    assert [w.start_ms for w in p.windows] == [T0 + H] and p.hours_not_in_es == 1
    # a pattern without any doc: nothing in ES
    p = plan(Scope(["s"], [DAY], 10, 12), {"a": ["s"]}, {"a": None}, T0 + 5 * H)
    assert p.windows == [] and (p.hours_not_in_es, p.hours_to_read) == (2, 0)


def test_plan_service_with_two_patterns_reads_both():
    pats = {"a": ["s", "x"], "b": ["s", "y"]}
    p = plan(Scope(["s"], [DAY], 10, 13), pats, {"a": T0, "b": T0 + 2 * H}, T0 + 9 * H)
    assert [(w.start_ms, w.services, w.patterns) for w in p.windows] == [(T0 + 2 * H, ("s",), ("a", "b"))]
    assert p.hours_not_in_es == 2
    p = plan(Scope(None, [DAY], 10, 11), pats, {"a": T0, "b": T0}, T0 + 9 * H)
    assert p.windows[0].services == ("s", "x", "y") and p.windows[0].patterns == ("a", "b")
    assert p.windows[0].services and p.hours_to_read == 3


# --- the lane over FakeSource and the owner ----------------------------------

def docs(hour, per=2, service="s", level="ERROR", msg="payment failed for user 42", module="M1", prefix="d",
         skip=lambda m: False, minutes=60):
    return [hit(f"{prefix}-{service}-{hour}-{m}-{i}", T0 + (hour - 10) * H + m * 60_000 + i * 1000,
                service=service, level=level, message=msg, moduleCode=module)
            for m in range(minutes) if not skip(m) for i in range(per)]


def mixed():
    """Two hours: errors with silent minutes, warnings in other module, infos."""
    out = []
    for hour in (10, 11):
        out += docs(hour, skip=lambda m: m % 7 == 3)
        out += docs(hour, per=1, level="WARN", msg="disk usage at 91 percent", module="M2", prefix="w",
                    skip=lambda m: m % 5 != 0)
    out += docs(11, per=3, level="INFO", msg="job done in 12 ms", module="", prefix="i", skip=lambda m: m % 4 != 1)
    return out


def snap(conn):
    return {t: sorted(tuple(r.values()) for r in rows(conn, f"SELECT * FROM {t}")) for t in TABLES}


def wipe(conn):
    for t in (*TABLES, "templates", "drain3_state", "template_modules", "error_samples", "modules", "services",
              "retrain_runs"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def lane_of(conn, cfg, src, *, hour_from=10, hour_to=12, services=None, patterns=None, oldest=T0,
            owner_hour_ms=T0 + 4 * H, pos_ms=0, first=None, run_id=None, size=None):
    now_s = (T0 + 6 * H) / 1000
    owner = Owner(conn, cfg, M, now=lambda: now_s)
    patterns = patterns or {"a": ["s"]}
    p = plan(Scope(services, [DAY], hour_from, hour_to), patterns, dict.fromkeys(patterns, oldest), owner_hour_ms)
    if run_id is None:
        run_id = runs.start(conn, "reread", "manual")
        conn.rollback()
    cfg.elasticsearch.batch_size = size or src.size
    return owner, Lane(p, run_id, pos_ms, src, owner.miners, KnownLines(1000), first or {}, cfg), run_id


def drive(owner, lane, run_id, steps=1000, flush=True, budget=0.0):
    """One page per step (budget 0); flush after each step like the engine's flush."""
    for _ in range(steps):
        if lane.done:
            break
        owner.add_lane(run_id, lane.step(budget, 1.0), {"pos_ms": lane.pos_ms, "steps": []})
        if flush:
            owner.flush({}, {})


def totals(conn):
    return {(r["day"], r["hour"]): r["sum"] for r in
            rows(conn, "SELECT day, hour, sum FROM hourly_counts WHERE key = '*total' AND service = 's'")}


def test_reread_equals_live_counts(conn, cfg):
    hits = mixed()
    run_minutes(conn, cfg, hits, T0 + 2 * H)
    live = snap(conn)
    assert live["service_hours"] and live["template_module_hours"]
    wipe(conn)
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": hits}))
    drive(owner, lane, rid)
    assert snap(conn) == live


def live_two_patterns(conn, cfg, src, until_ms):
    saved = {}
    reader = Reader(src, cfg, KnownLines(1000), now=lambda: until_ms / 1000 + 60)
    reader.set_sources({"a": T0, "b": T0}, saved)
    owner = Owner(conn, cfg, M, now=lambda: until_ms / 1000 + 60)
    for _ in range(20):
        batch = reader.poll()
        reader.learn(owner.handle(batch))
        owner.flush(reader.cursors(owner.boundary_ms), {})
        reader.forget_before(owner.boundary_ms)
        if batch["watermark_ms"] >= until_ms:
            return
    raise AssertionError("watermark never passed until_ms")


def test_reread_equals_live_two_services_two_patterns(conn, cfg, monkeypatch):
    import logai2.jobs.reread as rr
    import logai2.reader.reader as rd
    for mod in (rr, rd):   # a line whose normalization fails is counted as <service>:unparsed
        real = mod.normalize
        monkeypatch.setattr(mod, "normalize",
                            lambda m, real=real: (_ for _ in ()).throw(ValueError("bad")) if "poison" in m else real(m))
    # every template of a appears in hour 10 and b's after them: Drain3 numbers templates by first appearance,
    # which is the same for live (a, then b) and the lane (per hour, a then b) then
    both = lambda h: docs(h, skip=lambda m: m % 7 == 3) + docs(h, per=1, level="WARN", msg="disk usage at 91 percent",
                                                               module="M2", prefix="w", skip=lambda m: m % 5 != 0)
    hits = {"a": both(10) + both(11) + docs(10, service="t", msg="timeout talking to host 7", module="M3", prefix="ta",
                                skip=lambda m: m % 4 == 0)
                 + [hit("poison", T0 + 5000, service="t", message="poison pill", level="WARN")],
            "b": docs(10, service="s", level="WARN", msg="queue length is 9", module="M4", prefix="sb",
                      skip=lambda m: m % 3 != 0)
                 + docs(11, service="s", level="INFO", msg="queue length is 9", module="", prefix="sb2",
                        skip=lambda m: m % 9 != 0)}
    src = FakeSource(hits)
    live_two_patterns(conn, cfg, src, T0 + 2 * H)
    live = snap(conn)
    assert {r[0] for r in live["service_hours"]} == {"s", "t"}
    assert any(r[1] == "t:unparsed" for r in live["hourly_counts"]) and len(live["service_hours"]) == 4
    wipe(conn)
    now_s = (T0 + 6 * H) / 1000
    owner = Owner(conn, cfg, M, now=lambda: now_s)
    p = plan(Scope(None, [DAY], 10, 12), {"a": ["s", "t"], "b": ["s"]}, {"a": T0, "b": T0}, T0 + 4 * H)
    rid = runs.start(conn, "reread", "manual")
    conn.rollback()
    lane = Lane(p, rid, 0, src, owner.miners, KnownLines(1000), {}, cfg)
    drive(owner, lane, rid)
    assert snap(conn) == live


def test_reread_failed_flush_rolls_back_the_replace(conn, cfg):
    old = docs(10, msg="old failure 1", prefix="o") + docs(11, prefix="k", level="WARN", msg="disk usage at 91 percent")
    new = docs(10, per=1, level="WARN", msg="brand new warning", module="M9", prefix="n") \
        + docs(11, prefix="k2", msg="disk usage at 91 percent")

    def replace(fail):
        run_minutes(conn, cfg, old, T0 + 2 * H)
        before = snap(conn)
        owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": new}))
        drive(owner, lane, rid, flush=False, budget=5.0)
        if fail:
            real = owner_mod.copy_upsert

            def boom(cur, table, rows_):
                if table == "template_module_hours" and rows_:   # runs after the lane's DELETEs
                    raise RuntimeError("flush dies")
                return real(cur, table, rows_)
            with mock.patch.object(owner_mod, "copy_upsert", boom), pytest.raises(RuntimeError):
                owner.flush({}, {})
            assert snap(conn) == before   # delete and insert rolled back together
        owner.flush({}, {})   # the retry writes the pending hours once
        return snap(conn)
    retried = replace(fail=True)
    wipe(conn)
    assert retried == replace(fail=False) and retried != {}


def test_reread_never_writes_hours_not_in_es(conn, cfg):
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": docs(10) + docs(11)}), oldest=T0 + H)
    drive(owner, lane, rid)
    assert {r["hour"] for r in rows(conn, "SELECT hour FROM service_hours")} == {11}
    assert {r["hour"] for r in rows(conn, "SELECT hour FROM hourly_counts")} == {11}


def test_reread_stops_before_owner_hour(conn, cfg):
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": docs(10) + docs(11)}), owner_hour_ms=T0 + H)
    drive(owner, lane, rid)
    assert {r["hour"] for r in rows(conn, "SELECT hour FROM service_hours")} == {10}


def test_reread_replaces_whole_hour(conn, cfg):
    old = docs(10, msg="old failure 1", prefix="o") + docs(11, prefix="k")
    run_minutes(conn, cfg, old, T0 + 2 * H)
    before = snap(conn)
    new = docs(10, per=1, level="WARN", msg="brand new warning", module="M9", prefix="n") + docs(11, prefix="k")
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": new}), hour_to=11)   # hour 10 only
    drive(owner, lane, rid)
    after = snap(conn)
    assert totals(conn) == {(DAY, 10): 60, (DAY, 11): 120}
    assert rows(conn, "SELECT n_minutes FROM service_hours WHERE hour = 10")[0]["n_minutes"] == 60
    assert "*error" not in {r["key"] for r in rows(conn, "SELECT key FROM hourly_counts WHERE hour = 10")}
    assert {r["module_code"] for r in rows(conn, "SELECT module_code FROM template_module_hours WHERE hour = 10")} \
        == {"M9"}
    # hour 11 was not in the scope: its rows are byte for byte what live wrote
    pick = lambda s, h: [r for r in s["hourly_counts"] if r[3] == h] + [r for r in s["service_hours"] if r[2] == h]
    assert pick(after, 11) == pick(before, 11)


def test_reread_replaces_old_rows_of_a_gone_template(conn, cfg):
    run_minutes(conn, cfg, docs(10, msg="old failure 1", prefix="o", level="WARN"), T0 + H)
    old_keys = {r["key"] for r in rows(conn, "SELECT key FROM hourly_counts") if not r["key"].startswith("*")}
    assert old_keys
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": docs(10, level="INFO", msg="other text entirely", prefix="n",
                                                                minutes=10)}), hour_to=11)
    drive(owner, lane, rid)
    assert totals(conn) == {(DAY, 10): 20}
    assert rows(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE key = '*warn'")[0]["n"] is None
    assert rows(conn, "SELECT n_minutes FROM service_hours")[0]["n_minutes"] == 60


def test_reread_other_service_untouched(conn, cfg):
    both = docs(10, service="s") + docs(10, service="t", msg="timeout talking to host 7", prefix="t")
    run_minutes(conn, cfg, both, T0 + H)
    t_before = [r for r in snap(conn)["hourly_counts"] if r[0] == "t"]
    th_before = [r for r in snap(conn)["service_hours"] if r[0] == "t"]
    extra = both + docs(10, service="s", prefix="late", minutes=1) + docs(10, service="t", prefix="tlate", minutes=1)
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": extra}), services=["s"], hour_to=11,
                               patterns={"a": ["s", "t"]})
    drive(owner, lane, rid)
    s = snap(conn)
    assert [r for r in s["hourly_counts"] if r[0] == "t"] == t_before
    assert [r for r in s["service_hours"] if r[0] == "t"] == th_before
    assert totals(conn) == {(DAY, 10): 122}


def test_reread_crash_resumes_exact(conn, cfg):
    hits = mixed()
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": hits}))
    drive(owner, lane, rid)
    clean = snap(conn)
    wipe(conn)
    # crash: the first window is flushed, the second is only queued in memory
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": hits}))
    drive(owner, lane, rid, steps=1)
    owner.add_lane(rid, lane.step(0.0, 1.0), {"pos_ms": lane.pos_ms, "steps": []})   # never flushed
    pos = rows(conn, "SELECT pos_ms FROM retrain_runs WHERE id = %s", rid)[0]["pos_ms"]
    assert pos == T0 + H
    owner, lane, _ = lane_of(conn, cfg, FakeSource({"a": hits}), pos_ms=pos, run_id=rid)   # restart from the DB
    drive(owner, lane, rid)
    assert snap(conn) == clean
    assert rows(conn, "SELECT pos_ms FROM retrain_runs WHERE id = %s", rid)[0]["pos_ms"] == T0 + 2 * H


def test_reread_index_deleted_midway_warns(conn, cfg, caplog):
    src = FakeSource({"a-1": docs(10), "a-2": docs(11)})
    owner, lane, rid = lane_of(conn, cfg, src, patterns={"a-*": ["s"]})
    # plan() was made while a-1 existed; retention drops it before the lane reads hour 10
    src.delete_index("a-1")
    with caplog.at_level(logging.WARNING, logger="logai2"):
        drive(owner, lane, rid)
    assert {r["hour"] for r in rows(conn, "SELECT hour FROM service_hours")} == {11}
    assert lane.skipped == 1
    assert any("no longer in Elasticsearch" in r.message for r in caplog.records)


def test_samples_only_for_new_hours(conn, cfg):
    run_minutes(conn, cfg, docs(10, per=1, minutes=3), T0 + H)
    n = rows(conn, "SELECT count(*) AS n FROM error_samples")[0]["n"]
    assert n == 3
    # hour 10 had rows: a re-read must not add its samples again; hour 11 is new and gets them
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": docs(10, per=1, minutes=3) + docs(11, per=1, minutes=3)}))
    drive(owner, lane, rid)
    assert rows(conn, "SELECT count(*) AS n FROM error_samples")[0]["n"] == 6
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": docs(10, per=1, minutes=3) + docs(11, per=1, minutes=3)}))
    drive(owner, lane, rid)
    assert rows(conn, "SELECT count(*) AS n FROM error_samples")[0]["n"] == 6


def test_lane_dedup_bounded(conn, cfg):
    # 2 docs per millisecond, pages of 51: every page boundary splits a millisecond pair
    hits = [hit(f"x{i}-{j}", T0 + i * 7, service="s", level="INFO", message="job done in 12 ms")
            for i in range(300) for j in range(2)]
    src = FakeSource({"a": hits})
    src.size = 51
    owner, lane, rid = lane_of(conn, cfg, src, hour_to=11, size=51)
    biggest = 0
    for _ in range(100):
        if lane.done:
            break
        owner.add_lane(rid, lane.step(0.0, 1.0), {"pos_ms": lane.pos_ms, "steps": []})
        biggest = max(biggest, len(lane.seen))
        owner.flush({}, {})
    assert lane.done and biggest <= 2   # ids only at the current position ms, never one set per hour
    assert lane.docs_read == 600 and totals(conn) == {(DAY, 10): 600}


def test_lane_dedup_page_boundary_counts_every_doc_once(conn, cfg):
    hits = [hit(f"y{i}", T0 + (i // 3) * 1000, service="s", level="INFO", message="job done in 12 ms")
            for i in range(200)]
    src = FakeSource({"a": hits})
    src.size = 10
    owner, lane, rid = lane_of(conn, cfg, src, hour_to=11, size=10)
    drive(owner, lane, rid)
    assert totals(conn) == {(DAY, 10): 200}


def test_lane_passes_the_es_timeout(conn, cfg):
    cfg.retrain.es_timeout_seconds = 17
    src = FakeSource({"a": docs(10, minutes=2)})
    owner, lane, rid = lane_of(conn, cfg, src, hour_to=11)
    drive(owner, lane, rid)
    assert set(src.timeouts) == {17}


def test_failed_page_after_a_closed_window_loses_no_hour(conn, cfg):
    hits = mixed() + docs(12, prefix="e", minutes=20)
    owner, lane, rid = lane_of(conn, cfg, FakeSource({"a": hits}), hour_to=13)
    drive(owner, lane, rid, budget=5.0)
    clean = snap(conn)
    wipe(conn)
    src = FakeSource({"a": hits})
    owner, lane, rid = lane_of(conn, cfg, src, hour_to=13)
    real, calls = src.page, [0]

    def page(*a, **k):
        calls[0] += 1
        if calls[0] == 2:   # window 1 is closed by now, window 2's page fails
            raise ConnectionError("blip")
        return real(*a, **k)
    src.page = page
    for _ in range(10):
        if lane.done:
            break
        try:
            got = lane.step(5.0, 1.0)
        except ConnectionError:
            got = lane.take_ready()   # what the engine does on the error path
        owner.add_lane(rid, got, {"pos_ms": lane.pos_ms, "steps": []})
        owner.flush({}, {})
    assert snap(conn) == clean


def test_note_es_gone_skips_only_services_with_a_gone_pattern(conn, cfg):
    msg = "payment failed for user 42"
    src = FakeSource({"a": docs(10, service="x") + docs(10, service="z", prefix="za"),
                      "b": docs(10, service="y", prefix="yb") + docs(10, service="z", prefix="zb")})
    pats = {"a": ["x", "z"], "b": ["y", "z"]}
    owner, lane, rid = lane_of(conn, cfg, src, hour_to=11, patterns=pats)
    src.delete_index("b")   # retention
    drive(owner, lane, rid)
    assert {r["service"] for r in rows(conn, "SELECT service FROM service_hours")} == {"x"}
    assert lane.skipped == 2
    # z's lines were read from a, but its hour is not written: nothing may make the cache answer for it
    assert lane.cache.get("z", normalize(msg)) is None and lane.cache.get("x", normalize(msg)) is not None


def test_count_calls_are_per_pattern_span(ready):
    engine, clock, src = ready
    out = engine._run("retrain_estimate", Scope(None, [date(2026, 10, 13), D14], 0, 24).to_json())
    assert out["hours_to_read"] == 12 + 24 and out["hours_not_in_es"] == 12
    assert len(src.counts) == 1   # one pattern, one contiguous span
    assert src.counts[0][3] == engine.cfg.retrain.es_timeout_seconds
    src.counts.clear()
    engine._run("retrain_run", YESTERDAY.to_json())
    assert len(src.counts) == 1


# --- the engine --------------------------------------------------------------

D14 = date(2026, 10, 14)
YESTERDAY = Scope(None, [D14], 10, 14)   # 10:00-14:00 local, yesterday: hours 10, 11, 12, 13


def at(day, hour, minute=0):
    from datetime import datetime
    from logai2.timeutil import TZ
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp() * 1000)


@pytest.fixture
def ready(conn, cfg, make):
    """A caught-up engine with 2 days of history (2 docs a minute, service s) and one late doc per hour of
    YESTERDAY in ES that live never saw."""
    cfg.start.days_back = 2
    cfg.flush.seconds = 0
    cfg.retrain.tick_seconds = 0.0   # one page per tick: one window per tick
    clock = Clock(NOON)
    src = FakeSource({"a": history(NOON - 2 * 86400, NOON - 60, 2)})
    engine = make(src, clock)
    engine.start()
    catch_up(engine, clock)
    for h in range(10, 14):
        src.hits["a"].append(hit(f"late{h}", at(D14, h, 30) + 500, service="s", level="ERROR",
                                 message="ERROR payment failed for user 42"))
    return engine, clock, src


def run_until_done(engine, clock, ticks=60):
    for _ in range(ticks):
        engine.tick()
        clock.t += 1
        if engine.reread is None:
            return
    raise AssertionError("the re-read never finished")


def run_row(conn, rid):
    row = q(conn, "SELECT * FROM retrain_runs WHERE id = %s", rid)[0]
    return {**row, "steps": {s["key"]: s for s in row["steps"]}}


def hour_totals(conn, day=D14):
    return {r["hour"]: r["sum"] for r in
            q(conn, "SELECT hour, sum FROM hourly_counts WHERE key = '*total' AND service = 's' AND day = %s", day)}


def test_reread_runs_end_to_end(conn, ready):
    engine, clock, src = ready
    assert hour_totals(conn)[10] == 120
    result = engine._run("retrain_run", YESTERDAY.to_json())
    rid = result["run_id"]
    run_until_done(engine, clock)
    row = run_row(conn, rid)
    assert row["state"] == "succeeded" and row["kind"] == "reread" and row["trigger"] == "manual"
    assert set(row["steps"]) == {"scope", "partitions", "read", "rebuild", "mark", "housekeeping"}
    scope = row["steps"]["scope"]["detail"]
    assert (scope["services"], scope["patterns"], scope["hours_chosen"], scope["hours_to_read"]) == (1, 1, 4, 4)
    assert (scope["hours_not_in_es"], scope["hours_live"]) == (0, 0)
    read = row["steps"]["read"]["detail"]
    assert read["hours_done"] == read["hours_total"] == 4 and read["docs_read"] == 4 * 121
    assert read["docs_target"] == 4 * 121 and read["pct"] == 100
    for key in ("new_templates", "rate", "position_ms", "live_lag_s", "paused", "eta_s"):
        assert key in read
    assert row["steps"]["read"]["finished_at"] is not None
    assert row["result"]["hours_replaced"] == 4 and row["result"]["docs_read"] == 4 * 121
    assert row["result"]["baseline_rows"] > 0 and row["result"]["hours_not_in_es"] == 0
    assert row["pos_ms"] == at(D14, 14)
    t = hour_totals(conn)
    assert (t[9], t[10], t[11], t[12], t[13], t[14]) == (120, 121, 121, 121, 121, 120)
    assert q(conn, "SELECT n_minutes FROM service_hours WHERE day = %s AND hour = 11", D14)[0]["n_minutes"] == 60
    assert engine.reread is None


def test_reread_cancel_keeps_done_hours_no_rebuild(conn, ready):
    engine, clock, src = ready
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    for _ in range(3):   # a step per window
        engine.tick()
        clock.t += 1
    assert engine._run("retrain_cancel", {"run_id": rid}) == {}
    assert engine.reread is None
    row = run_row(conn, rid)
    assert row["state"] == "cancelled" and "rebuild" not in row["steps"]
    t = hour_totals(conn)
    done = [h for h in (10, 11, 12, 13) if t[h] == 121]
    assert 1 <= len(done) < 4 and all(t[h] == 120 for h in (10, 11, 12, 13) if h not in done)
    for _ in range(3):   # nothing goes on
        engine.tick()
    assert hour_totals(conn) == t


def test_cancel_unknown_run(ready):
    engine, clock, src = ready
    assert "error" in engine._run("retrain_cancel", {"run_id": 12345})


def test_reread_es_error_fails_with_step(conn, ready):
    engine, clock, src = ready
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    live = src.page

    def page(target, from_ms, upper_ms, timeout=None):
        if timeout is not None:   # only the lane passes a timeout
            raise ConnectionError("es is down")
        return live(target, from_ms, upper_ms)
    src.page = page
    for _ in range(3):
        engine.tick()
        clock.t += 1
    assert engine.reread is None
    row = run_row(conn, rid)
    assert row["state"] == "failed" and row["failed_step"] == "read" and "es is down" in row["error"]
    assert row["error_type"] == "ConnectionError"
    assert row["steps"]["read"]["finished_at"] is not None and "error" in row["steps"]["read"]["detail"]
    assert "rebuild" not in row["steps"] and any("failed" in line["message"] for line in row["log"])
    assert hour_totals(conn)[10] == 120


def test_reread_survives_two_page_failures(conn, ready):
    engine, clock, src = ready
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    live = src.page
    fails = [2]

    def page(target, from_ms, upper_ms, timeout=None):
        if timeout is not None and fails[0] > 0:
            fails[0] -= 1
            raise ConnectionError("blip")
        return live(target, from_ms, upper_ms)
    src.page = page
    run_until_done(engine, clock)
    assert run_row(conn, rid)["state"] == "succeeded"


def test_warning_while_running_is_in_the_run_log_after_the_next_flush(conn, ready):
    engine, clock, src = ready
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    logging.getLogger("logai2.test").warning("slow page while reading")
    engine.tick()
    clock.t += 1
    engine.tick()
    assert engine.reread is not None   # still running
    log = run_row(conn, rid)["log"]
    assert any(l["level"] == "WARNING" and "slow page while reading" in l["message"] for l in log)


def test_lane_waits_while_live_lags(conn, cfg, ready):
    engine, clock, src = ready
    cfg.retrain.max_live_lag_seconds = 10   # live reads up to 30 s behind: always "lagging"
    engine._run("retrain_run", YESTERDAY.to_json())
    for _ in range(4):
        engine.tick()
        clock.t += 1
    read = engine.reread["read"]
    assert read["docs_read"] == 0 and read["paused"] >= 4
    cfg.retrain.max_live_lag_seconds = 60
    run_until_done(engine, clock)
    assert hour_totals(conn)[10] == 121


def test_lane_waits_until_live_is_caught_up(ready):
    engine, clock, src = ready
    engine._run("retrain_run", YESTERDAY.to_json())
    engine._positions = {i: {**p, "caught_up": False} for i, p in engine._positions.items()}
    engine._lane_tick(clock.t)
    assert engine.reread["read"]["docs_read"] == 0 and engine.reread["read"]["paused"] == 1


def test_second_run_rejected(conn, ready):
    engine, clock, src = ready
    first = engine._run("retrain_run", YESTERDAY.to_json())
    assert "run_id" in first
    assert engine._run("retrain_run", YESTERDAY.to_json()) == {"error": "a retrain is running"}
    assert q(conn, "SELECT count(*) AS n FROM retrain_runs WHERE kind = 'reread'")[0]["n"] == 1


def test_nothing_to_reread_makes_no_run(conn, ready):
    engine, clock, src = ready
    out = engine._run("retrain_run", Scope(None, [date(2026, 10, 10)], 0, 24).to_json())   # before ES's oldest doc
    assert out["error"] == "nothing to re-read" and out["hours_not_in_es"] == 24 and out["hours_to_read"] == 0
    assert q(conn, "SELECT count(*) AS n FROM retrain_runs WHERE kind = 'reread'")[0]["n"] == 0


def test_owner_hour_is_never_replaced(conn, ready):
    engine, clock, src = ready
    today = Scope(None, [date(2026, 10, 15)], 9, 14)   # now is 12:00: live hour is 11:00 (live closed to 11:59)
    out = engine._run("retrain_run", today.to_json())
    run_until_done(engine, clock)
    row = run_row(conn, out["run_id"])
    assert row["steps"]["scope"]["detail"]["hours_live"] == 3 and row["steps"]["scope"]["detail"]["hours_to_read"] == 2
    assert row["result"]["hours_replaced"] == 2


def test_reread_crash_resumes_exact_engine(conn, cfg, ready, make):
    engine, clock, src = ready
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    for _ in range(3):   # windows 1-2 flushed, 3 queued in memory only
        engine.tick()
        clock.t += 1
    assert 0 < run_row(conn, rid)["pos_ms"] < at(D14, 14)
    engine.db.close()
    again = make(src, clock)   # a new process: everything comes from the DB
    again.start()
    assert again.reread is not None and again.reread["run_id"] == rid
    assert "resumed_at" in again.reread["read"]
    run_until_done(again, clock)
    row = run_row(conn, rid)
    assert row["state"] == "succeeded" and row["result"]["hours_replaced"] == 4
    t = hour_totals(conn)
    assert (t[10], t[11], t[12], t[13]) == (121, 121, 121, 121)
    assert q(conn, "SELECT count(*) AS n FROM service_hours WHERE day = %s AND hour BETWEEN 10 AND 13", D14)[0]["n"] == 4
    assert q(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE key = '*total' AND day = %s", D14)[0]["n"] == 24 * 120 + 4


def test_lane_error_after_closed_windows_keeps_them(conn, cfg, ready):
    engine, clock, src = ready
    cfg.retrain.tick_seconds = 5.0   # every window closes in one step
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    live, calls = src.page, [0]

    def page(target, from_ms, upper_ms, timeout=None):
        if timeout is not None:
            calls[0] += 1
            if calls[0] >= 2:
                raise ConnectionError("es is down")
        return live(target, from_ms, upper_ms)
    src.page = page
    for _ in range(3):
        engine.tick()
        clock.t += 1
    row = run_row(conn, rid)
    assert row["state"] == "failed" and row["failed_step"] == "read"
    t = hour_totals(conn)
    assert (t[10], t[11], t[12], t[13]) == (121, 120, 120, 120)   # the closed hour is written, the failed ones not
    assert row["result"]["hours_replaced"] == 1 and row["pos_ms"] == at(D14, 11)


def test_lane_single_error_after_closed_windows_still_replaces_all(conn, cfg, ready):
    engine, clock, src = ready
    cfg.retrain.tick_seconds = 5.0
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    live, calls = src.page, [0]

    def page(target, from_ms, upper_ms, timeout=None):
        if timeout is not None:
            calls[0] += 1
            if calls[0] == 3:
                raise ConnectionError("blip")
        return live(target, from_ms, upper_ms)
    src.page = page
    run_until_done(engine, clock)
    row = run_row(conn, rid)
    assert row["state"] == "succeeded" and row["result"]["hours_replaced"] == 4
    t = hour_totals(conn)
    assert (t[10], t[11], t[12], t[13]) == (121, 121, 121, 121)


def test_resume_survives_a_transient_es_error(conn, cfg, ready, make):
    engine, clock, src = ready
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    engine.tick()
    engine.db.close()
    src.oldest_failing = True
    again = make(src, clock)
    again.start()   # the plan cannot be made: the run is neither failed nor lost
    assert again.reread is None and run_row(conn, rid)["state"] == "running"
    assert again._run("retrain_run", YESTERDAY.to_json()) == {"error": "a retrain is running"}
    src.oldest_failing = False
    again.tick()   # too early to retry
    assert again.reread is None
    clock.t += cfg.elasticsearch.index_refresh_seconds + 1
    again.tick()
    assert again.reread is not None and again.reread["run_id"] == rid
    run_until_done(again, clock)
    assert run_row(conn, rid)["state"] == "succeeded"


def test_resume_does_not_count_the_partial_window_twice(conn, cfg, ready, make):
    engine, clock, src = ready
    src.size = cfg.elasticsearch.batch_size = 50   # 3 pages per window, one page per tick
    rid = engine._run("retrain_run", YESTERDAY.to_json())["run_id"]
    for _ in range(5):   # window 1 closed, window 2 part read, and that progress flushed
        engine.tick()
        clock.t += 1
    row = run_row(conn, rid)
    assert row["pos_ms"] == at(D14, 11) and row["steps"]["read"]["detail"]["docs_read"] > 121
    engine.db.close()
    again = make(src, clock)
    again.start()
    run_until_done(again, clock)
    row = run_row(conn, rid)
    assert row["state"] == "succeeded" and row["result"]["docs_read"] == 4 * 121
    assert row["steps"]["read"]["detail"]["docs_read"] == 4 * 121


def test_estimate_counts_docs(conn, ready):
    engine, clock, src = ready
    out = engine._run("retrain_estimate", YESTERDAY.to_json())
    assert out["docs"] == 4 * 121 and out["hours_to_read"] == 4 and out["services"] == 1 and out["patterns"] == 1
    assert out["hours_not_in_es"] == 0 and out["hours_live"] == 0 and out["indices"] == 1
    assert out["eta_s"] == pytest.approx(4 * 121 / engine.cfg.retrain.default_rate)
    assert q(conn, "SELECT count(*) AS n FROM retrain_runs WHERE kind = 'reread'")[0]["n"] == 0
    assert engine.reread is None


def test_commands_are_registered(conn):
    for kind in ("retrain_run", "retrain_cancel", "retrain_estimate"):
        assert kind in commands.KINDS

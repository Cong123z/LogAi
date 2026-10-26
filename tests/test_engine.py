"""Engine over FakeSource, real Postgres and a fake clock (2026-10-15 is a Thursday)."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from logai2 import commands
from logai2.db import get_setting, put_setting
from logai2.engine import Engine
from logai2.metrics import Metrics
from logai2.timeutil import TZ
from tests.conftest import TEST_DSN
from tests.fakes import FakeSource, hit

NOON = datetime(2026, 10, 15, 12, 0, tzinfo=TZ).timestamp()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def history(start_s, end_s, per_min, msg="ERROR payment failed for user 42", level="ERROR", prefix="h"):
    """per_min hits in every minute of [start_s, end_s)."""
    out = []
    for m in range(int(start_s) // 60, int(end_s) // 60):
        out += [hit(f"{prefix}{m}-{i}", m * 60_000 + i * 1000, service="s", level=level, message=msg)
                for i in range(per_min)]
    return out


@pytest.fixture
def make(conn, cfg):
    cfg.database.dsn = TEST_DSN
    cfg.elasticsearch.index = "a"
    made = []

    def build(src, clock, metrics=None):
        e = Engine(cfg, conn, src, now=clock, metrics=metrics)
        e.sleeps = []

        def sleep(s):
            e.sleeps.append(s)
            assert len(e.sleeps) < 10, "flush keeps failing"
        e.sleep = sleep
        made.append(e)
        return e
    yield build
    for e in made:
        e.db.close()


def q(conn, sql, *args):
    rows = conn.execute(sql, args or None).fetchall()
    conn.rollback()
    return rows


def setting(conn, key):
    value = get_setting(conn, key)
    conn.rollback()
    return value


def catch_up(engine, clock, ticks=10):
    for _ in range(ticks):
        engine.tick()
        if engine.owner.detection_enabled:
            return
        clock.t += 1
    raise AssertionError("never caught up")


def test_first_start_reads_days_back_then_enables_detection(conn, cfg, make):
    cfg.start.days_back = 2   # the history below is 2 days
    clock = Clock(NOON)
    src = FakeSource({"a": history(NOON - 2 * 86400, NOON - 60, 1)})
    engine = make(src, clock)
    engine.start()
    assert setting(conn, "detection_enabled") is None
    assert setting(conn, "bootstrap") == {"since": NOON - 2 * 86400, "until": NOON}
    catch_up(engine, clock)
    assert setting(conn, "detection_enabled") is True
    assert setting(conn, "last_commit")["today"] == "2026-10-15"
    assert q(conn, "SELECT count(*) AS n FROM baseline")[0]["n"] > 0
    assert q(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE key = '*total'")[0]["n"] == 2 * 1440 - 1
    status = q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]
    assert status["detection_enabled"] and status["indices"]["a"]["caught_up"]
    assert status["indices"]["a"]["floor"] == (NOON - 2 * 86400) * 1000
    assert status["resolution"] == {"a": ["a"]} and status["available_indices"] == [{"index": "a"}]


def test_first_catch_up_creates_partitions_outside_test_range(conn, cfg, make):
    # conftest creates October only: these days have no partitions until the engine makes them
    now = datetime(2026, 11, 20, 12, 0, tzinfo=TZ).timestamp()
    clock = Clock(now)
    engine = make(FakeSource({"a": history(now - 2 * 86400, now - 60, 1)}), clock)
    engine.start()
    catch_up(engine, clock)
    days = [r["day"] for r in q(conn, "SELECT DISTINCT day FROM hourly_counts ORDER BY day")]
    assert days == [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)]
    assert engine.sleeps == []


def test_burst_after_commit_alerts_within_3_minutes(conn, cfg, make):
    cfg.flush.seconds = 0
    clock = Clock(NOON)
    hits = history(NOON - 2 * 86400, NOON - 60, 3)
    src = FakeSource({"a": hits})
    metrics = Metrics(None)
    engine = make(src, clock, metrics)
    engine.start()
    catch_up(engine, clock)
    for k in range(1, 4):   # burst minutes 12:01, 12:02, 12:03
        start = NOON + k * 60
        hits += history(start, start + 60, 40, prefix="b")
        clock.t = start + 60 + 31   # watermark now - 30 s passes the minute's end
        engine.tick()
        engine.tick()
    state = q(conn, "SELECT state FROM alert_state WHERE key = 's:T00001'")[0]["state"]
    assert state == "ALERTING"
    assert q(conn, "SELECT count(*) AS n FROM alert_changes WHERE key = 's:T00001'")[0]["n"] >= 2
    value = metrics.registry.get_sample_value(
        "log_alert_state", {"service": "s", "doc_id": "undocumented", "state": "ALERTING"})
    assert value == 1
    assert metrics.registry.get_sample_value(
        "log_alert_state", {"service": "s", "doc_id": "undocumented", "state": "NORMAL"}) == 0
    assert metrics.registry.get_sample_value("logai_events_total") == len(hits)


def test_missed_commit_runs_on_start(conn, cfg, make):
    cfg.start.days_back = 0
    put_setting(conn, "detection_enabled", True)
    put_setting(conn, "last_commit", {"today": "2026-10-13"})
    engine = make(FakeSource({"a": []}), Clock(datetime(2026, 10, 15, 3, 0, tzinfo=TZ).timestamp()))
    engine.start()
    assert setting(conn, "last_commit")["today"] == "2026-10-15"


def test_no_commit_before_commit_time(conn, cfg, make):
    cfg.start.days_back = 0
    put_setting(conn, "detection_enabled", True)
    put_setting(conn, "last_commit", {"today": "2026-10-13"})
    engine = make(FakeSource({"a": []}), Clock(datetime(2026, 10, 15, 1, 0, tzinfo=TZ).timestamp()))
    engine.start()
    assert setting(conn, "last_commit")["today"] == "2026-10-13"


def test_daily_commit_uses_owner_day_not_wall_day(conn, cfg, make):
    clock = Clock(NOON)
    src = FakeSource({"a": history(NOON - 2 * 86400, NOON - 60, 1)})
    engine = make(src, clock)
    engine.start()
    catch_up(engine, clock)
    src.fail("a")   # the index lags: its position stays on 10-15 while the wall clock is 10-16 02:01
    clock.t = datetime(2026, 10, 16, 2, 1, tzinfo=TZ).timestamp()
    engine.tick()
    assert setting(conn, "last_commit")["today"] == "2026-10-15"
    engine.tick()   # healthy again and caught up: the owner reaches 10-16
    assert setting(conn, "last_commit")["today"] == "2026-10-16"


def test_recompute_command(conn, cfg, make):
    cfg.start.days_back = 0
    engine = make(FakeSource({"a": []}), Clock(NOON))
    engine.start()
    commands.enqueue(conn, "recompute_baseline")
    engine.tick()
    assert conn.execute("SELECT result FROM commands").fetchone()["result"]["days_committed"] >= 0


def runs_of(conn, trigger=None):
    return q(conn, "SELECT * FROM retrain_runs WHERE %s::text IS NULL OR trigger = %s ORDER BY id",
             trigger, trigger)


def test_rebuild_run_recorded(conn, cfg, make):
    cfg.start.days_back = 0
    engine = make(FakeSource({"a": []}), Clock(NOON))
    engine.start()
    commands.enqueue(conn, "recompute_baseline")
    engine.tick()
    (run,) = runs_of(conn, "manual")
    assert (run["kind"], run["trigger"], run["state"]) == ("rebuild", "manual", "succeeded")
    assert [s["key"] for s in run["steps"]] == ["mark", "rebuild", "housekeeping"]
    assert all(s["finished_at"] for s in run["steps"]) and run["finished_at"]
    assert "baseline_rows" in run["result"]
    res = q(conn, "SELECT result FROM commands")[0]["result"]
    assert res["run_id"] == run["id"] and "days_committed" in res and "baseline_rows" in res


def test_failed_rebuild_records_error_and_step(conn, cfg, make, monkeypatch):
    cfg.start.days_back = 0
    put_setting(conn, "detection_enabled", True)
    put_setting(conn, "last_commit", {"today": "2026-10-15"})
    engine = make(FakeSource({"a": []}), Clock(NOON))
    engine.start()
    before = q(conn, "SELECT count(*) AS n FROM baseline")[0]["n"]
    monkeypatch.setattr("logai2.jobs.commit.REBUILD", "SELECT * FROM no_such_table")
    commands.enqueue(conn, "recompute_baseline")
    engine.tick()
    (run,) = runs_of(conn, "manual")
    assert (run["state"], run["failed_step"], run["trigger"]) == ("failed", "rebuild", "manual")
    assert run["error_type"] and run["error"] and run["finished_at"]
    assert "error" in q(conn, "SELECT result FROM commands")[0]["result"]
    assert [(s["key"], s["finished_at"] is not None) for s in run["steps"]] == [("mark", True), ("rebuild", True)]
    assert run["steps"][1]["detail"] == {}
    assert q(conn, "SELECT count(*) AS n FROM baseline")[0]["n"] == before


def test_triggers_recorded(conn, cfg, make):
    cfg.start.days_back = 2
    clock = Clock(NOON)
    engine = make(FakeSource({"a": history(NOON - 2 * 86400, NOON - 60, 1)}), clock)
    engine.start()
    catch_up(engine, clock)
    assert [r["trigger"] for r in runs_of(conn)] == ["first_start"]
    clock.t = datetime(2026, 10, 16, 2, 1, tzinfo=TZ).timestamp()
    engine.tick()
    assert [r["trigger"] for r in runs_of(conn)] == ["first_start", "schedule"]
    cfg.start.days_back = 0
    put_setting(conn, "last_commit", {"today": "2026-10-13"})
    e2 = make(FakeSource({"a": []}), Clock(datetime(2026, 10, 17, 3, 0, tzinfo=TZ).timestamp()))
    e2.start()
    assert [r["trigger"] for r in runs_of(conn)][-1] == "startup"


def test_bad_command_records_error_and_engine_continues(conn, cfg, make):
    cfg.start.days_back = 0
    engine = make(FakeSource({"a": []}), Clock(NOON))
    engine.start()
    bad = commands.enqueue(conn, "explode")
    good = commands.enqueue(conn, "reload_links")
    engine.tick()
    results = {r["id"]: r["result"] for r in q(conn, "SELECT id, result FROM commands")}
    assert "error" in results[bad] and results[good] == {}


def test_reload_sources_reconnects_and_reresolves(conn, cfg, make):
    cfg.start.days_back = 0
    src = FakeSource({"a": [], "b": []})
    engine = make(src, Clock(NOON))
    engine.start()
    put_setting(conn, "es_connection", {"hosts": ["http://other:9200"], "username": "u", "password": "p",
                                        "verify_certs": False})
    put_setting(conn, "index_selection", {"entries": [{"pattern": "*", "added_at": 0}]})
    commands.enqueue(conn, "reload_sources")
    engine.tick()
    assert src.connected.hosts == ["http://other:9200"] and src.connected.verify_certs is False
    assert set(engine.reader.floors) == {"*"}


def test_index_added_later_starts_at_added_at(conn, cfg, make):
    added = NOON + 3600
    put_setting(conn, "bootstrap", {"since": NOON - 86400, "until": NOON})
    put_setting(conn, "index_selection", {"entries": [{"pattern": "a", "added_at": 0},
                                                      {"pattern": "b", "added_at": added}]})
    src = FakeSource({"a": [], "b": history(added - 600, added + 600, 1)})
    engine = make(src, Clock(added + 3600))
    engine.start()
    assert dict(engine.reader.floors) == {"a": (NOON - 86400) * 1000, "b": added * 1000}
    engine.tick()
    engine.tick()
    assert q(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE key = '*total'")[0]["n"] == 10


def test_flush_retry_does_not_poll(conn, cfg, make, monkeypatch):
    cfg.start.days_back = 0
    cfg.flush.seconds = 0
    src = FakeSource({"a": []})
    engine = make(src, Clock(NOON))
    engine.start()
    pages = []
    monkeypatch.setattr(src, "page", lambda *a: pages.append(a) or [])
    real = engine.owner.flush
    fails = [RuntimeError("db down")]

    def flaky(*a):
        if fails:
            raise fails.pop()
        real(*a)
    monkeypatch.setattr(engine.owner, "flush", flaky)
    engine.tick()
    assert len(pages) == 1 and engine.sleeps == [1]


def test_seed_documents(conn, cfg, make, tmp_path):
    cfg.start.days_back = 0
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"entries": [{"id": "D1", "title": "Pay", "text": "body", "error_code": "E1"}]}))
    cfg.documentation.seed_path = str(path)
    make(FakeSource({"a": []}), Clock(NOON)).start()
    assert q(conn, "SELECT doc_id, title, handling, error, error_code FROM documents") == [
        {"doc_id": "D1", "title": "Pay", "handling": "body", "error": "", "error_code": "E1"}]


def test_restart_after_long_downtime_creates_partitions(conn, cfg, make):
    # first run on 11-05 creates 11-05..11-07; the restart on 11-12 resumes from the 11-05 cursor
    cfg.start.days_back = 0
    first = datetime(2026, 11, 5, 12, 0, tzinfo=TZ).timestamp()
    clock = Clock(first)
    hits = []
    src = FakeSource({"a": hits})
    engine = make(src, clock)
    engine.start()
    catch_up(engine, clock)
    restart = datetime(2026, 11, 12, 12, 0, tzinfo=TZ).timestamp()
    hits += [hit(f"d{m}", m * 60_000, service="s", message="nightly job ok")
             for m in range(int(first) // 60 + 5, int(restart) // 60 - 5, 30)]
    clock2 = Clock(restart)
    engine2 = make(src, clock2)
    engine2.start()
    engine2.tick()
    clock2.t += 1
    engine2.tick()
    days = [r["day"] for r in q(conn, "SELECT DISTINCT day FROM hourly_counts ORDER BY day")]
    assert days == [date(2026, 11, d) for d in range(5, 13)]
    assert engine2.sleeps == []


def test_miss_ratio_warns_only_after_5_minutes_of_data(conn, cfg, make):
    cfg.start.days_back = 0
    cfg.flush.seconds = 0
    cfg.reader.cache_size = 0   # every line misses
    clock = Clock(NOON)
    hits = []
    engine = make(FakeSource({"a": hits}), clock)
    engine.start()

    def warnings():
        return q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]["warnings"]
    for k in range(7):
        start = NOON + k * 60
        hits += history(start, start + 60, 200 if k == 0 else 30, prefix=f"k{k}-")
        clock.t = start + 60 + 31
        engine.tick()
        if k == 0:
            assert warnings() == []   # cold cache right after start: no warning yet
    assert warnings() == ["high miss ratio: s 100%"]


def test_first_catch_up_completes_under_continuous_traffic(conn, cfg, make):
    # new docs every poll on a busy index: a short page still counts as caught up
    cfg.elasticsearch.batch_size = 1000
    clock = Clock(NOON)
    hits = history(NOON - 2 * 86400, NOON - 60, 1)
    src = FakeSource({"a": hits})
    src.size = 1000
    engine = make(src, clock)
    engine.start()
    for k in range(10):
        hits.append(hit(f"live{k}", int((clock.t - 31) * 1000), service="s", message="live"))
        engine.tick()
        if engine.owner.detection_enabled:
            break
        clock.t += 1
    assert engine.owner.detection_enabled and k >= 2   # 2880 backlog docs took 3 pages


def test_first_catch_up_ignores_failing_index(conn, cfg, make, monkeypatch):
    put_setting(conn, "index_selection", {"entries": [{"pattern": "a", "added_at": 0},
                                                      {"pattern": "b", "added_at": 0}]})
    clock = Clock(NOON)
    src = FakeSource({"a": history(NOON - 2 * 86400, NOON - 60, 1), "b": []})
    real = src.page
    monkeypatch.setattr(src, "page", lambda index, *a: (_ for _ in ()).throw(ConnectionError("b down"))
                        if index.split(",")[0] == "b" else real(index, *a))
    engine = make(src, clock)
    engine.start()
    catch_up(engine, clock)
    status = q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]
    assert status["indices"]["b"]["failing"] and status["indices"]["a"]["caught_up"]


def test_start_waits_for_elasticsearch(conn, cfg, make, monkeypatch):
    src = FakeSource({"a": []})
    real = src.resolve
    fails = [ConnectionError("es down"), ConnectionError("es down")]
    monkeypatch.setattr(src, "resolve", lambda p: (_ for _ in ()).throw(fails.pop()) if fails else real(p))
    engine = make(src, Clock(NOON))
    warned = []

    def sleep(s):
        put_setting(conn, "es_connection", {"hosts": ["http://fixed:9200"]})
        warned.append((s, q(
            conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]["warnings"]))
    engine.sleep = sleep
    engine.start()
    assert warned == [(1, ["Elasticsearch unreachable: es down"]), (2, ["Elasticsearch unreachable: es down"])]
    assert src.connected.hosts == ["http://fixed:9200"]   # settings saved from the web while waiting
    assert set(engine.reader.floors) == {"a"}


def test_start_logs_loaded_alerting_states(conn, cfg, make, caplog):
    cfg.start.days_back = 0
    conn.execute("INSERT INTO alert_state (service, key, state, since, streak, score, count) "
                 "VALUES ('s', 's:T1', 'ALERTING', now(), 3, 2.0, 9), ('s', 's:T2', 'NORMAL', now(), 0, 0, 0)")
    conn.commit()
    with caplog.at_level("WARNING", logger="logai2.engine"):
        make(FakeSource({"a": []}), Clock(NOON)).start()
    assert "1 non-NORMAL alert state" in caplog.text


def _run_minutes(engine, clock, hits, minutes):
    for k in range(minutes):
        start = NOON + k * 60
        hits += history(start, start + 60, 30, prefix=f"k{k}-")
        clock.t = start + 60 + 31
        engine.tick()


def test_status_history_and_flush_fields(conn, cfg, make):
    cfg.start.days_back = 0
    cfg.flush.seconds = 0
    clock = Clock(NOON)
    hits = []
    engine = make(FakeSource({"a": hits}), clock)
    engine.start()
    _run_minutes(engine, clock, hits, 3)
    s = q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]
    assert 2 <= len(s["history"]) <= 60 and set(s["history"][0]) == {"t", "lag", "eps"}
    assert s["history"][-1]["t"] % 60 == 0 and s["events_per_second_15m"] > 0
    assert s["flush_seconds"] >= 0 and s["flush_seconds_max"] >= s["flush_seconds"] and s["late_counts"] == 0


def test_status_has_service_minutes(conn, cfg, make):
    cfg.start.days_back = 0
    cfg.flush.seconds = 0
    clock = Clock(NOON)
    hits = []
    engine = make(FakeSource({"a": hits}), clock)
    engine.start()
    _run_minutes(engine, clock, hits, 3)
    sm = q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]["service_minutes"]
    (points,) = sm.values()
    assert points and all(t % 60 == 0 and n > 0 for t, n in points)
    assert [t for t, _ in points] == sorted(t for t, _ in points)


def test_history_keeps_60_minutes(conn, cfg, make):
    cfg.start.days_back = 0
    cfg.flush.seconds = 0
    clock = Clock(NOON)
    hits = []
    engine = make(FakeSource({"a": hits}), clock)
    engine.start()
    _run_minutes(engine, clock, hits, 70)
    h = q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]["history"]
    assert len(h) == 60 and h[0]["t"] < h[-1]["t"]


def test_upgrade_from_index_cursors(conn, cfg, make):
    # rows from before the upgrade are keyed by concrete index and all hold the minute boundary
    b = (NOON - 3600) * 1000
    put_setting(conn, "bootstrap", {"since": NOON - 86400, "until": NOON})
    put_setting(conn, "index_selection", {"entries": [{"pattern": "a", "added_at": 0}]})
    conn.execute("INSERT INTO es_cursors (index, since_ms, floor_ms) VALUES ('a-old-1', %s, 1), ('a-old-2', %s, 1)", (b, b))
    conn.commit()
    engine = make(FakeSource({"a": []}), Clock(NOON))
    engine.start()
    assert engine.reader._pos["a"] == b


def test_empty_pattern_caught_up_with_warning(conn, cfg, make):
    put_setting(conn, "index_selection", {"entries": [{"pattern": "nope-*", "added_at": 0}]})
    clock = Clock(NOON)
    engine = make(FakeSource({"a": []}), clock)
    engine.start()
    engine.tick()
    assert engine._caught_up()
    assert "no index matches nope-*" in engine._status()["warnings"]


def _started(conn, cfg, make):
    cfg.start.days_back = 0
    put_setting(conn, "detection_enabled", True)
    put_setting(conn, "last_commit", {"today": "2026-10-15"})
    engine = make(FakeSource({"a": []}), Clock(NOON))
    engine.start()
    return engine


def test_run_recording_failure_does_not_break_commit(conn, cfg, make, monkeypatch):
    engine = _started(conn, cfg, make)
    reloads = []
    monkeypatch.setattr(engine.owner, "reload_baseline", lambda: reloads.append(1))

    def boom(*a, **kw):
        raise RuntimeError("runs down")
    monkeypatch.setattr("logai2.jobs.runs.step", boom)
    monkeypatch.setattr("logai2.jobs.runs.finish", boom)
    result = engine._commit("manual")
    assert "days_committed" in result and reloads == [1]
    assert engine.last_commit["today"] == "2026-10-15" and engine.last_commit["at"]
    assert not engine.db.info.transaction_status.value   # idle
    assert [r["state"] for r in runs_of(conn, "manual")] == ["running"]


def test_run_start_failure_still_commits(conn, cfg, make, monkeypatch):
    engine = _started(conn, cfg, make)

    def boom(*a, **kw):
        raise RuntimeError("runs down")
    monkeypatch.setattr("logai2.jobs.runs.start", boom)
    assert "days_committed" in engine._commit("manual")
    assert runs_of(conn, "manual") == []

"""Owner + Reader + FakeSource against real Postgres: counts are exact across crashes and failed flushes."""
from __future__ import annotations

from datetime import date, datetime, timezone

import zlib

import psycopg
import pytest

from logai2.db import connect, ensure_partitions, put_setting
from logai2.owner.owner import Owner
from logai2.reader.cache import KnownLines
from logai2.reader.reader import Reader
from tests.conftest import TEST_DSN
from tests.fakes import FakeSource, hit

T0 = int(datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 10:00 local, Tuesday
M = T0 // 60_000


def rows(conn, q, *args):
    out = conn.execute(q, args or None).fetchall()
    conn.rollback()   # flush needs an idle connection
    return out


def make(conn, cfg, src, now_s, floor_ms=T0):
    """A fresh Reader/Owner pair built from what the database holds, as after a restart."""
    saved = {r["index"]: r["since_ms"] for r in rows(conn, "SELECT index, since_ms FROM es_cursors")}
    reader = Reader(src, cfg, KnownLines(1000), now=lambda: now_s)
    reader.set_sources({"a": floor_ms}, saved)
    owner = Owner(conn, cfg, saved.get("a", floor_ms) // 60_000, now=lambda: now_s)
    return reader, owner


def tick(reader, owner, flush=True):
    batch = reader.poll()
    reader.learn(owner.handle(batch))
    if flush:
        owner.flush(reader.cursors(owner.boundary_ms), {"events": batch["events"]})
        reader.forget_before(owner.boundary_ms)
    return batch


def run_minutes(conn, cfg, hits, until_ms, floor_ms=T0, src=None):
    """Reader→Owner→flush until the watermark passes until_ms (every minute before it is closed)."""
    src = src or FakeSource({"a": hits})
    reader, owner = make(conn, cfg, src, until_ms / 1000 + 60, floor_ms)
    for _ in range(20):
        if tick(reader, owner)["watermark_ms"] >= until_ms:
            return reader, owner
    raise AssertionError("watermark never passed until_ms")


def lines(minute, n, msg="payment failed for user 42", start=0, **src):
    return [hit(f"{minute}-{start + i}", T0 + minute * 60_000 + 1000 * (start + i), service="s", message=msg, **src)
            for i in range(n)]


def counts(conn):
    return [(r["sum"], r["sumsq"]) for r in
            rows(conn, "SELECT key, sum, sumsq FROM hourly_counts WHERE key LIKE 's:%%' ORDER BY key")]


def test_counts_exact(conn, cfg):
    run_minutes(conn, cfg, lines(0, 3) + lines(1, 2), T0 + 180_000)
    assert counts(conn) == [(5, 13)]
    assert rows(conn, "SELECT n_minutes FROM service_hours")[0]["n_minutes"] == 3
    total = rows(conn, "SELECT sum, sumsq FROM hourly_counts WHERE key = '*total'")[0]
    assert (total["sum"], total["sumsq"]) == (5, 13)
    (t,) = rows(conn, "SELECT * FROM templates")
    assert t["template_id"] == "s:T00001" and t["level"] == "INFO"
    assert t["last_seen"] == datetime.fromtimestamp((M + 1) * 60, timezone.utc)
    assert rows(conn, "SELECT partition FROM services WHERE service = 's'")[0]["partition"] == zlib.crc32(b"s") % 60
    assert rows(conn, "SELECT count(*) AS n FROM drain3_state")[0]["n"] == 1


def test_service_minutes_keeps_last_60_closed_minutes(conn, cfg):
    hits = [h for m in range(61) for h in lines(m, m % 50 + 1)]
    _, owner = run_minutes(conn, cfg, hits, T0 + 61 * 60_000)
    sm = list(owner.service_minutes["s"])
    assert len(sm) == 60 and sm[0] == (M + 1, 2) and sm[-1] == (M + 60, 11)


def test_crash_between_flushes_is_exact(conn, cfg):
    src = FakeSource({"a": lines(0, 3) + lines(1, 1)})
    reader, owner = make(conn, cfg, src, T0 / 1000 + 120)        # upper = T0 + 90 s
    tick(reader, owner)                                          # M closes and is flushed
    assert counts(conn) == [(3, 9)]
    src.hits["a"] += lines(1, 1, start=1) + lines(2, 1)
    reader.now = owner.now = lambda: T0 / 1000 + 240
    tick(reader, owner, flush=False)                             # M+1, M+2 close in memory only: crash
    run_minutes(conn, cfg, None, T0 + 180_000, src=src)          # restart from the database
    assert counts(conn) == [(6, 14)]                             # 3, 2, 1: no double count, no loss
    assert rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours")[0]["n"] == 3
    assert [r["template_id"] for r in rows(conn, "SELECT template_id FROM templates")] == ["s:T00001"]


def test_flush_failure_keeps_pending(conn, cfg, monkeypatch, schema):
    real = psycopg.Cursor.executemany

    def boom(self, query, params, **kw):
        if "es_cursors" in str(query):
            raise RuntimeError("disk full")
        return real(self, query, params, **kw)

    src = FakeSource({"a": lines(0, 3) + lines(1, 2)})
    reader, owner = make(conn, cfg, src, T0 / 1000 + 240)
    batch = tick(reader, owner, flush=False)
    batch = tick(reader, owner, flush=False)
    assert batch["watermark_ms"] >= T0 + 180_000
    monkeypatch.setattr(psycopg.Cursor, "executemany", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        owner.flush(reader.cursors(owner.boundary_ms), {})
    other = connect(TEST_DSN)
    try:
        other.execute(f"SET search_path TO {schema}, public")
        for table in ("hourly_counts", "templates", "services", "drain3_state", "es_cursors"):
            assert other.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0, table
    finally:
        other.close()
    monkeypatch.setattr(psycopg.Cursor, "executemany", real)
    owner.flush(reader.cursors(owner.boundary_ms), {})
    owner.flush(reader.cursors(owner.boundary_ms), {})           # nothing pending: writes nothing twice
    assert counts(conn) == [(5, 13)]
    assert rows(conn, "SELECT n_minutes FROM service_hours")[0]["n_minutes"] == 3
    assert rows(conn, "SELECT count(*) AS n FROM drain3_state")[0]["n"] == 1


def test_catch_up_days_back_writes_old_partitions(conn, cfg):
    # 2026-09-27 23:59 and 2026-09-28 00:00 local: before the test partitions, across a local midnight
    mid = int(datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)
    hits = [hit(str(i), mid + off, service="s", message="boot ok") for i, off in enumerate((-60_000, -50_000, 30_000))]
    reader, owner = make(conn, cfg, FakeSource({"a": hits}), mid / 1000 + 120, floor_ms=mid - 60_000)
    with pytest.raises(psycopg.Error):                           # closes 23:59: no partition for that day yet
        tick(reader, owner)
    ensure_partitions(conn, date(2026, 9, 27), date(2026, 9, 28))   # the engine does this at bootstrap
    assert tick(reader, owner)["watermark_ms"] >= mid + 60_000   # closes 00:00; one flush writes both days
    assert rows(conn, "SELECT day, hour, n_minutes FROM service_hours ORDER BY day") == [
        {"day": date(2026, 9, 27), "hour": 23, "n_minutes": 1}, {"day": date(2026, 9, 28), "hour": 0, "n_minutes": 1}]
    assert rows(conn, "SELECT day, hour, sum FROM hourly_counts WHERE key = '*total' ORDER BY day") == [
        {"day": date(2026, 9, 27), "hour": 23, "sum": 2}, {"day": date(2026, 9, 28), "hour": 0, "sum": 1}]


def test_cursor_is_open_minute_boundary(conn, cfg):
    src = FakeSource({"a": lines(0, 2) + lines(1, 2)})
    src.size = cfg.elasticsearch.batch_size = 4   # a full page: the position (minute 1) holds the watermark
    reader, owner = make(conn, cfg, src, T0 / 1000 + 600)
    batch = tick(reader, owner)
    (c,) = rows(conn, "SELECT * FROM es_cursors")
    assert c["since_ms"] == owner.boundary_ms == (M + 1) * 60_000
    assert c["since_ms"] <= batch["positions"]["a"]["pos_ms"] and c["floor_ms"] == T0
    assert rows(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"] == {"events": 4}


def test_error_samples_capped_at_20(conn, cfg):
    hits = [h for m in range(25) for h in lines(m, 1, msg=f"db timeout after {m} ms", level="ERROR")]
    hits += lines(0, 1, msg="all good")                          # INFO template: no samples
    run_minutes(conn, cfg, hits, T0 + 25 * 60_000)
    got = rows(conn, "SELECT template_id, at, line FROM error_samples ORDER BY at")
    assert len(got) == 20 and {r["template_id"] for r in got} == {"s:T00001"}
    assert got[0]["at"] == datetime.fromtimestamp((M + 5) * 60, timezone.utc)
    assert got[-1]["line"] == "db timeout after 24 ms"


def test_level_promoted_and_unparsed(conn, cfg, monkeypatch):
    src = FakeSource({"a": lines(0, 1, level="INFO") + lines(0, 1, start=1, level="ERROR")
                      + [hit("x", T0 + 5000, service="s", message="poison pill")]})
    reader, owner = make(conn, cfg, src, T0 / 1000 + 240)
    real = owner.miners.match
    monkeypatch.setattr(owner.miners, "match",
                        lambda s, t: (_ for _ in ()).throw(ValueError("bad")) if "poison" in t else real(s, t))
    for _ in range(2):
        tick(reader, owner)
    levels = {r["template_id"]: r["level"] for r in rows(conn, "SELECT template_id, level FROM templates")}
    assert levels == {"s:T00001": "ERROR", "s:unparsed": "INFO"}
    assert rows(conn, "SELECT sum FROM hourly_counts WHERE key = 's:unparsed'")[0]["sum"] == 1


def test_normalize_failure_counted_unparsed(conn, cfg, monkeypatch):
    from logai2.reader import reader as reader_mod
    real = reader_mod.normalize
    monkeypatch.setattr(reader_mod, "normalize",
                        lambda m: (_ for _ in ()).throw(ValueError("bad")) if "poison" in m else real(m))
    src = FakeSource({"a": lines(0, 1) + [hit("x", T0 + 5000, service="s", message="poison pill")]})
    reader, owner = make(conn, cfg, src, T0 / 1000 + 240)
    for _ in range(2):
        tick(reader, owner)
    assert rows(conn, "SELECT text FROM templates WHERE template_id = 's:unparsed'") == [{"text": "(unparsed)"}]
    assert rows(conn, "SELECT sum FROM hourly_counts WHERE key = 's:unparsed'")[0]["sum"] == 1


def test_detection_states_survive_restart(conn, cfg):
    put_setting(conn, "detection_enabled", True)
    err = dict(level="ERROR", moduleCode="PAY")
    src = FakeSource({"a": lines(0, 10, **err)})
    run_minutes(conn, cfg, None, T0 + 60_000, src=src)
    (st,) = rows(conn, "SELECT state, streak FROM alert_state WHERE key = 's:T00001'")
    assert (st["state"], st["streak"]) == ("NORMAL", 1)          # a partial streak is persisted
    src.hits["a"] += lines(1, 10, **err) + lines(2, 10, **err)
    _, owner = run_minutes(conn, cfg, None, T0 + 180_000, src=src)   # fresh owner continues the streak
    assert owner.detection_enabled
    (st,) = rows(conn, "SELECT state, count FROM alert_state WHERE key = 's:T00001'")
    assert (st["state"], st["count"]) == ("ALERTING", 10)
    ch = rows(conn, "SELECT from_state, to_state FROM alert_changes WHERE key = 's:T00001' ORDER BY at")
    assert [(c["from_state"], c["to_state"]) for c in ch] == [("NORMAL", "WARMING"), ("WARMING", "ALERTING")]
    am = rows(conn, "SELECT key, module_code, minute, count FROM alert_minutes ORDER BY minute")
    assert [(r["key"], r["module_code"], r["count"]) for r in am] == [("s:T00001", "PAY", 10)] * 3   # incl. the streak's first minute
    assert am[0]["minute"] == datetime.fromtimestamp(M * 60, timezone.utc)
    assert rows(conn, "SELECT module_code, service FROM modules") == [{"module_code": "PAY", "service": "s"}]


def test_cursors_of_dropped_indices_are_deleted(conn, cfg):
    _, owner = make(conn, cfg, FakeSource({}), T0 / 1000)
    owner.flush({"a": (T0, T0), "b": (T0, T0)}, {})
    owner.flush({"a": (T0 + 60_000, T0)}, {})
    assert rows(conn, "SELECT index, since_ms FROM es_cursors") == [{"index": "a", "since_ms": T0 + 60_000}]
    owner.flush({}, {})                                          # <> ALL('{}') is true: deletes all
    assert rows(conn, "SELECT count(*) AS n FROM es_cursors")[0]["n"] == 0


def test_error_samples_not_duplicated_by_replay(conn, cfg):
    src = FakeSource({"a": lines(0, 1, msg="db down", level="ERROR") + lines(1, 1, msg="db down", level="ERROR")})
    reader, owner = make(conn, cfg, src, T0 / 1000 + 120)        # upper = T0 + 90 s
    tick(reader, owner)                                          # M closed and flushed; M+1 open
    run_minutes(conn, cfg, None, T0 + 120_000, src=src)          # crash, restart re-reads M+1
    got = rows(conn, "SELECT at FROM error_samples ORDER BY at")
    assert [r["at"] for r in got] == [datetime.fromtimestamp(m * 60, timezone.utc) for m in (M, M + 1)]


def test_new_service_zero_filled_from_first_minute(conn, cfg):
    hits = lines(0, 1) + [hit("t1", T0 + 120_000, service="t", message="hello")]
    run_minutes(conn, cfg, hits, T0 + 180_000)                   # one batch closes M..M+2
    got = {r["service"]: r["n_minutes"] for r in rows(conn, "SELECT service, n_minutes FROM service_hours")}
    assert got == {"s": 3, "t": 1}


def test_text_change_clears_the_vector_and_only_alerting_links_load(conn, cfg):
    from logai2.owner.owner import SQL
    vec = "[" + ",".join(["0.1"] * 1024) + "]"
    conn.execute("INSERT INTO templates VALUES ('s', 's:T1', 'ERROR', 'a <*>', now(), now(), %s::vector)", (vec,))
    conn.execute(SQL["templates"], ("s", "s:T1", "ERROR", "a <*>", datetime.now(timezone.utc), datetime.now(timezone.utc)))
    assert conn.execute("SELECT embedding IS NOT NULL AS v FROM templates").fetchone()["v"]   # same text: kept
    conn.execute(SQL["templates"], ("s", "s:T1", "ERROR", "a <*> b", datetime.now(timezone.utc), datetime.now(timezone.utc)))
    assert conn.execute("SELECT embedding IS NULL AS v FROM templates").fetchone()["v"]       # new text: cleared

    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('D1', 't', 'h', 'r')")
    for tid, doc, source in [("s:T1", "D1", "manual"), ("s:T2", "D1", "corpus"), ("s:T3", "D1", "llm"),
                             ("s:T4", "D1", "draft"), ("s:T5", "D1", "rejected"), ("s:T6", None, "manual")]:
        conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('s', %s, %s, %s)",
                     (tid, doc, source))
    conn.commit()
    owner = Owner(conn, cfg, start_minute=0)
    assert owner.detector.links == {("s", "s:T1"): "D1", ("s", "s:T2"): "D1", ("s", "s:T3"): "D1"}


def _svc_hits(minute, service, module, n=1):
    kw = dict(moduleCode=module) if module else {}
    return [hit(f"{service}-{minute}-{i}", T0 + minute * 60_000 + 1000 * i, service=service,
                message="payment failed for user 42", **kw) for i in range(n)]


def _module_pairs(conn):
    return [(r["service"], r["module_code"]) for r in
            rows(conn, "SELECT service, module_code FROM modules ORDER BY 1, 2")]


def test_module_rows_per_service(conn, cfg):
    hits = _svc_hits(0, "a", "M1") + _svc_hits(0, "b", "M1") + _svc_hits(0, "a", None)
    run_minutes(conn, cfg, None, T0 + 60_000, src=FakeSource({"a": hits}))
    assert _module_pairs(conn) == [("a", "M1"), ("b", "M1")]


def test_module_set_survives_restart(conn, cfg):
    src = FakeSource({"a": _svc_hits(0, "a", "M1")})
    run_minutes(conn, cfg, None, T0 + 60_000, src=src)
    src.hits["a"] += _svc_hits(1, "b", "M1")
    run_minutes(conn, cfg, None, T0 + 120_000, src=src)
    assert _module_pairs(conn) == [("a", "M1"), ("b", "M1")]


def _high_setup(conn):
    put_setting(conn, "detection_enabled", True)
    for hour in (10, 11):
        conn.execute("INSERT INTO baseline VALUES ('s', 's:T00001', 'weekday', %s, 1, 0.5, 5)", (hour,))
    conn.commit()


def _alert_minutes(conn):
    return [(r["minute"], r["count"]) for r in rows(
        conn, "SELECT minute, count FROM alert_minutes WHERE key = 's:T00001' AND module_code = 'M1' ORDER BY minute")]


def test_first_high_minute_in_alert_minutes(conn, cfg):
    _high_setup(conn)
    hits = [h for m in range(4) for h in lines(m, 40, level="ERROR", moduleCode="M1")]
    run_minutes(conn, cfg, None, T0 + 4 * 60_000, src=FakeSource({"a": hits}))
    got = _alert_minutes(conn)
    assert [m for m, _ in got] == [datetime.fromtimestamp((M + k) * 60, timezone.utc) for k in range(4)]
    assert {c for _, c in got} == {40}


def test_single_high_minute_writes_one_row_and_no_change(conn, cfg):
    _high_setup(conn)
    hits = lines(0, 40, level="ERROR", moduleCode="M1")
    run_minutes(conn, cfg, None, T0 + 3 * 60_000, src=FakeSource({"a": hits}))
    assert len(_alert_minutes(conn)) == 1
    assert rows(conn, "SELECT 1 FROM alert_changes") == []


def _mod_rows(conn):
    return [(r["template_id"], r["module_code"], r["count"]) for r in rows(
        conn, "SELECT template_id, module_code, count FROM template_module_hours ORDER BY template_id, module_code")]


def test_module_hours_and_pairs_written(conn, cfg):
    hits = lines(0, 2, moduleCode="A") + lines(0, 1, start=2, moduleCode="B")
    run_minutes(conn, cfg, hits, T0 + 60_000)
    assert _mod_rows(conn) == [("s:T00001", "A", 2), ("s:T00001", "B", 1)]
    got = rows(conn, "SELECT module_code, first_seen, last_seen FROM template_modules ORDER BY module_code")
    assert [r["module_code"] for r in got] == ["A", "B"]
    assert got[0]["first_seen"] == got[0]["last_seen"] == datetime.fromtimestamp(M * 60, timezone.utc)


def test_module_hours_exact_across_failed_flush(conn, cfg, monkeypatch):
    hits = lines(0, 2, moduleCode="A") + lines(1, 3, moduleCode="A") + lines(1, 1, start=3, moduleCode="B")
    real = psycopg.Cursor.executemany

    def boom(self, query, params, **kw):
        if "es_cursors" in str(query):
            raise RuntimeError("disk full")
        return real(self, query, params, **kw)

    reader, owner = make(conn, cfg, FakeSource({"a": hits}), T0 / 1000 + 240)
    tick(reader, owner, flush=False)
    tick(reader, owner, flush=False)
    with monkeypatch.context() as mp:
        mp.setattr(psycopg.Cursor, "executemany", boom)
        with pytest.raises(RuntimeError, match="disk full"):
            owner.flush(reader.cursors(owner.boundary_ms), {})
    assert _mod_rows(conn) == []
    owner.flush(reader.cursors(owner.boundary_ms), {})
    assert _mod_rows(conn) == [("s:T00001", "A", 5), ("s:T00001", "B", 1)]
    run_minutes(conn, cfg, hits, T0 + 180_000)      # restart: nothing is counted twice
    assert _mod_rows(conn) == [("s:T00001", "A", 5), ("s:T00001", "B", 1)]


def test_missing_module_stored_as_empty(conn, cfg):
    run_minutes(conn, cfg, lines(0, 2), T0 + 60_000)
    assert _mod_rows(conn) == [("s:T00001", "", 2)]


def test_unparsed_not_stored(conn, cfg, monkeypatch):
    from logai2.reader import reader as reader_mod
    real = reader_mod.normalize
    monkeypatch.setattr(reader_mod, "normalize",
                        lambda m: (_ for _ in ()).throw(ValueError("bad")) if "poison" in m else real(m))
    hits = lines(0, 1, moduleCode="A") + [hit("x", T0 + 5000, service="s", message="poison pill", moduleCode="A")]
    run_minutes(conn, cfg, hits, T0 + 60_000)
    assert rows(conn, "SELECT 1 FROM hourly_counts WHERE key = 's:unparsed'")
    assert _mod_rows(conn) == [("s:T00001", "A", 1)]


def test_module_hours_written_before_detection_enabled(conn, cfg):
    _, owner = run_minutes(conn, cfg, lines(0, 2, moduleCode="A"), T0 + 60_000)
    assert not owner.detection_enabled
    assert _mod_rows(conn) == [("s:T00001", "A", 2)]


def test_module_hours_skipped_past_retention_but_pair_kept(conn, cfg):
    # catch-up after a long downtime: the day is older than module_hours_days, its partition is gone
    hits = lines(0, 2, moduleCode="A")
    now_s = T0 / 1000 + (cfg.retention.module_hours_days + 3) * 86400
    reader, owner = make(conn, cfg, FakeSource({"a": hits}), now_s)
    for _ in range(20):
        if tick(reader, owner)["watermark_ms"] >= T0 + 60_000:
            break
    assert _mod_rows(conn) == []
    assert [r["module_code"] for r in rows(conn, "SELECT module_code FROM template_modules")] == ["A"]

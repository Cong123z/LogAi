"""Adversarial tests of the live recorder: exactly once across crashes, failed flushes (also at COMMIT),
hour/day boundaries, late counts, zero-fill and the retention cutoff; live output unchanged."""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, datetime, timezone

import psycopg
import pytest

from logai2.db import put_setting
from logai2.timeutil import local
from tests.fakes import FakeSource, hit
from tests.test_owner import T0, lines, make, rows, run_minutes, tick
from tests.test_recorder import MIN, feed, lab

MSGS = ("payment failed for user 42", "cache miss for key 7 on node 9")
LIVE = ("hourly_counts", "service_hours", "alert_changes", "alert_state", "alert_minutes",
        "template_module_hours", "templates", "services")


@pytest.fixture
def rec(cfg):
    cfg.lab.record = True
    return cfg


def lab_full(conn):
    return {(r["service"], r["key"], r["day"], r["hour"]): r["counts"] for r in
            rows(conn, "SELECT service, key, day, hour, counts FROM lab_minute_counts")}


def check_invariants(conn):
    """sum(row) == hourly_counts.sum per key-hour (both ways); *minutes sum == service_hours.n_minutes."""
    got = lab_full(conn)
    hc = {(r["service"], r["key"], r["day"], r["hour"]): r["sum"] for r in
          rows(conn, "SELECT service, key, day, hour, sum FROM hourly_counts")}
    sh = {(r["service"], r["day"], r["hour"]): r["n_minutes"] for r in
          rows(conn, "SELECT service, day, hour, n_minutes FROM service_hours")}
    assert {k: sum(v) for k, v in got.items() if k[1] != "*minutes"} == hc
    assert {(s, d, h): sum(v) for (s, k, d, h), v in got.items() if k == "*minutes"} == sh
    assert all(len(v) == 60 and all(x >= 0 for x in v) for v in got.values())
    return got


def clocked(conn, cfg, src, clock, floor_ms=T0):
    reader, owner = make(conn, cfg, src, clock[0], floor_ms)
    reader.now = owner.now = lambda: clock[0]
    return reader, owner


def fail_at_commit(conn, on: bool):
    """A deferred constraint trigger: every write is sent, then COMMIT itself fails."""
    if on:
        conn.execute("CREATE FUNCTION boom_at_commit() RETURNS trigger LANGUAGE plpgsql AS "
                     "$$ BEGIN RAISE EXCEPTION 'commit failed'; END $$")
        conn.execute("CREATE CONSTRAINT TRIGGER boom AFTER INSERT OR UPDATE ON worker_status "
                     "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION boom_at_commit()")
    else:
        conn.execute("DROP TRIGGER boom ON worker_status")
        conn.execute("DROP FUNCTION boom_at_commit()")
    conn.commit()


def test_crash_after_flush_closing_last_minute_of_hour(conn, rec):
    per = {m: m % 5 + 1 for m in range(70)}   # 10:00-11:09 local
    src = FakeSource({"a": feed(per)})
    run_minutes(conn, rec, None, T0 + 60 * MIN, src=src)   # closes exactly 10:59, flushed
    after = lab(conn)
    assert after[("s", "*minutes")] == [1] * 60
    # restart (no pending) and also a crash with 11:00-11:04 closed in memory only
    reader, owner = make(conn, rec, src, (T0 + 65 * MIN) / 1000 + 60)
    tick(reader, owner, flush=False)
    run_minutes(conn, rec, None, T0 + 70 * MIN, src=src)
    got = check_invariants(conn)
    d = date(2026, 10, 20)
    assert got[("s", "*total", d, 10)] == [per[m] for m in range(60)]
    assert got[("s", "*total", d, 11)] == [per[m] for m in range(60, 70)] + [0] * 50
    assert got[("s", "*minutes", d, 11)] == [1] * 10 + [0] * 50


def test_hour_boundary_inside_one_flush(conn, rec):
    per = {m: 2 for m in range(50, 70)}
    reader, owner = make(conn, rec, FakeSource({"a": feed(per)}), (T0 + 70 * MIN) / 1000 + 60, floor_ms=T0 + 50 * MIN)
    tick(reader, owner)
    got = check_invariants(conn)
    d = date(2026, 10, 20)
    assert got[("s", "*total", d, 10)] == [0] * 50 + [2] * 10
    assert got[("s", "*total", d, 11)] == [2] * 10 + [0] * 50
    assert got[("s", "*minutes", d, 10)] == [0] * 50 + [1] * 10


def test_day_boundary_inside_one_flush(conn, rec):
    mid = int(datetime(2026, 10, 20, 16, 50, tzinfo=timezone.utc).timestamp() * 1000)   # 23:50 local
    hits = [hit(f"x{i}", mid + i * 30_000, service="s", message=MSGS[0]) for i in range(40)]   # 20 minutes, 2 each
    reader, owner = make(conn, rec, FakeSource({"a": hits}), mid / 1000 + 21 * 60 + 60, floor_ms=mid)
    tick(reader, owner)
    got = check_invariants(conn)
    assert got[("s", "*total", date(2026, 10, 20), 23)] == [0] * 50 + [2] * 10
    assert got[("s", "*total", date(2026, 10, 21), 0)] == [2] * 10 + [0] * 50


def test_failure_at_commit_keeps_pending_and_writes_once(conn, rec):
    per = {m: 3 for m in range(5)}
    reader, owner = make(conn, rec, FakeSource({"a": feed(per)}), (T0 + 5 * MIN) / 1000 + 60)
    tick(reader, owner, flush=False)
    fail_at_commit(conn, True)
    with pytest.raises(psycopg.Error, match="commit failed"):
        owner.flush(reader.cursors(owner.boundary_ms), {})
    assert lab(conn) == {}
    fail_at_commit(conn, False)
    owner.flush(reader.cursors(owner.boundary_ms), {})
    owner.flush(reader.cursors(owner.boundary_ms), {})
    got = check_invariants(conn)
    assert got[("s", "*total", date(2026, 10, 20), 10)] == [3] * 5 + [0] * 55


def test_failure_at_commit_twice_then_more_minutes(conn, rec):
    per = {m: m + 1 for m in range(10)}
    clock = [(T0 + 3 * MIN) / 1000 + 60]
    src = FakeSource({"a": feed(per)})
    reader, owner = clocked(conn, rec, src, clock)
    tick(reader, owner, flush=False)
    fail_at_commit(conn, True)
    for _ in range(2):
        with pytest.raises(psycopg.Error):
            owner.flush(reader.cursors(owner.boundary_ms), {})
    fail_at_commit(conn, False)
    clock[0] = (T0 + 10 * MIN) / 1000 + 60
    tick(reader, owner)    # more minutes close into the same pending row, then one good flush
    got = check_invariants(conn)
    assert got[("s", "*total", date(2026, 10, 20), 10)] == [m + 1 for m in range(10)] + [0] * 50


def test_late_count_not_recorded(conn, rec):
    reader, owner = run_minutes(conn, rec, feed({0: 2, 1: 2}), T0 + 2 * MIN)
    before = lab_full(conn)
    late = {"service": "s", "template_id": "s:T00001", "text": None, "level": "INFO", "module": "",
            "minute": T0 // MIN, "n": 7, "sample": "x"}
    owner.handle({"counts": [late], "watermark_ms": owner.boundary_ms})
    owner.flush(reader.cursors(owner.boundary_ms), {})
    assert owner.minutes.late == 7
    assert lab_full(conn) == before
    check_invariants(conn)


def test_services_with_different_first_minutes_and_zero_fill_only(conn, rec):
    conn.execute("INSERT INTO services VALUES ('z', 1)")   # known, never logs: only zero-filled minutes
    conn.commit()
    hits = [hit(f"p{m}", T0 + m * MIN + 1000, service="p", message=MSGS[0]) for m in range(10)]
    hits += [hit(f"q{m}", T0 + m * MIN + 2000, service="q", message=MSGS[1]) for m in range(4, 10)]
    run_minutes(conn, rec, hits, T0 + 10 * MIN)
    got = check_invariants(conn)
    d = date(2026, 10, 20)
    assert got[("p", "*minutes", d, 10)] == [1] * 10 + [0] * 50
    assert got[("q", "*minutes", d, 10)] == [0] * 4 + [1] * 6 + [0] * 50
    assert got[("z", "*minutes", d, 10)] == [1] * 10 + [0] * 50
    assert [k for k in got if k[0] == "z"] == [("z", "*minutes", d, 10)]   # all-zero rows not stored


def test_restart_mid_hour_keeps_existing_rows(conn, rec):
    per = {m: 1 + (m % 2) for m in range(40)}
    src = FakeSource({"a": feed(per)})
    run_minutes(conn, rec, None, T0 + 25 * MIN, src=src)
    run_minutes(conn, rec, None, T0 + 40 * MIN, src=src)   # clean restart at 10:25
    got = check_invariants(conn)
    assert got[("s", "*total", date(2026, 10, 20), 10)] == [per[m] for m in range(40)] + [0] * 20
    assert got[("s", "*minutes", date(2026, 10, 20), 10)] == [1] * 40 + [0] * 20


def test_cutoff_exactly_at_boundary_day(conn, rec):
    rec.lab.days = 5
    mid = int(datetime(2026, 10, 19, 16, 58, tzinfo=timezone.utc).timestamp() * 1000)   # 10-19 23:58 local
    hits = [hit(f"x{i}", mid + i * MIN + 1000, service="s", message=MSGS[0]) for i in range(4)]
    reader, owner = make(conn, rec, FakeSource({"a": hits}), mid / 1000 + 5 * 60 + 60, floor_ms=mid)
    owner.now = lambda: datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc).timestamp()   # today 10-25, cutoff 10-20
    tick(reader, owner)
    got = lab_full(conn)
    assert set(got) == {("s", k, date(2026, 10, 20), 0) for k in ("*total", "*info", "*minutes", "s:T00001")}
    assert got[("s", "*total", date(2026, 10, 20), 0)] == [1, 1] + [0] * 58


def test_live_tables_identical_with_detection_on(conn, cfg):
    hits = [h for m in range(6) for h in lines(m, 40 if m in (2, 3, 4) else 1, level="ERROR", moduleCode="M1")]
    hits += [hit(f"e{m}", T0 + m * MIN + 5000, service="s", message="db timeout 5", level="ERROR", moduleCode="M1")
             for m in range(6)]

    def one(flag):
        conn.execute("TRUNCATE " + ", ".join((*LIVE, "lab_minute_counts", "es_cursors", "drain3_state", "error_samples",
                                              "template_modules", "modules", "baseline")) + " CASCADE")
        put_setting(conn, "detection_enabled", True)
        for hour in (10, 11):
            conn.execute("INSERT INTO baseline VALUES ('s', 's:T00001', 'weekday', %s, 1, 0.5, 5)", (hour,))
        conn.commit()
        cfg.lab.record = flag
        run_minutes(conn, cfg, hits, T0 + 6 * MIN)
        return {t: sorted(map(str, rows(conn, f"SELECT * FROM {t}"))) for t in LIVE}

    off, on = one(False), one(True)
    assert off["alert_state"] and off["alert_changes"]   # detection really ran
    assert on == off


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_feed_random_crashes(conn, rec, monkeypatch, seed):
    rng = random.Random(seed)
    start = int(datetime(2026, 10, 20, 15, 40, tzinfo=timezone.utc).timestamp() * 1000)   # 22:40 local
    n_min = 150                                                                            # to 01:10 next day
    first = {"p": 0, "q": rng.randint(5, 70)}
    hits, want = [], defaultdict(int)   # (service, minute) -> count
    for m in range(n_min):
        for s, f in first.items():
            if m < f or rng.random() < 0.2:
                continue
            for k in range(rng.randint(0, 4)):
                hits.append(hit(f"{s}{m}-{k}", start + m * MIN + rng.randint(0, 59_999), service=s,
                                message=MSGS[k % 2], level=rng.choice(("INFO", "ERROR", "WARN"))))
                want[(s, m)] += 1
    real_first = {s: min(m for (t, m) in want if t == s) for s in first}
    src = FakeSource({"a": hits})
    clock = [start / 1000 + 30]
    reader, owner = clocked(conn, rec, src, clock, start)
    real = psycopg.Cursor.executemany

    def boom(self, query, params, **kw):
        if "es_cursors" in str(query):
            raise RuntimeError("disk full")
        return real(self, query, params, **kw)

    end_s = (start + n_min * MIN) / 1000 + 120
    while True:
        clock[0] = min(clock[0] + rng.randint(5, 200), end_s)
        tick(reader, owner, flush=False)
        r = rng.random()
        if r < 0.15:                                    # crash: pending lost, restart from the database
            reader, owner = clocked(conn, rec, src, clock, start)
            continue
        if r < 0.25:                                    # failure inside the transaction
            monkeypatch.setattr(psycopg.Cursor, "executemany", boom)
            with pytest.raises(RuntimeError):
                owner.flush(reader.cursors(owner.boundary_ms), {})
            monkeypatch.setattr(psycopg.Cursor, "executemany", real)
        elif r < 0.32:                                  # failure at COMMIT
            fail_at_commit(conn, True)
            with pytest.raises(psycopg.Error):
                owner.flush(reader.cursors(owner.boundary_ms), {})
            fail_at_commit(conn, False)
        if r < 0.32 and rng.random() < 0.5:             # crash right after a failed flush
            reader, owner = clocked(conn, rec, src, clock, start)
            continue
        if r < 0.55:                                    # keep pending across ticks
            continue
        owner.flush(reader.cursors(owner.boundary_ms), {})
        reader.forget_read()
        if clock[0] >= end_s and owner.boundary_ms >= start + n_min * MIN:
            break
    got = check_invariants(conn)
    total = {(s, d, h): v for (s, k, d, h), v in got.items() if k == "*total"}
    mins = {(s, d, h): v for (s, k, d, h), v in got.items() if k == "*minutes"}
    exp_total, exp_mins = defaultdict(lambda: [0] * 60), defaultdict(lambda: [0] * 60)
    for m in range(owner.boundary_ms // MIN - start // MIN):   # every closed minute, also past the feed
        t = local(start // MIN + m)
        for s in first:
            if m >= real_first[s]:
                exp_mins[(s, t.date(), t.hour)][t.minute] = 1
                if want[(s, m)]:
                    exp_total[(s, t.date(), t.hour)][t.minute] += want[(s, m)]
    assert total == dict(exp_total)
    assert mins == dict(exp_mins)
    for (s, d, h), v in total.items():   # template rows and level rows each add up to *total, slot by slot
        tmpl = [r for (s2, k, d2, h2), r in got.items() if (s2, d2, h2) == (s, d, h) and ":" in k]
        lvl = [r for (s2, k, d2, h2), r in got.items() if (s2, d2, h2) == (s, d, h) and k in ("*error", "*warn", "*info")]
        assert [sum(c) for c in zip(*tmpl)] == v and [sum(c) for c in zip(*lvl)] == v

"""Live recorder: per-minute counts of each key-hour, written once in the flush transaction."""
from __future__ import annotations

from datetime import date

import psycopg
import pytest

from logai2.db import ensure_partitions, put_setting
from logai2.owner.owner import SQL, copy_upsert
from tests.fakes import FakeSource
from tests.test_owner import M, T0, lines, make, rows, run_minutes, tick

MIN = 60_000


@pytest.fixture
def rec(cfg):
    cfg.lab.record = True
    return cfg


def lab(conn):
    return {(r["service"], r["key"]): r["counts"] for r in
            rows(conn, "SELECT service, key, counts FROM lab_minute_counts ORDER BY service, key")}


def feed(per_minute):   # {minute: n} -> hits
    return [h for m, n in per_minute.items() for h in lines(m, n)]


def test_flag_off_writes_nothing(conn, cfg):
    run_minutes(conn, cfg, feed({0: 3, 1: 2}), T0 + 3 * MIN)
    assert rows(conn, "SELECT count(*) AS n FROM lab_minute_counts")[0]["n"] == 0


def test_hour_across_three_flushes(conn, rec):
    per = {m: m % 4 + 1 for m in range(60)}
    hits = feed(per)
    src = FakeSource({"a": hits})
    for end in (20, 40, 60):
        run_minutes(conn, rec, None, T0 + end * MIN, src=src)
    got = lab(conn)
    assert got[("s", "*total")] == [per[m] for m in range(60)]
    assert got[("s", "s:T00001")] == [per[m] for m in range(60)]
    assert got[("s", "*minutes")] == [1] * 60
    hc = {r["key"]: r["sum"] for r in rows(conn, "SELECT key, sum FROM hourly_counts")}
    for (svc, key), counts in got.items():
        if key != "*minutes":
            assert sum(counts) == hc[key], key
    assert sum(got[("s", "*minutes")]) == rows(conn, "SELECT n_minutes FROM service_hours")[0]["n_minutes"]


def test_crash_mid_hour_exact(conn, rec):
    per = {m: m % 3 + 1 for m in range(60)}
    src = FakeSource({"a": feed(per)})
    run_minutes(conn, rec, None, T0 + 30 * MIN, src=src)
    # minutes 30-34 close in memory only, then the process dies
    reader, owner = make(conn, rec, src, (T0 + 35 * MIN) / 1000 + 60)
    tick(reader, owner, flush=False)
    run_minutes(conn, rec, None, T0 + 60 * MIN, src=src)
    got = lab(conn)
    assert got[("s", "*total")] == [per[m] for m in range(60)]
    assert got[("s", "*minutes")] == [1] * 60


def test_failed_flush_keeps_pending(conn, rec, monkeypatch):
    real = psycopg.Cursor.executemany

    def boom(self, query, params, **kw):
        if "es_cursors" in str(query):
            raise RuntimeError("disk full")
        return real(self, query, params, **kw)

    reader, owner = make(conn, rec, FakeSource({"a": feed({0: 3, 1: 2})}), T0 / 1000 + 240)
    tick(reader, owner, flush=False)
    monkeypatch.setattr(psycopg.Cursor, "executemany", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        owner.flush(reader.cursors(owner.boundary_ms), {})
    assert lab(conn) == {}
    monkeypatch.setattr(psycopg.Cursor, "executemany", real)
    owner.flush(reader.cursors(owner.boundary_ms), {})
    owner.flush(reader.cursors(owner.boundary_ms), {})
    got = lab(conn)
    assert got[("s", "*total")][:3] == [3, 2, 0] and sum(got[("s", "*total")]) == 5
    assert sum(got[("s", "*minutes")]) == 3


def test_silent_minutes_and_minutes_key(conn, rec):
    # a second template (different text) seen at minute 3 only
    hits = feed({0: 1, 1: 1, 2: 1, 4: 1}) + lines(3, 2, msg="cache miss for key 7 on node 9")
    run_minutes(conn, rec, hits, T0 + 5 * MIN)
    got = lab(conn)
    tid = [k for (s, k) in got if k.startswith("s:T")]
    silent = [got[("s", k)] for k in tid if sum(got[("s", k)]) == 2]
    assert silent == [[0, 0, 0, 2] + [0] * 56]
    assert got[("s", "*minutes")] == [1] * 5 + [0] * 55


def test_old_day_not_written(conn, rec):
    rec.lab.days = 5
    # now is 2026-10-20; shifting the clock 10 days ahead makes the closed day too old
    src = FakeSource({"a": feed({0: 2, 1: 2})})
    reader, owner = make(conn, rec, src, (T0 + 3 * MIN) / 1000 + 60)
    owner.now = lambda: (T0 + 10 * 86_400_000) / 1000
    tick(reader, owner)
    assert rows(conn, "SELECT count(*) AS n FROM lab_minute_counts")[0]["n"] == 0
    assert rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours")[0]["n"] == 3   # live path unaffected


def test_live_tables_unchanged(conn, cfg):
    put_setting(conn, "detection_enabled", True)
    for hour in (10, 11):
        conn.execute("INSERT INTO baseline VALUES ('s', 's:T00001', 'weekday', %s, 1, 0.5, 5)", (hour,))
    conn.commit()
    hits = [h for m in range(6) for h in lines(m, 40, level="ERROR", moduleCode="M1")]
    tables = ("hourly_counts", "service_hours", "alert_changes", "alert_state")

    def one(flag):
        for t in tables + ("lab_minute_counts", "alert_minutes", "template_module_hours", "template_modules", "modules",
                              "es_cursors", "templates", "services", "drain3_state"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        cfg.lab.record = flag
        run_minutes(conn, cfg, hits, T0 + 6 * MIN)
        return {t: rows(conn, f"SELECT * FROM {t} ORDER BY 1, 2, 3") for t in tables}

    off = one(False)
    on = one(True)
    assert off["alert_state"] and off["alert_changes"] and on["alert_state"] and on["alert_changes"]
    assert on == off
    assert rows(conn, "SELECT count(*) AS n FROM lab_minute_counts")[0]["n"] > 0


def test_int_array_copy(conn):
    ensure_partitions(conn, date(2026, 10, 20), date(2026, 10, 20))
    counts = list(range(60))
    with conn.transaction(), conn.cursor() as cur:
        copy_upsert(cur, "lab_minute_counts", [("s", "k", date(2026, 10, 20), 10, counts)])
        copy_upsert(cur, "lab_minute_counts", [("s", "k", date(2026, 10, 20), 10, counts)])
    assert lab(conn)[("s", "k")] == [2 * i for i in range(60)]
    assert "unnest" in SQL["lab_minute_counts"]

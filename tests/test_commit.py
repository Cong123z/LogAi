from datetime import date

import pytest

from logai2.db import ensure_partitions, get_setting
from logai2.jobs.commit import commit


def seed(conn, service, day, hour, n_minutes, sums):
    with conn.transaction():
        conn.execute("INSERT INTO service_hours VALUES (%s, %s, %s, %s)", (service, day, hour, n_minutes))
        for key, (s, sq) in sums.items():
            conn.execute("INSERT INTO hourly_counts VALUES (%s, %s, %s, %s, %s, %s)",
                         (service, key, day, hour, s, sq))


def baseline_rows(conn):
    return conn.execute("SELECT * FROM baseline ORDER BY service, key, daytype, hour").fetchall()


def test_commit_mean_std_and_silent_minutes(conn, cfg):
    # Mon 2026-10-12 hour 10: 60 minutes observed, key seen in 2 minutes with counts 3 and 3
    seed(conn, "s", date(2026, 10, 12), 10, 60, {"s:T1": (6, 18)})
    commit(conn, cfg, today=date(2026, 10, 13))
    row = conn.execute("SELECT mean, std, days FROM baseline WHERE key='s:T1'").fetchone()
    assert row["mean"] == pytest.approx(0.1) and row["std"] == pytest.approx((0.3 - 0.01) ** 0.5)
    assert row["days"] == 1


def test_commit_keeps_last_5_weekdays_and_skips_today(conn, cfg):
    old = [date(2026, 10, 5), date(2026, 10, 6)]
    recent = [date(2026, 10, d) for d in (7, 8, 9, 12, 13)]
    for day in old:
        seed(conn, "s", day, 10, 60, {"k": (600, 6000)})          # mean 10: must not be used
    for day in recent:
        seed(conn, "s", day, 10, 60, {"k": (60, 60)})             # mean 1, std 0
    seed(conn, "s", date(2026, 10, 14), 10, 60, {"k": (6000, 600000)})   # today, still staging
    out = commit(conn, cfg, today=date(2026, 10, 14))
    assert out == {"days_committed": 7, "baseline_rows": 1, "rows_before": 0}
    row = conn.execute("SELECT * FROM baseline").fetchone()
    assert (row["daytype"], row["mean"], row["std"], row["days"]) == ("weekday", 1.0, 0.0, 5)
    assert conn.execute("SELECT count(*) AS n FROM day_status WHERE day = '2026-10-14'").fetchone()["n"] == 0


def test_weekend_days_get_their_own_daytype(conn, cfg):
    seed(conn, "s", date(2026, 10, 17), 3, 60, {"k": (60, 60)})   # Saturday
    commit(conn, cfg, today=date(2026, 10, 19))
    assert conn.execute("SELECT daytype FROM day_status").fetchone()["daytype"] == "weekend"
    assert conn.execute("SELECT daytype FROM baseline").fetchone()["daytype"] == "weekend"


def test_exclusion_removes_hour(conn, cfg):
    for day in (date(2026, 10, 12), date(2026, 10, 13)):
        for hour in (10, 11):
            seed(conn, "s", day, hour, 60, {"k": (60, 60)})
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) VALUES "
                 "('s', '2026-10-12', 10, 11, 'exclude'), ('s', '2026-10-13', 10, 12, 'keep')")
    conn.commit()
    commit(conn, cfg, today=date(2026, 10, 14))
    days = {r["hour"]: r["days"] for r in conn.execute("SELECT hour, days FROM baseline")}
    assert days == {10: 1, 11: 2}


def test_commit_is_idempotent_and_catches_up_missed_days(conn, cfg):
    # three finished days never committed (engine was down at 02:00) -> all three in one call
    for d in (27, 28, 29):
        seed(conn, "s", date(2026, 10, d), 10, 60, {"k": (60 * d, 60 * d * d)})
    first = commit(conn, cfg, today=date(2026, 10, 30))
    assert first == {"days_committed": 3, "baseline_rows": 1, "rows_before": 0}
    rows = baseline_rows(conn)
    assert rows[0]["days"] == 3 and rows[0]["mean"] == pytest.approx(28)
    assert commit(conn, cfg, today=date(2026, 10, 30)) == {"days_committed": 0, "baseline_rows": 1, "rows_before": 1}
    assert baseline_rows(conn) == rows
    assert conn.execute("SELECT to_regclass('hourly_counts_20261101') AS t").fetchone()["t"]
    assert get_setting(conn, "last_commit")["today"] == "2026-10-30"


def test_failed_commit_rolls_back_everything(conn, cfg, monkeypatch):
    seed(conn, "s", date(2026, 10, 12), 10, 60, {"k": (60, 60)})
    commit(conn, cfg, today=date(2026, 10, 13))
    before_rows = baseline_rows(conn)
    seed(conn, "s", date(2026, 10, 13), 10, 60, {"k": (600, 6000)})

    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr("logai2.jobs.commit.drop_partitions_before", boom)   # fails after the rebuild
    with pytest.raises(RuntimeError):
        commit(conn, cfg, today=date(2026, 10, 14))
    committed = [r["day"] for r in conn.execute("SELECT day FROM day_status WHERE committed")]
    assert committed == [date(2026, 10, 12)]
    assert baseline_rows(conn) == before_rows


def test_new_level_key_baseline_starts_at_its_first_day(conn, cfg):
    days = [date(2026, 10, d) for d in (7, 8, 9, 12, 13)]
    for day in days[:-1]:
        seed(conn, "s", day, 10, 60, {"*total": (60, 60)})
    seed(conn, "s", days[-1], 10, 60, {"*total": (60, 60), "*warn": (60, 60), "k": (60, 60)})
    commit(conn, cfg, today=date(2026, 10, 14))
    rows = {r["key"]: r for r in conn.execute("SELECT key, mean, days FROM baseline WHERE hour = 10")}
    assert rows["*warn"]["mean"] == pytest.approx(1.0) and rows["*warn"]["days"] == 1
    assert rows["k"]["mean"] == pytest.approx(0.2) and rows["k"]["days"] == 5     # absent days were really 0


def test_commit_drops_module_hours_after_7_days(conn, cfg):
    ensure_partitions(conn, date(2026, 10, 1), date(2026, 10, 31))
    seed(conn, "s", date(2026, 10, 12), 10, 60, {"k": (1, 1)})
    commit(conn, cfg, today=date(2026, 10, 20))
    tables = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert "template_module_hours_20261012" not in tables       # today-8
    assert "template_module_hours_20261013" in tables           # today-7 (boundary)
    assert "template_module_hours_20261014" in tables           # today-6
    assert "hourly_counts_20261012" in tables


def test_rebuild_false_keeps_baseline(conn, cfg):
    seed(conn, "s", date(2026, 10, 12), 10, 60, {"k": (60, 60)})
    commit(conn, cfg, today=date(2026, 10, 13))
    before = baseline_rows(conn)
    seed(conn, "s", date(2026, 10, 13), 10, 60, {"k": (600, 6000)})
    steps = []
    out = commit(conn, cfg, today=date(2026, 10, 14), rebuild=False, on_step=lambda *a: steps.append(a[0]))
    assert out["days_committed"] == 1 and out["baseline_rows"] == out["rows_before"] == len(before)
    assert baseline_rows(conn) == before
    assert steps == ["mark", "housekeeping"]


def test_commit_steps_and_failure_step(conn, cfg, monkeypatch):
    seen = []
    commit(conn, cfg, today=date(2026, 10, 13), on_step=lambda *a: seen.append(a))
    assert [a[0] for a in seen] == ["mark", "rebuild", "housekeeping"]
    assert seen[1][2] == {"rows_before": 0, "rows": 0} and seen[2][2]["runs_deleted"] == 0
    assert all(a[3] <= a[4] for a in seen)
    monkeypatch.setattr("logai2.jobs.commit.REBUILD", "SELECT * FROM no_such_table")
    with pytest.raises(Exception) as ei:
        commit(conn, cfg, today=date(2026, 10, 14))
    assert ei.value.step == "rebuild"


def test_old_runs_deleted(conn, cfg):
    for name, days in (("old", 366), ("new", 364)):
        conn.execute("INSERT INTO retrain_runs (kind, trigger, state, started_at) "
                     "VALUES (%s, 'manual', 'succeeded', now() - make_interval(days => %s))", (name, days))
    conn.commit()
    commit(conn, cfg, today=date(2026, 10, 13))
    assert [r["kind"] for r in conn.execute("SELECT kind FROM retrain_runs")] == ["new"]


def test_commit_drops_old_lab_partitions(conn, cfg):
    ensure_partitions(conn, date(2026, 10, 1), date(2026, 10, 31))
    cfg.lab.days = 7
    commit(conn, cfg, today=date(2026, 10, 20))
    tables = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert "lab_minute_counts_20261012" not in tables           # today-8
    assert "lab_minute_counts_20261013" in tables               # today-7 (boundary)

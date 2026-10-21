from datetime import date, datetime

import pytest

from logai2.timeutil import TZ
from logai2.web.app import create_app
from logai2.web.baseline_view import left_days, matrix
from tests.conftest import TEST_DSN

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=TZ).timestamp()


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def service(conn, name):
    conn.execute("INSERT INTO services VALUES (%s, 0)", (name,))
    conn.commit()


def hours(conn, svc, key, day, cells):
    """cells: {hour: (sum, sumsq, n_minutes)}"""
    for hour, (s, sq, n) in cells.items():
        conn.execute("INSERT INTO service_hours VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (svc, day, hour, n))
        conn.execute("INSERT INTO hourly_counts VALUES (%s, %s, %s, %s, %s, %s)", (svc, key, day, hour, s, sq))
    conn.commit()


def base(conn, svc, key, dt, hour, mean, std):
    conn.execute("INSERT INTO baseline VALUES (%s, %s, %s, %s, %s, %s, 5)", (svc, key, dt, hour, mean, std))
    conn.commit()


def committed(conn, svc, days):
    for d in days:
        conn.execute("INSERT INTO day_status VALUES (%s, %s, true, %s)", (svc, d, "weekend" if d.weekday() >= 5 else "weekday"))
    conn.commit()


def exclude(conn, svc, day, hour_from, hour_to):
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                 "VALUES (%s, %s, %s, %s, 'exclude')", (svc, day, hour_from, hour_to))
    conn.commit()


def cell(m, day_iso, hour):
    day = next(d for d in m["days"] if d["day"] == day_iso)
    return next((h for h in day["hours"] if h["hour"] == hour), None)


WEEKDAYS = [date(2026, 10, d) for d in (20, 19, 16, 15, 14)]      # Tue Mon Fri Thu Wed, newest first


def seed_pay(conn):
    service(conn, "pay")
    committed(conn, "pay", WEEKDAYS)
    base(conn, "pay", "*total", "weekday", 2, 540, 57)
    base(conn, "pay", "*total", "weekday", 3, 520, 50)
    base(conn, "pay", "*total", "weekend", 3, 0, 0)
    hours(conn, "pay", "*total", date(2026, 10, 20), {2: (1840 * 60, 0, 60)})
    hours(conn, "pay", "*total", date(2026, 10, 16), {10: (6000, 0, 60)})
    exclude(conn, "pay", date(2026, 10, 16), 10, 11)
    exclude(conn, "pay", date(2026, 10, 19), 2, 3)
    exclude(conn, "pay", date(2026, 10, 15), 2, 3)
    # *total not NORMAL 20 Oct 02:10-02:51 local = 19 Oct 19:10-19:51 UTC
    for at, frm, to in (("2026-10-19 19:10+00", "NORMAL", "ALERTING"), ("2026-10-19 19:51+00", "ALERTING", "NORMAL")):
        conn.execute("INSERT INTO alert_changes VALUES ('pay', '*total', %s, %s, %s, 0)", (at, frm, to))
    conn.commit()


def test_matrix_z_class_and_marks(conn, cfg):
    seed_pay(conn)
    m = matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)
    h2 = cell(m, "2026-10-20", 2)
    assert (round(h2["rate"]), h2["mean"], h2["std"], round(h2["z"], 1), h2["cls"]) == (1840, 540.0, 57.0, 22.8, "up3")
    assert h2["suggested"] and h2["not_normal_minutes"] == 41 and h2["decision"] is None
    assert cell(m, "2026-10-16", 10)["decision"] == "exclude"
    assert m["k"] == {"weekday": 3.0, "weekend": 4.0} and m["profiles"]["weekday"] == "full"
    assert [d["day"] for d in m["days"]][:2] == ["2026-10-21", "2026-10-20"] and m["days"][1]["in_baseline"]


def test_matrix_without_baseline(conn, cfg):
    service(conn, "new")
    hours(conn, "new", "*total", date(2026, 10, 20), {9: (600, 0, 60)})
    h = cell(matrix(conn, cfg, "new", "*total", 14, TODAY, NOW), "2026-10-20", 9)
    assert h["z"] is None and h["no_baseline"] and h["cls"] == "none"
    m = matrix(conn, cfg, "new", "*total", 14, TODAY, NOW)
    assert all(c["days"] == 0 for c in m["coverage"]["weekday"]) and m["profiles"]["weekday"] == "none"


def test_weekend_borrows_weekday_and_no_drop_colour(conn, cfg):
    service(conn, "pay")
    committed(conn, "pay", WEEKDAYS + [date(2026, 10, 10)])
    base(conn, "pay", "*total", "weekday", 10, 100, 10)
    hours(conn, "pay", "*total", date(2026, 10, 17), {10: (20 * 60, 0, 60)})
    m = matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)
    h = cell(m, "2026-10-17", 10)
    assert h["mean"] == 100.0 and h["z"] == -8.0 and h["cls"] == "in"
    assert m["profiles"]["weekend"] == "borrowed"


def test_flat_key_uses_sigma_floor(conn, cfg):
    service(conn, "pay")
    committed(conn, "pay", WEEKDAYS)
    base(conn, "pay", "*error", "weekday", 4, 5, 0)
    hours(conn, "pay", "*error", date(2026, 10, 20), {4: (9 * 60, 0, 60)})
    assert cell(matrix(conn, cfg, "pay", "*error", 14, TODAY, NOW), "2026-10-20", 4)["z"] == 4.0


def test_partial_and_missing_hours(conn, cfg):
    service(conn, "pay")
    hours(conn, "pay", "*total", TODAY, {14: (600, 0, 20)})
    today = {d["day"]: d for d in matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)["days"]}["2026-10-21"]
    assert {h["hour"]: h["rate"] for h in today["hours"]}.get(14) == 30.0 and 15 not in [h["hour"] for h in today["hours"]]


def test_doc_key_sums_linked_templates(conn, cfg):
    service(conn, "pay")
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('DOC-001', 't', 'h', 'r')")
    for t in ("pay:T00001", "pay:T00002"):
        conn.execute("INSERT INTO templates VALUES ('pay', %s, 'ERROR', 'boom', now(), now())", (t,))
        conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES ('pay', %s, 'DOC-001')", (t,))
    conn.commit()
    committed(conn, "pay", WEEKDAYS)
    base(conn, "pay", "pay:T00001", "weekday", 2, 1, 0.5)
    base(conn, "pay", "pay:T00002", "weekday", 2, 0.5, 0.5)
    hours(conn, "pay", "pay:T00001", date(2026, 10, 20), {2: (120, 0, 60)})
    hours(conn, "pay", "pay:T00002", date(2026, 10, 20), {2: (60, 0, 60)})
    h = cell(matrix(conn, cfg, "pay", "doc:DOC-001", 14, TODAY, NOW), "2026-10-20", 2)
    assert (h["rate"], h["mean"], h["std"]) == (3.0, 1.5, 1.0)


def test_unknown_keys_404(client, conn):
    service(conn, "pay")
    service(conn, "other")
    conn.execute("INSERT INTO templates VALUES ('other', 'other:T00001', 'ERROR', 'boom', now(), now())")
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('DOC-EMPTY', 't', 'h', 'r')")
    conn.commit()
    for key in ("pay:T09999", "other:T00001", "doc:DOC-404", "doc:DOC-EMPTY"):
        assert client.get(f"/api/baseline/matrix?service=pay&key={key}").status_code == 404
    assert client.get("/api/baseline/matrix?service=pay&days=29").status_code == 400
    assert client.get("/api/baseline/matrix?service=pay").status_code == 200


def test_coverage_counts_used_days_minus_exclusions_and_idle(conn, cfg):
    seed_pay(conn)
    cov = matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)["coverage"]
    assert cov["weekday"][2] == {"hour": 2, "days": 3, "of": 5, "idle": False,
                                 "used": ["2026-10-20", "2026-10-16", "2026-10-14"],
                                 "excluded": ["2026-10-19", "2026-10-15"]}
    assert cov["weekend"][3]["idle"] is True


def test_left_days_before_the_click(conn, cfg):
    seed_pay(conn)
    assert left_days(conn, "pay", date(2026, 10, 20), 2, 4, True) == [
        {"hour": 2, "days": 2, "of": 5}, {"hour": 3, "days": 4, "of": 5}]

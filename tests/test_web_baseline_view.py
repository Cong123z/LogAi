from datetime import date, datetime

import pytest

from logai2.timeutil import TZ
from logai2.web.app import create_app
from logai2.web.baseline_view import incident, inspect, left_days, matrix, series
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


def test_zero_baseline_with_events_is_up3(conn, cfg):
    service(conn, "pay")
    committed(conn, "pay", WEEKDAYS)
    base(conn, "pay", "*total", "weekday", 3, 0, 0)
    hours(conn, "pay", "*total", date(2026, 10, 20), {3: (120, 0, 60)})
    hours(conn, "pay", "*total", date(2026, 10, 19), {3: (0, 0, 60)})
    m = matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)
    busy, quiet = cell(m, "2026-10-20", 3), cell(m, "2026-10-19", 3)
    assert (busy["no_baseline"], busy["z"], busy["cls"]) == (True, None, "up3")
    assert (quiet["no_baseline"], quiet["z"], quiet["cls"]) == (True, None, "in")


def seed_inspect(conn):
    seed_pay(conn)
    hours(conn, "pay", "*total", date(2026, 10, 13), {2: (530 * 60, 0, 60)})
    hours(conn, "pay", "*total", date(2026, 10, 20), {3: (600 * 60, 0, 60)})
    conn.execute("INSERT INTO documents VALUES ('DOC-1', 'Recharge failures', '', 'b', 'r')")
    for tid, first in (("T00032", "2026-10-10 00:00+07"), ("T00141", "2026-10-10 00:00+07"),
                       ("T00309", "2026-10-20 01:30+07"), ("T00001", "2026-10-19 12:00+07")):
        conn.execute("INSERT INTO templates VALUES ('pay', %s, 'ERROR', %s, %s, %s)",
                     (f"pay:{tid}", f"text {tid}", first, first))
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES ('pay', 'pay:T00032', 'DOC-1')")
    conn.commit()
    for tid, rate, mean in (("T00032", 175, 36), ("T00141", 90, 30), ("T00309", 12, None), ("T00001", 5, 5)):
        hours(conn, "pay", f"pay:{tid}", date(2026, 10, 20), {2: (rate * 60, 0, 60), 3: (rate * 60, 0, 60)})
        for h in (2, 3):
            if mean:
                base(conn, "pay", f"pay:{tid}", "weekday", h, mean, 3)


def test_inspect_day_series_band_and_last_week(conn, cfg):
    seed_inspect(conn)
    r = inspect(conn, cfg, "pay", "*total", date(2026, 10, 20), 2, 4, NOW)
    h2 = r["day"][2]
    assert (round(h2["rate"]), h2["band_low"], h2["band_high"], round(h2["last_week"])) == (1840, 369.0, 711.0, 530)
    assert r["day"][5]["rate"] is None            # no service_hours row
    assert r["sentence"]["peak"]["hour"] == 2 and r["sentence"]["in_baseline"] is True
    assert r["sentence"]["keys"] == 1 and r["sentence"]["not_normal_minutes"] == 41
    assert r["keys"] == [{"key": "*total", "periods": [{"from": datetime(2026, 10, 20, 2, 10, tzinfo=TZ).timestamp(),
                                                        "to": datetime(2026, 10, 20, 2, 51, tzinfo=TZ).timestamp(),
                                                        "state": "ALERTING"}]}]


def test_inspect_contributors_increased_and_new(conn, cfg):
    seed_inspect(conn)
    r = inspect(conn, cfg, "pay", "*total", date(2026, 10, 20), 2, 4, NOW)
    assert [i["template_id"] for i in r["increased"]][:2] == ["pay:T00032", "pay:T00141"]
    top = r["increased"][0]
    assert (top["rate"], top["mean"], top["excess"], top["times_normal"]) == (175.0, 36.0, 139.0, 4.9)
    assert (top["doc_id"], top["doc_title"]) == ("DOC-1", "Recharge failures")
    assert all(i["excess"] > 0 for i in r["increased"]) and len(r["increased"]) <= 10
    assert [n["template_id"] for n in r["new"]] == ["pay:T00309"]   # first seen 01:30; T00001 (19 Oct) is not new
    assert r["new"][0]["rate"] == 12.0
    assert not {n["template_id"] for n in r["new"]} & {i["template_id"] for i in r["increased"]}
    assert r["left_days"] == [{"hour": 2, "days": 2, "of": 5}, {"hour": 3, "days": 4, "of": 5}]


def test_inspect_route_and_validation(client, conn):
    seed_inspect(conn)
    q = "/api/baseline/inspect?service=pay&key=*total&day=2026-10-20&hour_from=2&hour_to=4"
    assert client.get(q).get_json()["sentence"]["peak"]["hour"] == 2
    assert client.get(q.replace("key=*total", "key=nope")).status_code == 404
    assert client.get(q.replace("service=pay", "service=zzz")).status_code == 404
    r = client.get("/api/baseline/inspect?service=pay&key=*total&day=2026-10-20&hour_from=4&hour_to=4")
    assert (r.status_code, r.get_json()["error"]) == (400, "hours must be whole numbers with 0 <= hour_from < hour_to <= 24")
    assert client.get("/api/baseline/inspect?service=pay&key=*total&day=2026-10-20&hour_from=a&hour_to=4").status_code == 400
    r = client.get("/api/baseline/inspect?service=pay&key=*total&day=nope&hour_from=1&hour_to=2")
    assert (r.status_code, r.get_json()["error"]) == (400, "day must be an ISO date (YYYY-MM-DD)")


def test_inspect_band_for_hour_without_service_hours_row(conn, cfg):
    seed_inspect(conn)
    base(conn, "pay", "*total", "weekday", 5, 100, 10)
    base(conn, "pay", "*total", "weekday", 6, 100, 10)
    h5 = inspect(conn, cfg, "pay", "*total", date(2026, 10, 20), 2, 4, NOW)["day"][5]
    assert (h5["rate"], h5["band_low"], h5["band_high"]) == (None, 70.0, 130.0)


def seed_series(conn):
    service(conn, "pay")
    committed(conn, "pay", WEEKDAYS)
    for tid, level in (("T00032", "ERROR"), ("T00011", "INFO"), ("T00309", "ERROR")):
        conn.execute("INSERT INTO templates VALUES ('pay', %s, %s, 'boom', now(), now())", (f"pay:{tid}", level))
    conn.commit()
    for h in (2, 3, 10, 13, 14, 15, 18):
        base(conn, "pay", "pay:T00032", "weekday", h, 36, 6.5)
    base(conn, "pay", "*total", "weekday", 16, 500, 50)
    base(conn, "pay", "pay:T00011", "weekday", 16, 100, 10)
    t32 = "pay:T00032"
    hours(conn, "pay", t32, date(2026, 10, 20), {2: (792 * 60, 0, 60), 3: (792 * 60, 0, 60)})     # x22, hour 4 missing
    hours(conn, "pay", t32, TODAY, {13: (864 * 6, 0, 60), 14: (864 * 6, 0, 60)})                  # x2.4
    hours(conn, "pay", t32, date(2026, 10, 16), {10: (100 * 60, 0, 60)})
    hours(conn, "pay", t32, date(2026, 10, 12), {18: (100 * 60, 0, 60)})
    hours(conn, "pay", "*total", date(2026, 10, 15), {16: (225 * 60, 0, 60)})
    hours(conn, "pay", "pay:T00011", date(2026, 10, 15), {16: (45 * 60, 0, 60)})
    hours(conn, "pay", "pay:T00309", TODAY, {14: (600, 0, 60)})
    exclude(conn, "pay", date(2026, 10, 16), 10, 11)
    for at, frm, to in (("2026-10-19 19:10+00", "NORMAL", "ALERTING"), ("2026-10-19 19:51+00", "ALERTING", "NORMAL")):
        conn.execute("INSERT INTO alert_changes VALUES ('pay', %s, %s, %s, %s, 0)", (t32, at, frm, to))
    # alert minutes: today 13:00-15:00 (= 06:00-08:00 UTC) and Tue 20 Oct 02:10
    for mod, at, n in (("LIXI_PROMO", "2026-10-21 06:10+00", 3000), ("LIXI_PROMO", "2026-10-21 07:10+00", 2210),
                       ("API_TOPUP", "2026-10-21 06:11+00", 1180), ("API_TOPUP", "2026-10-19 19:10+00", 40)):
        conn.execute("INSERT INTO alert_minutes VALUES ('pay', %s, %s, %s, %s, %s)",
                     (t32, mod, TODAY if "10-21" in at else date(2026, 10, 20), at, n))
    for i in range(12):
        conn.execute("INSERT INTO error_samples VALUES ('pay', %s, %s, %s)",
                     (t32, f"2026-10-21 07:{i:02d}+00", f"ERROR LixiHandler {i}"))
    conn.commit()


def test_series_points_and_incidents(conn, cfg):
    seed_series(conn)
    r = series(conn, cfg, "pay", "pay:T00032", 14, TODAY, NOW)
    tue = [i for i in r["incidents"] if i["day"] == "2026-10-20"][0]
    assert (tue["hour_from"], tue["hour_to"], tue["ongoing"], tue["times_normal"]) == (2, 4, False, 22.0)
    assert (tue["peak"]["band_low"], tue["peak"]["band_high"], round(tue["peak"]["z"], 1)) == (16.5, 55.5, 116.3)
    today = [i for i in r["incidents"] if i["day"] == "2026-10-21"][0]
    assert today["ongoing"] and (today["hour_from"], today["hour_to"]) == (13, 15)
    assert r["points"][0]["day"] == "2026-10-08" and r["baseline_state"] == "full"
    assert next(p for p in r["points"] if (p["day"], p["hour"]) == ("2026-10-20", 4))["rate"] is None
    assert next(p for p in r["points"] if (p["day"], p["hour"]) == ("2026-10-21", 13))["last_week"] is None
    assert [p["hour"] for p in r["profile"]["today"]] == [13, 14] and r["profile"]["weekend"] == []
    assert r["profile"]["weekday"][0] == {"hour": 2, "mean": 36.0, "band_low": 16.5, "band_high": 55.5}


def test_series_decision_review_and_none(conn, cfg):
    seed_series(conn)
    d = {(i["day"], i["hour_from"]): i["decision"]
         for i in series(conn, cfg, "pay", "pay:T00032", 14, TODAY, NOW)["incidents"]}
    assert d[("2026-10-16", 10)] == "exclude" and d[("2026-10-20", 2)] == "review" and d[("2026-10-12", 18)] is None


def test_series_drops_only_for_service_keys(conn, cfg):
    seed_series(conn)
    assert not any(i["day"] == "2026-10-15" for i in series(conn, cfg, "pay", "pay:T00011", 14, TODAY, NOW)["incidents"])
    assert any(i["day"] == "2026-10-15" for i in series(conn, cfg, "pay", "*total", 14, TODAY, NOW)["incidents"])


def test_series_new_template_has_no_band(conn, cfg):
    seed_series(conn)
    r = series(conn, cfg, "pay", "pay:T00309", 1, TODAY, NOW)
    assert r["baseline_state"] == "none" and r["incidents"] == [] and r["profile"]["weekday"] == []
    assert all(p["band_high"] is None for p in r["points"])


def test_incident_modules_and_samples_only_when_ongoing(conn, cfg):
    seed_series(conn)
    live = incident(conn, "pay", "pay:T00032", date(2026, 10, 21), 13, 15, NOW)
    assert live["ongoing"] and live["modules"][0] == {"module": "LIXI_PROMO", "lines": 5210} and len(live["samples"]) == 10
    live = incident(conn, "pay", "*error", date(2026, 10, 21), 13, 15, NOW)
    assert live["modules"][1] == {"module": "API_TOPUP", "lines": 1180} and live["samples"][0]["template_id"] == "pay:T00032"
    old = incident(conn, "pay", "pay:T00032", date(2026, 10, 20), 2, 4, NOW)
    assert not old["ongoing"] and old["samples"] == [] and old["modules"] == [{"module": "API_TOPUP", "lines": 40}]


def test_series_validation(client, conn):
    service(conn, "pay")
    assert client.get("/api/baseline/series?service=pay&key=*total&days=5").status_code == 400
    assert client.get("/api/baseline/series?service=pay&key=pay:T09999").status_code == 404
    assert client.get("/api/baseline/series?service=pay").status_code == 200
    assert client.get("/api/baseline/incident?service=pay&key=*total&day=2026-10-21&hour_from=5&hour_to=5").status_code == 400
    assert client.get("/api/baseline/incident?service=pay&key=nope&day=2026-10-21&hour_from=1&hour_to=2").status_code == 404


def low_case(conn, key, mean, committed_days):
    service(conn, "pay")
    committed(conn, "pay", committed_days)
    base(conn, "pay", key, "weekday", 16, mean, mean / 10)
    hours(conn, "pay", key, date(2026, 10, 15), {16: (mean // 2 * 60, 0, 60)})      # half the mean: far below the band


@pytest.mark.parametrize("key, mean, days, low", [
    ("*total", 500, WEEKDAYS, True),            # full profile, mean >= drop.min_mean: the detector raises a drop
    ("*total", 500, WEEKDAYS[:2], False),       # young profile: no drop
    ("*total", 10, WEEKDAYS, False),            # mean below drop.min_mean: too quiet to call a drop
    ("*error", 500, WEEKDAYS, False),           # only *total drops
])
def test_low_side_follows_the_detector_drop_rule(conn, cfg, key, mean, days, low):
    low_case(conn, key, mean, days)
    c = cell(matrix(conn, cfg, "pay", key, 14, TODAY, NOW), "2026-10-15", 16)
    p = next(q for q in series(conn, cfg, "pay", key, 14, TODAY, NOW)["points"] if (q["day"], q["hour"]) == ("2026-10-15", 16))
    assert p["out"] is low and c["cls"].startswith("down") is low

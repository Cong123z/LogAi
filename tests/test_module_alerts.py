from datetime import datetime

import pytest

from logai2.module_alerts import data_until, module_alerts, parse_window
from logai2.timeutil import TZ


def t(h, m, day=14):
    return datetime(2026, 10, day, h, m, tzinfo=TZ).timestamp()


def dt(ts):
    return datetime.fromtimestamp(ts, TZ)


def seed_module(conn, module="M1", service="a"):
    conn.execute("INSERT INTO services VALUES (%s, 0) ON CONFLICT DO NOTHING", (service,))
    conn.execute("INSERT INTO modules (module_code, service) VALUES (%s, %s) ON CONFLICT DO NOTHING", (module, service))


def tmpl(conn, service, tid, text="boom", level="ERROR"):
    conn.execute("INSERT INTO templates VALUES (%s, %s, %s, %s, now(), now())", (service, tid, level, text))


def chg(conn, service, key, at, frm, to):
    conn.execute("INSERT INTO alert_changes VALUES (%s, %s, %s, %s, %s, 1, NULL, NULL)", (service, key, dt(at), frm, to))


def mins(conn, service, key, module, start_ts, n, count):
    for i in range(n):
        m = dt(start_ts + 60 * i)
        conn.execute("INSERT INTO alert_minutes VALUES (%s, %s, %s, %s, %s, %s)",
                     (service, key, module, m.date(), m, count))


def seed_basic(conn):
    """s:T1 in service a: minute 08:00 streak 1, 08:01 WARMING, 08:02..08:05 ALERTING, COOLING from 08:06."""
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 2), "NORMAL", "WARMING")
    chg(conn, "a", "a:T1", t(8, 3), "WARMING", "ALERTING")
    chg(conn, "a", "a:T1", t(8, 7), "ALERTING", "COOLING")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 9, 10)
    mins(conn, "a", "a:T1", "M2", t(8, 0), 9, 5)


def test_counts_from_first_high_minute_to_cooling(conn):
    seed_basic(conn)
    items = module_alerts(conn, "M1", t(8, 0), t(8, 15))
    assert len(items) == 1
    it = items[0]
    assert it["periods"] == [{"start": t(8, 0), "end": t(8, 6)}] and it["peak_state"] == "ALERTING"
    assert it["alerting_minutes"] == 6
    assert it["module"]["total"] == 60 and it["module"]["peak"] == 10 and it["module"]["avg"] == 10.0
    assert it["template"]["total"] == 90 and it["template"]["peak"] == 15
    assert (it["service"], it["template_id"], it["text"], it["level"]) == ("a", "a:T1", "boom", "ERROR")
    assert it["doc_id"] is None and it["doc_title"] is None
    assert len(it["module"]["series"]) == 6 and it["module"]["series"][0] == {"minute": t(8, 0), "count": 10}


def test_warming_that_falls_back(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 2), "NORMAL", "WARMING")
    chg(conn, "a", "a:T1", t(8, 3), "WARMING", "NORMAL")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 3, 4)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert it["peak_state"] == "WARMING" and it["alerting_minutes"] == 2
    assert it["module"]["total"] == 8


def test_started_before_window_and_still_open(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(7, 50), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 10, 3)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 10))[0]
    assert it["periods"] == [{"start": t(7, 49), "end": None}] and it["alerting_minutes"] == 10
    assert it["module"]["total"] == 30


def test_window_of_one_minute_inside_an_alert(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 0), "WARMING", "ALERTING")
    chg(conn, "a", "a:T1", t(8, 10), "ALERTING", "NORMAL")
    mins(conn, "a", "a:T1", "M1", t(7, 59), 10, 2)
    conn.execute("UPDATE alert_minutes SET count = 9 WHERE minute = %s", (dt(t(8, 3)),))
    it = module_alerts(conn, "M1", t(8, 3), t(8, 4))[0]
    assert it["alerting_minutes"] == 1 and it["module"]["total"] == 9 and it["module"]["peak"] == 9
    assert it["template"]["total"] == 9


def test_cooling_before_window_and_other_module_left_out(conn):
    seed_module(conn)
    for k in ("a:T1", "a:T2", "a:T3"):
        tmpl(conn, "a", k)
    chg(conn, "a", "a:T1", t(7, 0), "WARMING", "ALERTING")
    chg(conn, "a", "a:T2", t(7, 0), "WARMING", "ALERTING")
    chg(conn, "a", "a:T2", t(7, 59), "ALERTING", "COOLING")
    chg(conn, "a", "a:T3", t(7, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 5, 1)
    mins(conn, "a", "a:T2", "M1", t(8, 0), 5, 1)
    mins(conn, "a", "a:T3", "M2", t(8, 0), 5, 1)
    assert [i["template_id"] for i in module_alerts(conn, "M1", t(8, 0), t(8, 15))] == ["a:T1"]


def test_module_in_two_services(conn):
    # modules_pkey is (module_code) until Task 41 lands; drop it inside this test schema
    conn.execute("ALTER TABLE modules DROP CONSTRAINT modules_pkey")
    for svc in ("a", "b"):
        seed_module(conn, "M1", svc)
        tmpl(conn, svc, f"{svc}:T1")
        chg(conn, svc, f"{svc}:T1", t(8, 0), "WARMING", "ALERTING")
        mins(conn, svc, f"{svc}:T1", "M1", t(8, 0), 5, 2)
    items = module_alerts(conn, "M1", t(8, 0), t(8, 15))
    assert {i["service"] for i in items} == {"a", "b"}
    assert [i["service"] for i in items] == ["a", "b"]


def test_window_across_local_midnight(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(23, 59), "WARMING", "ALERTING")           # minute 23:58 on
    chg(conn, "a", "a:T1", t(0, 4, 15), "ALERTING", "NORMAL")          # first non-alerting minute 00:03
    mins(conn, "a", "a:T1", "M1", t(23, 58), 5, 7)                     # 23:58 .. 00:02
    items = module_alerts(conn, "M1", t(23, 55), t(23, 55) + 600)
    assert items[0]["module"]["total"] == 5 * 7


def test_series_per_hour_over_one_day(conn):
    seed_module(conn)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", t(8, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T1", "M1", t(8, 0), 90, 1)
    it = module_alerts(conn, "M1", t(0, 0), t(0, 0, 16))[0]
    assert it["module"]["series"] == [{"hour": t(8, 0), "count": 60}, {"hour": t(9, 0), "count": 30}]
    assert it["module"]["total"] == 90


def test_unknown_module_and_missing_template_row(conn):
    assert module_alerts(conn, "NOPE", t(8, 0), t(8, 15)) == []
    seed_module(conn)
    chg(conn, "a", "a:T9", t(8, 0), "WARMING", "ALERTING")
    mins(conn, "a", "a:T9", "M1", t(8, 0), 3, 1)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert it["text"] == "" and it["level"] == "INFO"


def test_doc_and_service_keys_never_returned(conn):
    seed_module(conn)
    for k in ("doc:DOC-1", "*error"):
        chg(conn, "a", k, t(8, 0), "WARMING", "ALERTING")
        mins(conn, "a", k, "M1", t(8, 0), 3, 1)
    assert module_alerts(conn, "M1", t(8, 0), t(8, 15)) == []


def test_doc_link_only_for_alerting_sources(conn):
    seed_basic(conn)
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('D1', 'Fix it', 'h', 'r')")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('a', 'a:T1', 'D1', 'draft')")
    assert module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]["doc_id"] is None
    conn.execute("UPDATE template_doc_links SET source = 'manual'")
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert (it["doc_id"], it["doc_title"]) == ("D1", "Fix it")


def test_data_until(conn):
    assert data_until(conn) is None
    conn.execute("""INSERT INTO worker_status (worker, status) VALUES ('engine', '{"heartbeat": 1000.0, "lag_seconds": 75.0}')""")
    assert data_until(conn) == 900.0


def test_parse_window():
    now = 1792461600.0                                  # 2026-10-20T02:00:00Z
    pw = lambda **a: parse_window(a, now, 90)
    assert pw(module_code=" M1 ", **{"from": "1792458000", "to": "1792458300"})[0] == "M1"
    assert pw(module_code="M", **{"from": "2026-10-20T01:00:00Z", "to": "1792461600"})[3:] == (1792458000.0, 1792461600.0)
    assert pw(module_code="M", **{"from": "1792458030500", "to": "1792458031200"})[3:] == (1792458000.0, 1792458060.0)
    assert pw(module_code="M", **{"from": "2026-10-20T01:00:59.999Z", "to": "2026-10-20T01:01:00.001Z"})[3:] \
        == (1792458000.0, 1792458120.0)
    assert pw(module_code="M", **{"from": "1792458000", "to": "1792458300"})[3:] == (1792458000.0, 1792458300.0)
    assert pw(module_code="M", **{"from": "1792458012", "to": "1792458012"})[3:] == (1792458000.0, 1792458060.0)
    assert pw(module_code="M", **{"from": "2026-10-20T08:00:00+07:00", "to": "1792458060"})[3:] \
        == (1792458000.0, 1792458060.0)                 # offset honoured
    assert pw(module_code="M", **{"from": "1792458030500", "to": "1792458031200"})[1:3] == (1792458030.5, 1792458031.2)
    oldest = datetime(2026, 7, 22, tzinfo=TZ).timestamp()       # local date of now is 2026-10-20
    assert pw(module_code="M", **{"from": "0", "to": "1792458000"})[3] == oldest
    assert pw(module_code="M", **{"from": "1792458000", "to": str(now + 3600)})[4] == now
    for bad in ({}, {"module_code": ""}, {"module_code": "M"}, {"module_code": "M", "from": "0"},
                {"module_code": "M", "to": "0"}, {"module_code": "M", "from": "abc", "to": "0"},
                {"module_code": "M", "from": "200", "to": "100"}):
        with pytest.raises(ValueError):
            parse_window(bad, now, 90)

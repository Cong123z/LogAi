from datetime import date, datetime, timedelta

import pytest

from logai2.db import ensure_partitions
from logai2.timeutil import TZ
from logai2.web import modules_view
from logai2.web.app import create_app
from logai2.web.modules_view import modules_for, window_hours
from tests.conftest import TEST_DSN

NOW = datetime(2026, 10, 20, 10, 30, tzinfo=TZ).timestamp()   # fixed: no midnight flake
TODAY = date(2026, 10, 20)


@pytest.fixture
def client(conn, cfg, schema, monkeypatch):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    monkeypatch.setattr(modules_view, "clock", lambda: NOW)
    ensure_partitions(conn, TODAY - timedelta(days=4), TODAY)
    return create_app(cfg).test_client()


def tmpl(conn, tid, service="pay"):
    conn.execute("INSERT INTO templates VALUES (%s, %s, 'INFO', %s, '2026-10-01Z', '2026-10-20Z')", (service, tid, tid))


def seen(conn, tid, module, last_seen="2026-10-20 03:00Z", service="pay"):
    conn.execute("INSERT INTO template_modules VALUES (%s, %s, %s, '2026-10-01Z', %s)", (service, tid, module, last_seen))


def hours(conn, tid, module, count, day=TODAY, hour=9, service="pay"):
    conn.execute("INSERT INTO template_module_hours VALUES (%s, %s, %s, %s, %s, %s)", (service, tid, module, day, hour, count))


def test_window_hours_crosses_midnight():
    w = window_hours(datetime(2026, 10, 20, 1, 30, tzinfo=TZ).timestamp())
    assert len(w) == 24 and w[0] == (date(2026, 10, 19), 2) and w[-1] == (TODAY, 1)


def test_modules_share_in_window(conn):
    ensure_partitions(conn, TODAY - timedelta(days=1), TODAY)
    seen(conn, "T1", "A"); seen(conn, "T1", "B")
    hours(conn, "T1", "A", 30); hours(conn, "T1", "B", 10)
    mods = modules_for(conn, [("pay", "T1")], NOW)[("pay", "T1")]
    assert [(m["module"], m["lines"], m["share"]) for m in mods] == [("A", 30, 0.75), ("B", 10, 0.25)]


def test_old_module_listed_without_share(client, conn):
    for m in "AC":
        seen(conn, "T1", m, "2026-10-17 03:00Z" if m == "C" else "2026-10-20 03:00Z")
    tmpl(conn, "T1")
    hours(conn, "T1", "A", 5)
    hours(conn, "T1", "C", 7, day=TODAY - timedelta(days=3))   # outside the window
    hours(conn, "T1", "A", 9, hour=10)                          # current hour counts
    hours(conn, "T1", "A", 1, hour=11)                          # future bucket: not in the window
    conn.commit()
    item = client.get("/api/templates?module=C").get_json()["items"][0]
    assert [(m["module"], m["lines"], m["share"]) for m in item["modules"]] == [("A", 14, 1.0), ("C", 0, None)]


def test_empty_module_listed(conn):
    seen(conn, "T1", ""); hours(conn, "T1", "", 3)
    assert modules_for(conn, [("pay", "T1")], NOW)[("pay", "T1")][0]["module"] == ""


def test_module_filter_orders_window_first_and_narrows(client, conn):
    for t in ("T1", "T2", "T3"):
        tmpl(conn, t)
    seen(conn, "T1", "A"); seen(conn, "T2", "A"); seen(conn, "T3", "B")
    hours(conn, "T2", "A", 4)   # only T2 has window lines in A
    conn.commit()
    body = client.get("/api/templates?module=A").get_json()
    assert [i["template_id"] for i in body["items"]] == ["T2", "T1"]
    assert body["total"] == 2 and body["stats"]["total"] == 2
    assert client.get("/api/templates?module=").get_json()["total"] == 0


def test_api_modules_counts(client, conn):
    seen(conn, "T1", "A"); seen(conn, "T2", "A"); seen(conn, "T2", "B"); seen(conn, "T9", "A", service="other")
    hours(conn, "T1", "A", 5); hours(conn, "T2", "A", 6); hours(conn, "T2", "B", 1, day=TODAY - timedelta(days=2))
    conn.commit()
    assert client.get("/api/modules?service=pay").get_json() == [
        {"module": "A", "templates": 2, "lines_24h": 11}, {"module": "B", "templates": 1, "lines_24h": 0}]
    assert client.get("/api/modules").status_code == 400

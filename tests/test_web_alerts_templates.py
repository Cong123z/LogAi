import time
from datetime import timedelta

import pytest

from logai2.db import ensure_partitions
from logai2.timeutil import day_hour, daytype, minute_of
from logai2.web.app import create_app
from tests.conftest import TEST_DSN


@pytest.fixture
def app(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg)


@pytest.fixture
def client(app):
    return app.test_client()


def add_template(conn, tid, text="user <*> logged in", level="INFO", service="pay", last_seen="2026-10-20 10:00Z"):
    conn.execute("INSERT INTO templates VALUES (%s, %s, %s, %s, '2026-10-19 10:00Z', %s)",
                 (service, tid, level, text, last_seen))


def add_doc(conn, doc_id, title="Card gateway down"):
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES (%s, %s, '', 'r1')", (doc_id, title))


def add_state(conn, key, state, count, low=None, high=None, service="pay"):
    conn.execute("INSERT INTO alert_state VALUES (%s, %s, %s, '2026-10-20 10:00Z', 1, 1.5, %s, %s, %s)",
                 (service, key, state, count, low, high))


def test_alerts_grouped_by_document(client, conn):
    add_doc(conn, "D1")
    for n in (1, 2, 3):
        add_template(conn, f"pay:T0000{n}", text=f"<b>timeout {n}</b>", level="ERROR")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES "
                 "('pay', 'pay:T00001', 'D1'), ('pay', 'pay:T00002', 'D1')")
    add_state(conn, "pay:T00001", "ALERTING", 40, 1.0, 9.0)
    add_state(conn, "pay:T00002", "WARMING", 12, 0.0, 6.0)
    add_state(conn, "pay:T00003", "NORMAL", 1, 0.0, 6.0)
    add_state(conn, "*total", "WARMING", 900, 100.0, 500.0)
    add_state(conn, "doc:D1", "WARMING", 52, 0.0, 13.0)
    day, hour = day_hour(minute_of(time.time()))
    for tid, mean, std in (("pay:T00001", 3.0, 2.0), ("pay:T00002", 1.0, 0.5)):
        conn.execute("INSERT INTO baseline VALUES ('pay', %s, %s, %s, %s, %s, 5)", (tid, daytype(day), hour, mean, std))
    conn.commit()

    rows = client.get("/api/alerts").get_json()
    assert [(r["kind"], r["state"]) for r in rows] == [("document", "ALERTING"), ("service", "WARMING")]
    doc, svc = rows
    assert doc["doc_id"] == "D1" and doc["title"] == "Card gateway down"
    assert [(t["template_id"], t["text"], t["count"], t["band_low"], t["band_high"], t["mean"], t["std"])
            for t in doc["templates"]] == [("pay:T00001", "<b>timeout 1</b>", 40, 1.0, 9.0, 3.0, 2.0),
                                           ("pay:T00002", "<b>timeout 2</b>", 12, 0.0, 6.0, 1.0, 0.5)]
    # the doc key's normal is the sum over its linked templates
    assert doc["totals"] == [{"key": "doc:D1", "state": "WARMING", "count": 52, "band_low": 0.0, "band_high": 13.0, "mean": 4.0, "std": 2.5}]
    assert isinstance(doc["since"], float)
    # service-wide and doc keys have no template text: their counts ride along for the card
    assert svc["totals"] == [{"key": "*total", "state": "WARMING", "count": 900, "band_low": 100.0, "band_high": 500.0,
                              "mean": None, "std": None}]


def test_templates_rate_and_band(client, conn):
    day, hour = day_hour(minute_of(time.time()))
    ensure_partitions(conn, day, day)
    dt = daytype(day)
    for svc in ("pay", "young"):
        add_template(conn, f"{svc}:T00001", service=svc, level="ERROR")
        conn.execute("INSERT INTO service_hours VALUES (%s, %s, %s, 10)", (svc, day, hour))
        conn.execute("INSERT INTO hourly_counts VALUES (%s, %s, %s, %s, 50, 300)", (svc, f"{svc}:T00001", day, hour))
        for h in (hour, (hour + 1) % 24):
            conn.execute("INSERT INTO baseline VALUES (%s, %s, %s, %s, 4.0, 1.0, 5)", (svc, f"{svc}:T00001", dt, h))
    # 'pay' has a full baseline (k 3); 'young' none committed yet (k 4)
    for i in range(1, 6):
        d = day - timedelta(days=7 * i)
        conn.execute("INSERT INTO day_status VALUES ('pay', %s, true, %s)", (d, dt))
    add_template(conn, "idle:T00001", service="idle")   # no service_hours row this hour → no rate
    conn.commit()

    items = {i["template_id"]: i for i in client.get("/api/templates?size=10").get_json()["items"]}
    assert (items["pay:T00001"]["rate"], items["pay:T00001"]["band_low"], items["pay:T00001"]["band_high"]) == (5.0, 1.0, 7.0)
    assert (items["young:T00001"]["band_low"], items["young:T00001"]["band_high"]) == (0.0, 8.0)
    assert items["idle:T00001"]["rate"] is None and items["idle:T00001"]["band_high"] is None


def test_templates_paginated_and_filtered(client, conn):
    for n in range(5):
        add_template(conn, f"pay:T0000{n}", text=f"Gateway TIMEOUT after {n}s" if n % 2 else f"ok {n}",
                     level="ERROR" if n < 3 else "WARN", last_seen=f"2026-10-20 10:0{n}Z")
    add_template(conn, "web:T00001", text="gateway timeout", service="web")
    conn.execute("INSERT INTO services VALUES ('web', 1), ('pay', 0)")
    add_doc(conn, "D1")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES ('pay', 'pay:T00001', 'D1')")
    conn.commit()

    body = client.get("/api/templates?service=pay&size=2&page=2").get_json()
    assert body["total"] == 5 and [i["template_id"] for i in body["items"]] == ["pay:T00002", "pay:T00001"]
    assert (body["items"][1]["doc_id"], body["items"][1]["doc_title"]) == ("D1", "Card gateway down")
    body = client.get("/api/templates?service=pay&q=timeout").get_json()
    assert body["total"] == 2 and {i["template_id"] for i in body["items"]} == {"pay:T00001", "pay:T00003"}
    body = client.get("/api/templates?service=pay&q=timeout&level=ERROR").get_json()
    assert [i["template_id"] for i in body["items"]] == ["pay:T00001"]
    assert client.get("/api/templates?q=GATEWAY").get_json()["total"] == 3   # all services
    assert client.get("/api/templates?page=x").status_code == 400
    assert client.get("/api/services").get_json() == ["pay", "web"]


def _review_fixture(conn):
    # (template, level, last_seen minute, link source or None)
    rows = [("pay:T00001", "INFO", 5, "corpus"), ("pay:T00002", "ERROR", 3, "llm"),
            ("pay:T00003", "WARN", 0, "inherited"), ("pay:T00004", "ERROR", 1, "draft"),
            ("pay:T00005", "ERROR", 9, "manual"), ("pay:T00006", "ERROR", 8, None),
            ("web:T00001", "ERROR", 7, "corpus")]
    add_doc(conn, "D1")
    for tid, level, m, source in rows:
        svc = tid.split(":")[0]
        add_template(conn, tid, level=level, service=svc, last_seen=f"2026-10-20 10:0{m}Z")
        if source:
            conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                         "VALUES (%s, %s, 'D1', %s)", (svc, tid, source))
    add_state(conn, "pay:T00003", "ALERTING", 20)
    conn.commit()


def test_review_filter_and_order(client, conn):
    _review_fixture(conn)
    body = client.get("/api/templates?link_source=review&service=pay").get_json()
    assert [i["template_id"] for i in body["items"]] == ["pay:T00003", "pay:T00002", "pay:T00004", "pay:T00001"]
    assert body["total"] == 4
    assert client.get("/api/templates?link_source=bogus").status_code == 400


def test_review_total_follows_filters(client, conn):
    _review_fixture(conn)
    get = lambda q: client.get(f"/api/templates?{q}").get_json()["review_total"]
    assert get("") == 5
    assert get("service=pay") == 4
    assert get("service=pay&level=ERROR") == 2
    assert get("link_source=manual") == 5     # link_source does not narrow the count
    assert get("q=nothing-matches") == 0


def test_stats_count_levels_and_known(client, conn):
    _review_fixture(conn)
    get = lambda q: client.get(f"/api/templates?{q}").get_json()["stats"]
    # pay: corpus, llm, inherited and manual are known; the draft and the unlinked one are not
    assert get("service=pay") == {"total": 6, "error": 4, "warn": 1, "info": 1, "known": 4, "unknown": 2}
    assert get("service=pay&link_source=none") == get("service=pay")   # link_source does not narrow the cards
    assert get("level=ERROR")["known"] == 3


def test_status_filter_known_unknown(client, conn):
    _review_fixture(conn)
    get = lambda q: client.get(f"/api/templates?service=pay&{q}").get_json()
    known = get("status=known")
    assert sorted(i["template_id"] for i in known["items"]) == ["pay:T00001", "pay:T00002", "pay:T00003", "pay:T00005"]
    assert known["total"] == 4 and all(i["known"] for i in known["items"])
    unknown = get("status=unknown")
    assert sorted(i["template_id"] for i in unknown["items"]) == ["pay:T00004", "pay:T00006"]
    assert not any(i["known"] for i in unknown["items"])
    assert unknown["stats"]["total"] == 6 and unknown["review_total"] == 4   # status narrows only the list
    assert get("status=unknown&link_source=review")["total"] == 1             # the draft
    assert client.get("/api/templates?status=bogus").status_code == 400


def test_link_unknown_doc_404_and_link_enqueues_reload(client, conn):
    add_template(conn, "pay:T00001")
    add_doc(conn, "D1")
    conn.commit()
    assert [(d["doc_id"], d["title"]) for d in client.get("/api/documents").get_json()] == [("D1", "Card gateway down")]
    url = "/api/templates/pay/pay:T00001/link"
    assert client.put(url, json={"doc_id": "nope"}).status_code == 404
    assert client.put("/api/templates/pay/pay:T09999/link", json={"doc_id": "D1"}).status_code == 404
    assert client.put(url, json={"doc_id": 5}).status_code == 400
    assert conn.execute("SELECT count(*) AS n FROM commands").fetchone()["n"] == 0

    assert client.put(url, json={"doc_id": "D1"}).status_code == 200
    assert conn.execute("SELECT doc_id FROM template_doc_links").fetchall() == [{"doc_id": "D1"}]
    assert client.put(url, json={"doc_id": None}).status_code == 200      # sticky "no document"
    assert conn.execute("SELECT doc_id, source FROM template_doc_links").fetchall() == [{"doc_id": None, "source": "manual"}]
    assert client.delete(url).status_code == 200                           # back to automatic
    assert conn.execute("SELECT count(*) AS n FROM template_doc_links").fetchone()["n"] == 0
    assert [r["kind"] for r in conn.execute("SELECT kind FROM commands ORDER BY id")] == ["reload_links"] * 3


def test_every_error_is_json(app):
    app.add_url_rule("/api/boom", "boom", lambda: 1 / 0)
    client = app.test_client()
    r = client.get("/api/boom")
    assert r.status_code == 500 and r.get_json() == {"error": "Internal server error"}
    r = client.get("/api/nothing-here")
    assert r.status_code == 404 and "error" in r.get_json()
    r = client.delete("/api/alerts")
    assert r.status_code == 405 and "GET" in r.headers["Allow"] and "error" in r.get_json()


def test_verify_certs_must_be_boolean(client):
    r = client.put("/api/sources/connection", json={"hosts": ["http://es:9200"], "username": "u", "password": "p",
                                                    "verify_certs": "false", "revision": None})
    assert r.status_code == 400 and "verify_certs" in r.get_json()["error"]


def test_echarts_is_served_locally(client):
    r = client.get("/static/vendor/echarts.min.js")
    assert r.status_code == 200 and b"echarts" in r.data[:4096]

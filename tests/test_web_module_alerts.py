from datetime import datetime
from urllib.parse import urlencode

import pytest

from logai2.timeutil import TZ
from logai2.web.app import create_app
from tests.conftest import TEST_DSN
from tests.test_module_alerts import chg, mins, seed_basic, seed_module, tmpl


def local_iso(h, m, s=0, ms=0):
    return datetime(2026, 10, 14, h, m, s, ms * 1000, tzinfo=TZ).isoformat(timespec="milliseconds")


@pytest.fixture
def app(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg)


@pytest.fixture
def client(app):
    return app.test_client()


def get(client, module="M1", frm=None, to=None, **kw):
    q = {"module_code": module, "from": frm or local_iso(8, 0, 30, 500), "to": to or local_iso(8, 14, 10, 200)}
    return client.get("/api/module-alerts?" + urlencode(q), **kw)


def test_shape(client, conn):
    seed_basic(conn)
    conn.commit()
    conn.execute("""INSERT INTO worker_status (worker, status) VALUES ('engine', '{"heartbeat": 1792461600.0, "lag_seconds": 75.0}')""")
    conn.commit()
    body = get(client).get_json()
    assert body["module_code"] == "M1"
    assert body["from"] == "2026-10-14T01:00:30.500Z" and body["to"] == "2026-10-14T01:14:10.200Z"
    assert body["effective_from"] == "2026-10-14T01:00:00Z" and body["effective_to"] == "2026-10-14T01:15:00Z"
    assert body["resolution"] == "minute" and body["unit"] == "events_per_minute"
    assert body["data_until"] == "2026-10-20T01:58:00Z"
    it = body["templates"][0]
    assert it["module"]["total"] == 60 and it["peak_state"] == "ALERTING"
    assert it["periods"][0] == {"start": "2026-10-14T01:00:00Z", "end": "2026-10-14T01:06:00Z"}
    assert it["module"]["series"][0] == {"minute": "2026-10-14T01:00:00Z", "count": 10}


def test_hourly_series_formatted(client, conn):
    seed_basic(conn)
    conn.commit()
    body = get(client, frm=local_iso(8, 0), to="2026-10-16T08:00:00+07:00").get_json()
    assert body["templates"][0]["module"]["series"][0] == {"hour": "2026-10-14T01:00:00Z", "count": 60}


def test_unknown_module_empty_200(client):
    r = get(client, module="nope")
    assert r.status_code == 200 and r.get_json()["templates"] == []


@pytest.mark.parametrize("qs", ["", "module_code=&from=0&to=60", "module_code=M", "module_code=M&from=0",
                                "module_code=M&to=60", "module_code=M&from=abc&to=60",
                                "module_code=M&from=200&to=100"])
def test_bad_input_400(client, qs):
    r = client.get("/api/module-alerts?" + qs)
    assert r.status_code == 400 and r.get_json()["error"]


def test_module_code_unicode_and_slash_round_trip(client, conn):
    code = "VTN/066 é"
    seed_module(conn, code)
    tmpl(conn, "a", "a:T1")
    chg(conn, "a", "a:T1", datetime(2026, 10, 14, 8, 3, tzinfo=TZ).timestamp(), "NORMAL", "ALERTING")
    mins(conn, "a", "a:T1", code, datetime(2026, 10, 14, 8, 0, tzinfo=TZ).timestamp(), 5, 7)
    conn.commit()
    body = get(client, module=code).get_json()
    assert body["module_code"] == code and body["templates"][0]["module"]["total"] > 0


def test_data_until_null_without_engine_row(client):
    assert get(client).get_json()["data_until"] is None


def test_token(conn, cfg, schema):
    cfg.web.api_token = "s3cret"
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    c = create_app(cfg).test_client()
    r = get(c)
    assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}
    assert get(c, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert get(c, headers={"Authorization": "s3cret"}).status_code == 401
    assert get(c, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/alerts").status_code == 200
    assert c.get("/api/module-alerts").status_code == 401   # token check runs before parsing

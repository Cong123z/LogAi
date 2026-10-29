import json
import time

import pytest
from psycopg.types.json import Jsonb

from logai2.web.app import create_app
from tests.conftest import TEST_DSN


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def test_selection_round_trip_and_command(client, conn):
    before = time.time()
    r = client.put("/api/sources/selection", json={"patterns": ["app-*", "app-*", "db-logs"], "revision": None})
    assert r.status_code == 200
    body = client.get("/api/sources").get_json()
    entries = body["selection"]["entries"]
    assert [e["pattern"] for e in entries] == ["app-*", "db-logs"]
    assert all(e["added_at"] >= before for e in entries)
    assert conn.execute("SELECT kind FROM commands").fetchall() == [{"kind": "reload_sources"}]

    # an existing pattern keeps its added_at
    first = entries[0]["added_at"]
    client.put("/api/sources/selection", json={"patterns": ["app-*"], "revision": body["selection"]["revision"]})
    again = client.get("/api/sources").get_json()["selection"]["entries"]
    assert again == [{"pattern": "app-*", "added_at": first, "enabled": True, "enabled_at": None}]


def test_selection_configured_index_keeps_engine_start(client, cfg):
    # before the first save the engine reads cfg's index from added_at 0; saving it must not skip logs
    client.put("/api/sources/selection", json={"patterns": [cfg.elasticsearch.index], "revision": None})
    entries = client.get("/api/sources").get_json()["selection"]["entries"]
    assert entries == [{"pattern": cfg.elasticsearch.index, "added_at": 0, "enabled": True, "enabled_at": None}]


def test_selection_invalid_pattern_400(client):
    r = client.put("/api/sources/selection", json={"patterns": ["Bad,Index"], "revision": None})
    assert r.status_code == 400 and "error" in r.get_json()


def test_selection_stale_revision_409(client):
    rev = client.put("/api/sources/selection", json={"patterns": ["a"], "revision": None}).get_json()["revision"]
    client.put("/api/sources/selection", json={"patterns": ["b"], "revision": rev})
    r = client.put("/api/sources/selection", json={"patterns": ["c"], "revision": rev})
    assert r.status_code == 409
    current = client.get("/api/sources").get_json()["selection"]["revision"]
    assert r.get_json() == {"error": "stale", "revision": current}
    # a client that never saw the saved row cannot overwrite it either
    assert client.put("/api/sources/selection", json={"patterns": ["c"], "revision": None}).status_code == 409


def test_password_never_returned(client):
    client.put("/api/sources/connection", json={"hosts": ["http://es:9200"], "username": "u",
                                                "password": "p", "verify_certs": True, "revision": None})
    body = client.get("/api/sources").get_json()
    # the brief's `"p" not in json.dumps(...)` can never pass ("http", "has_password"): check the value instead
    assert '"p"' not in json.dumps(body) and body["connection"]["has_password"]
    assert "password" not in body["connection"]


def test_empty_password_keeps_saved_on_same_host(client, conn):
    rev = client.put("/api/sources/connection", json={"hosts": ["http://es:9200"], "username": "u",
                                                      "password": "secret", "verify_certs": True,
                                                      "revision": None}).get_json()["revision"]
    r = client.put("/api/sources/connection", json={"hosts": ["http://ES:9200/"], "username": "v",
                                                    "password": "", "verify_certs": False, "revision": rev})
    assert r.status_code == 200 and "secret" not in r.get_data(as_text=True)
    saved = conn.execute("SELECT value FROM settings WHERE key = 'es_connection'").fetchone()["value"]
    assert saved == {"hosts": ["http://ES:9200"], "username": "v", "password": "secret", "verify_certs": False}
    assert [r["kind"] for r in conn.execute("SELECT kind FROM commands")] == ["reload_sources"] * 2


def test_empty_password_on_new_host_rejected(client):
    rev = client.put("/api/sources/connection", json={"hosts": ["http://es:9200"], "username": "u",
                                                      "password": "p", "verify_certs": True,
                                                      "revision": None}).get_json()["revision"]
    r = client.put("/api/sources/connection", json={"hosts": ["http://other:9200"], "username": "u",
                                                    "password": "", "verify_certs": True, "revision": rev})
    assert r.status_code == 400 and r.get_json() == {"error": "password required"}


def test_pipeline_stale_flag(client, conn):
    assert client.get("/api/pipeline").get_json()["stale"] is True   # engine never ran

    def beat(at):
        with conn.transaction():
            conn.execute("INSERT INTO worker_status (worker, status) VALUES ('engine', %s) "
                         "ON CONFLICT (worker) DO UPDATE SET status = EXCLUDED.status",
                         (Jsonb({"heartbeat": at, "lag_seconds": 1.5}),))

    beat(time.time())
    conn.execute("INSERT INTO es_cursors (index, since_ms, floor_ms) VALUES ('app-1', 5, 1)")
    conn.commit()
    body = client.get("/api/pipeline").get_json()
    assert body["stale"] is False and body["status"]["lag_seconds"] == 1.5
    assert body["cursors"] == [{"index": "app-1", "since_ms": 5, "floor_ms": 1}]
    beat(time.time() - 31)
    assert client.get("/api/pipeline").get_json()["stale"] is True


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"LogAI2 Console" in r.data
    assert client.get("/static/js/views/sources.js").status_code == 200


def put(client, patterns, disabled=None):
    rev = client.get("/api/sources").get_json()["selection"]["revision"]
    body = {"patterns": patterns, "revision": rev}
    if disabled is not None:
        body["disabled"] = disabled
    return client.put("/api/sources/selection", json=body)


def entries(client):
    return {e["pattern"]: e for e in client.get("/api/sources").get_json()["selection"]["entries"]}


def test_selection_disable_and_reenable(client):
    put(client, ["a", "b"])
    before = entries(client)
    assert before["b"]["enabled"] is True and before["b"]["enabled_at"] is None
    put(client, ["a", "b"], disabled=["b"])
    off = entries(client)
    assert off["b"]["enabled"] is False and off["a"]["enabled"] is True
    assert off["b"]["added_at"] == before["b"]["added_at"] and off["a"]["enabled_at"] is None
    t_switch = time.time()
    put(client, ["a", "b"], disabled=[])
    on = entries(client)
    assert on["b"]["enabled"] is True and t_switch <= on["b"]["enabled_at"] <= time.time()
    # a save that leaves it On keeps enabled_at
    put(client, ["a", "b"])
    assert entries(client)["b"]["enabled_at"] == on["b"]["enabled_at"]


def test_selection_disabled_must_be_selected(client):
    r = put(client, ["a"], disabled=["zzz"])
    assert r.status_code == 400 and r.get_json()["error"] == "disabled must name selected patterns"
    assert put(client, ["a"], disabled="a").status_code == 400


def test_selection_without_enabled_is_on(client, conn):
    from logai2.db import put_setting
    put_setting(conn, "index_selection", {"entries": [{"pattern": "a", "added_at": 0}]})
    conn.commit()
    assert entries(client)["a"] == {"pattern": "a", "added_at": 0, "enabled": True, "enabled_at": None}

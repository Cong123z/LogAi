import json

import pytest

from logai2.web.app import create_app
from tests.conftest import TEST_DSN


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def test_profiles_crud_key_is_write_only_and_one_active(client, conn):
    put = lambda name, **kw: client.put(f"/api/llm/profiles/{name}", json=kw)
    r = put("gemini", base_url="https://proxy.example/v1/", model="gemini-3.8-flash", api_key="sk-1", revision=None)
    assert r.status_code == 200
    rev = r.get_json()["revision"]
    assert put("gemini", base_url="https://x/v1", model="m", revision=None).status_code == 409   # stale
    assert put("gemini", base_url="https://x/v1", model="m2", api_key="", revision=rev).status_code == 200
    assert conn.execute("SELECT api_key, base_url FROM llm_profiles").fetchone() == {"api_key": "sk-1", "base_url": "https://x/v1"}
    assert put("deep", base_url="http://d", model="deepseek", revision=None).status_code == 200
    listed = client.get("/api/llm/profiles").get_json()
    assert "sk-1" not in json.dumps(listed) and [p["has_api_key"] for p in listed] == [False, True]

    activate = lambda name, active: client.post(f"/api/llm/profiles/{name}/activate", json={"active": active})
    assert put("deep", base_url="http://d", model="deepseek", priority=5,
               revision=client.get("/api/llm/profiles").get_json()[0]["revision"]).status_code == 200
    assert activate("gemini", True).status_code == 200 and activate("deep", True).status_code == 200
    # every active profile is used, lowest priority first (gemini got 1 as the first profile)
    assert [(p["name"], p["priority"]) for p in client.get("/api/llm/profiles").get_json() if p["active"]] == [
        ("gemini", 1), ("deep", 5)]
    assert activate("deep", False).status_code == 200
    assert [p["name"] for p in client.get("/api/llm/profiles").get_json() if p["active"]] == ["gemini"]
    assert activate("nope", True).status_code == 404 and activate("deep", "yes").status_code == 400

    for bad in ({"base_url": "ftp://x", "model": "m"}, {"base_url": "https://u:p@x", "model": "m"},
                {"base_url": "https://x", "model": ""}, {"base_url": "https://x", "model": "m", "timeout_seconds": 1},
                {"base_url": "https://x", "model": "m", "api_key": "a\nb"},
                {"base_url": "https://x", "model": "m", "priority": -1}):
        assert put("bad", revision=None, **bad).status_code == 400, bad
    assert put("bad name!", base_url="https://x", model="m", revision=None).status_code == 400
    assert client.delete("/api/llm/profiles/deep").status_code == 200
    assert client.delete("/api/llm/profiles/deep").status_code == 404


def test_profile_key_must_be_ascii_and_name_has_no_newline(client):
    assert client.put("/api/llm/profiles/p", json={"base_url": "https://x", "model": "m", "api_key": "kéy",
                                                   "revision": None}).status_code == 400
    assert client.put("/api/llm/profiles/p%0A", json={"base_url": "https://x", "model": "m",
                                                      "revision": None}).status_code == 400


def test_usage_counts_llm_results_and_reports_the_linker(client, conn):
    conn.execute("INSERT INTO templates VALUES ('s', 'A', 'ERROR', 'a', now(), now()), ('s', 'B', 'ERROR', 'b', now(), now()), "
                 "('s', 'C', 'ERROR', 'c', now(), now())")
    conn.execute("INSERT INTO documents (doc_id, title, handling, status, revision) VALUES ('D1', 't', 'h', 'active', 'r'), "
                 "('D2', 't', 'h', 'draft', 'r')")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source, linked_at) VALUES "
                 "('s', 'A', 'D1', 'llm', now()), ('s', 'B', 'D2', 'draft', now() - interval '2 hours'), "
                 "('s', 'C', 'D1', 'manual', now())")
    conn.execute("INSERT INTO worker_status VALUES ('linker', %s, now())",
                 ('{"heartbeat": 9999999999, "llm": {"confirmed": 1, "suggested": 0, "none": 0, "failed": 0}, '
                  '"llm_last_error": "llm: timeout", "warnings": [], '
                  '"llm_profiles": {"p": {"down_until": 1, "error": "502"}}}',))
    conn.commit()
    u = client.get("/api/llm/usage").get_json()
    assert u["hour"] == {"llm": 1, "draft": 0} and u["day"] == {"llm": 1, "draft": 1} and u["drafts_waiting"] == 1
    assert u["linker_running"] and u["last_error"] == "llm: timeout" and u["limits"]["daily_max"] == 0
    assert u["profiles"] == {"p": {"down_until": 1, "error": "502"}}


def put_profile(client, name, **kw):
    assert client.put(f"/api/llm/profiles/{name}", json={"base_url": "http://x/v1", "model": "m", "api_key": "k",
                                                         "revision": None, **kw}).status_code == 200


def listed(client):
    return {r["name"]: (r["active"], r["priority"]) for r in client.get("/api/llm/profiles").get_json()}


def test_profile_order_sets_priority_and_active(client):
    for name in ("a", "b", "c"):
        put_profile(client, name)
    assert client.put("/api/llm/profiles/order", json={"order": ["c", "a"]}).get_json() == {"order": ["c", "a"]}
    rows = listed(client)
    assert rows["c"] == (True, 1) and rows["a"] == (True, 2) and rows["b"][0] is False


def test_profile_order_rejects_bad_bodies_and_changes_nothing(client):
    put_profile(client, "a")
    before = listed(client)
    for order in (["a", "zz"], ["a", "a"], "a", [1], None):
        assert client.put("/api/llm/profiles/order", json={"order": order}).status_code == 400, order
    assert listed(client) == before


def test_new_profile_priority_goes_last_and_edit_keeps_it(client):
    put_profile(client, "a")
    assert listed(client)["a"] == (False, 1)            # no active profile: priority 1
    client.put("/api/llm/profiles/order", json={"order": ["a"]})
    put_profile(client, "b")
    assert listed(client)["b"] == (False, 2)            # max(active) + 1
    client.put("/api/llm/profiles/order", json={"order": ["b", "a"]})
    rev = {r["name"]: r["revision"] for r in client.get("/api/llm/profiles").get_json()}["b"]
    put_profile(client, "b", model="m2", revision=rev)  # an edit without priority keeps it
    assert listed(client)["b"] == (True, 1)
    put_profile(client, "c", priority=7)                # an explicit priority still works
    assert listed(client)["c"][1] == 7

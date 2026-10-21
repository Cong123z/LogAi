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
    assert activate("gemini", True).status_code == 200 and activate("deep", True).status_code == 200
    assert [p["name"] for p in client.get("/api/llm/profiles").get_json() if p["active"]] == ["deep"]
    assert activate("deep", False).status_code == 200
    assert not any(p["active"] for p in client.get("/api/llm/profiles").get_json())
    assert activate("nope", True).status_code == 404 and activate("deep", "yes").status_code == 400

    for bad in ({"base_url": "ftp://x", "model": "m"}, {"base_url": "https://u:p@x", "model": "m"},
                {"base_url": "https://x", "model": ""}, {"base_url": "https://x", "model": "m", "timeout_seconds": 1},
                {"base_url": "https://x", "model": "m", "api_key": "a\nb"}):
        assert put("bad", revision=None, **bad).status_code == 400, bad
    assert put("bad name!", base_url="https://x", model="m", revision=None).status_code == 400
    assert client.delete("/api/llm/profiles/deep").status_code == 200
    assert client.delete("/api/llm/profiles/deep").status_code == 404


def test_profile_key_must_be_ascii_and_name_has_no_newline(client):
    assert client.put("/api/llm/profiles/p", json={"base_url": "https://x", "model": "m", "api_key": "kéy",
                                                   "revision": None}).status_code == 400
    assert client.put("/api/llm/profiles/p%0A", json={"base_url": "https://x", "model": "m",
                                                      "revision": None}).status_code == 400

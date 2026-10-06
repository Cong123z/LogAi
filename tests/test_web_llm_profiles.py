"""Web API for LLM profiles: keys go in, only hints come out."""
from __future__ import annotations

import json
import time

from test_web_grouping_api import _client

from logai.incident.profiles import LLMProfileStore
from logai.storage.grouping import GroupingOverrideStore

KEY = "sk-lXSTpg002it6f972aW8iAgVxM3CZxMYlF0cluYT562piZW7h"
NEW = {"name": "Zen", "endpoint": "https://zendigikey.shop", "api_key": KEY,
       "model": "gpt-5.6-luna", "activate": True}


def test_create_list_and_never_leak_key():
    temporary, base, client = _client()
    try:
        response = client.post("/api/llm-profiles", json=NEW)
        assert response.status_code == 201
        created = response.get_json()["profile"]
        assert created["endpoint"] == "https://zendigikey.shop/v1/chat/completions"
        listing = client.get("/api/llm-profiles")
        body = listing.get_json()
        assert body["active_profile_id"] == created["id"]
        assert body["profiles"][0]["api_key_hint"] == "sk-…W7h"
        assert KEY not in listing.get_data(as_text=True) and KEY not in response.get_data(as_text=True)
        assert LLMProfileStore(base / "llm_profiles.json").active_profile()["api_key"] == KEY
    finally:
        temporary.cleanup()


def test_update_switch_delete():
    temporary, base, client = _client()
    try:
        a = client.post("/api/llm-profiles", json=NEW).get_json()["profile"]["id"]
        b = client.post("/api/llm-profiles", json={**NEW, "name": "Local", "activate": False,
                                                    "endpoint": "http://ollama:11434/v1"}).get_json()["profile"]["id"]
        updated = client.put(f"/api/llm-profiles/{a}", json={"name": "Zen 2", "api_key": ""})
        assert updated.status_code == 200 and updated.get_json()["profile"]["name"] == "Zen 2"
        store = LLMProfileStore(base / "llm_profiles.json")
        assert store.get(a)["api_key"] == KEY
        response = client.delete(f"/api/llm-profiles/{a}")
        assert response.status_code == 409 and response.get_json()["error"] == "profile_active"
        assert client.put("/api/llm-profiles/active", json={"profile_id": b}).status_code == 200
        assert store.active_profile()["id"] == b
        assert client.delete(f"/api/llm-profiles/{a}").status_code == 200
        assert client.put("/api/llm-profiles/active", json={"profile_id": None}).status_code == 200
        assert store.active_profile() is None
    finally:
        temporary.cleanup()


def test_errors():
    temporary, base, client = _client()
    try:
        bad = client.post("/api/llm-profiles", json={**NEW, "endpoint": "zendigikey.shop"})
        assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_request"
        assert client.post("/api/llm-profiles", data="x").status_code == 400
        assert client.put("/api/llm-profiles/llm-missing", json={"name": "x"}).status_code == 404
        assert client.delete("/api/llm-profiles/llm-missing").status_code == 404
        assert client.put("/api/llm-profiles/active", json={"profile_id": "llm-missing"}).status_code == 404
    finally:
        temporary.cleanup()


def test_listing_reports_engine_applied_profile():
    temporary, base, client = _client()
    try:
        pid = client.post("/api/llm-profiles", json=NEW).get_json()["profile"]["id"]
        GroupingOverrideStore(base / "grouping_overrides.json", base / "grouping_status.json") \
            .update_heartbeat(time.time(), runtime={"llm_enabled": True, "llm_profile_id": pid})
        engine = client.get("/api/llm-profiles").get_json()["engine"]
        assert engine == {"alive": True, "llm_enabled": True, "llm_profile_id": pid}
    finally:
        temporary.cleanup()


def test_corrupt_profile_file_reports_error_and_is_kept():
    temporary, base, client = _client()
    try:
        (base / "llm_profiles.json").write_text("{oops", encoding="utf-8")
        listing = client.get("/api/llm-profiles")
        assert listing.status_code == 500 and listing.get_json()["error"] == "profiles_unreadable"
        created = client.post("/api/llm-profiles", json=NEW)
        assert created.status_code == 500
        assert (base / "llm_profiles.json").read_text(encoding="utf-8") == "{oops"
    finally:
        temporary.cleanup()


def test_endpoint_host_change_needs_key_via_api():
    temporary, base, client = _client()
    try:
        pid = client.post("/api/llm-profiles", json=NEW).get_json()["profile"]["id"]
        response = client.put(f"/api/llm-profiles/{pid}", json={"endpoint": "http://attacker:8080"})
        assert response.status_code == 400 and "API key" in response.get_json()["message"]
    finally:
        temporary.cleanup()

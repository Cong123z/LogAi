"""LLM profiles: stored endpoint/api_key/model sets, one active for the engine."""
from __future__ import annotations

import json
import os
import stat

import pytest

from logai.incident.profiles import (
    LLMProfileStore,
    ProfileError,
    key_hint,
    normalize_endpoint,
)


def test_normalize_endpoint():
    assert normalize_endpoint("https://zendigikey.shop") == "https://zendigikey.shop/v1/chat/completions"
    assert normalize_endpoint("https://x.io/v1/") == "https://x.io/v1/chat/completions"
    assert normalize_endpoint(" http://h:8000/v1/chat/completions ") == "http://h:8000/v1/chat/completions"
    assert normalize_endpoint("http://ollama:11434/api/custom") == "http://ollama:11434/api/custom"
    for bad in ["", "ftp://x", "zendigikey.shop", "https://"]:
        with pytest.raises(ProfileError):
            normalize_endpoint(bad)


def test_key_hint():
    assert key_hint("sk-lXSTpg002it6f972aW8iAgVxM3CZxMYlF0cluYT562piZW7h") == "sk-…W7h"
    assert key_hint("short") == "•••"
    assert key_hint("") == ""


def test_create_activate_and_file_mode(tmp_path):
    store = LLMProfileStore(tmp_path / "llm_profiles.json")
    created = store.create(name="Zen", endpoint="https://zendigikey.shop",
                           api_key="sk-secret-123456", model="gpt-5.6-luna", activate=True)
    assert "api_key" not in created and created["api_key_hint"] == "sk-…456"
    assert created["endpoint"] == "https://zendigikey.shop/v1/chat/completions"
    active = store.active_profile()
    assert active["api_key"] == "sk-secret-123456" and active["model"] == "gpt-5.6-luna"
    assert stat.S_IMODE(os.stat(tmp_path / "llm_profiles.json").st_mode) == 0o600
    view = store.public_view()
    assert view["active_profile_id"] == created["id"]
    assert "sk-secret-123456" not in json.dumps(view)


def test_update_keeps_key_when_blank(tmp_path):
    store = LLMProfileStore(tmp_path / "llm_profiles.json")
    pid = store.create(name="A", endpoint="https://a.io", api_key="sk-old-key-0001", model="m")["id"]
    store.update(pid, name="B", api_key="")
    assert store.get(pid)["api_key"] == "sk-old-key-0001" and store.get(pid)["name"] == "B"
    store.update(pid, api_key="sk-new-key-0002", model="m2")
    assert store.get(pid)["api_key"] == "sk-new-key-0002" and store.get(pid)["model"] == "m2"


def test_switch_and_delete_rules(tmp_path):
    store = LLMProfileStore(tmp_path / "llm_profiles.json")
    a = store.create(name="A", endpoint="https://a.io", api_key="k1", model="m", activate=True)["id"]
    b = store.create(name="B", endpoint="https://b.io", api_key="k2", model="m")["id"]
    assert store.active_profile()["id"] == a
    with pytest.raises(ProfileError):
        store.delete(a)  # active
    store.set_active(b)
    store.delete(a)
    assert [p["id"] for p in store.public_view()["profiles"]] == [b]
    store.set_active(None)
    assert store.active_profile() is None
    with pytest.raises(KeyError):
        store.set_active("missing")
    with pytest.raises(KeyError):
        store.update("missing", name="x")


def test_validation(tmp_path):
    store = LLMProfileStore(tmp_path / "llm_profiles.json")
    with pytest.raises(ProfileError):
        store.create(name="", endpoint="https://a.io", api_key="k", model="m")
    with pytest.raises(ProfileError):
        store.create(name="A", endpoint="https://a.io", api_key="k", model="")
    with pytest.raises(ProfileError):
        store.create(name="A", endpoint="https://a.io", api_key="k" * 600, model="m")


def test_corrupt_file_reads_empty(tmp_path):
    path = tmp_path / "llm_profiles.json"
    path.write_text("{oops", encoding="utf-8")
    store = LLMProfileStore(path)
    assert store.active_profile() is None and store.public_view()["profiles"] == []

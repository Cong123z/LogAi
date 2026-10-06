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


# --- engine applies the active profile ------------------------------------------

from unittest.mock import MagicMock

import test_incident_classifier as tic


@pytest.fixture(autouse=True)
def _use_real_numpy(real_numpy):
    tic.np = real_numpy


def _status(p):
    p._last_grouping_heartbeat = 0.0
    p._heartbeat_grouping()
    return p.grouping_store.load_status()["runtime"]


def test_active_profile_applied_without_restart(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="")
    assert not p.incident_classifier.enabled and _status(p)["llm_enabled"] is False
    store = LLMProfileStore(tmp_path / "llm_profiles.json")
    pid = store.create(name="Zen", endpoint="https://zendigikey.shop", api_key="sk-abc-123456",
                       model="gpt-5.6-luna", activate=True)["id"]
    p._refresh_llm_profile()
    cfg = p.incident_classifier.config
    assert (cfg.endpoint, cfg.api_key, cfg.model) == (
        "https://zendigikey.shop/v1/chat/completions", "sk-abc-123456", "gpt-5.6-luna")
    assert p.incident_classifier.enabled
    runtime = _status(p)
    assert runtime["llm_enabled"] is True and runtime["llm_profile_id"] == pid
    assert "sk-abc-123456" not in json.dumps(p.grouping_store.load_status())


def test_active_profile_used_at_startup(tmp_path):
    LLMProfileStore(tmp_path / "llm_profiles.json").create(
        name="Zen", endpoint="https://z.io", api_key="k", model="m", activate=True)
    p = tic._pipeline(tmp_path, endpoint="")
    assert p.incident_classifier.enabled and p.incident_classifier.config.model == "m"


def test_switch_back_to_env_config(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="http://env-llm")
    store = LLMProfileStore(tmp_path / "llm_profiles.json")
    store.create(name="Zen", endpoint="https://z.io", api_key="k", model="m", activate=True)
    p._refresh_llm_profile()
    assert p.incident_classifier.config.endpoint == "https://z.io/v1/chat/completions"
    store.set_active(None)
    p._refresh_llm_profile()
    assert p.incident_classifier.config.endpoint == "http://env-llm"
    assert _status(p)["llm_profile_id"] is None


def test_disabled_llm_skips_alerts_and_requests(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="")
    p.incident_classifier.submit = MagicMock()
    p.incident_classifier.submit_service = MagicMock()
    p._track_alert_episodes([tic._result(tic.WK)], [tic._state(tic.WK, "ALERTING")])
    from logai.incident.requests import add_request
    add_request(tmp_path / "analysis_requests.json", "auth", 10.0, now=10.0)
    p._process_analysis_requests()
    p.incident_classifier.submit.assert_not_called()
    p.incident_classifier.submit_service.assert_not_called()


def test_broken_profile_file_keeps_env_config(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="http://env-llm")
    (tmp_path / "llm_profiles.json").write_text("{bad", encoding="utf-8")
    p._refresh_llm_profile()
    assert p.incident_classifier.config.endpoint == "http://env-llm"

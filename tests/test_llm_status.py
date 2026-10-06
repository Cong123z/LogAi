"""Why LLM analysis is (not) working: engine-reported status + reason."""
from __future__ import annotations

import json
import time

import pytest

import test_incident_classifier as tic
from logai.incident.profiles import LLMProfileStore


@pytest.fixture(autouse=True)
def _use_real_numpy(real_numpy):
    tic.np = real_numpy


def _runtime(p):
    p._last_grouping_heartbeat = 0.0
    p._heartbeat_grouping()
    return p.grouping_store.load_status()["runtime"]


def test_classifier_tracks_last_call_outcome(tmp_path):
    clf, _, _, _ = tic._classifier(tmp_path, [
        tic.FakeResponse(401, {}),
        tic._reply({"documentation_id": "DOC-001", "reasoning": "r", "suggestion": None})])
    clf.process(tic.KEY, tic.ALERT, [])
    assert "401" in clf.last_error and clf.last_error_at > 0
    clf.process(tic.KEY, tic.ALERT, [])
    assert clf.last_error is None and clf.last_ok_at >= clf.last_error_at


def test_reason_disabled_without_profile_or_default(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="")
    status, reason, _ = p._llm_status()
    assert status == "disabled" and "No active LLM profile" in reason
    runtime = _runtime(p)
    assert runtime["llm_status"] == "disabled" and runtime["llm_reason"] == reason


def test_reason_unreadable_profile_file(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="")
    (tmp_path / "llm_profiles.json").write_text("{bad", encoding="utf-8")
    p._refresh_llm_profile()
    status, reason, _ = p._llm_status()
    assert status == "disabled" and "unreadable" in reason


def test_reason_ok_names_profile(tmp_path):
    LLMProfileStore(tmp_path / "llm_profiles.json").create(
        name="Zen", endpoint="https://z.io", api_key="sk-secret-9999", model="gpt-5.6-luna", activate=True)
    p = tic._pipeline(tmp_path, endpoint="")
    status, reason, _ = p._llm_status()
    assert status == "ok" and "Zen" in reason and "gpt-5.6-luna" in reason
    assert "sk-secret-9999" not in json.dumps(_runtime(p))


def test_reason_elasticsearch_unreachable(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="http://env-llm")
    p._record_es_error(RuntimeError("Failed to resolve 'elasticsearch'"))
    status, reason, since = p._llm_status()
    assert status == "error" and "Elasticsearch" in reason and "resolve" in reason and since > 0
    first_since = since
    p._record_es_error(RuntimeError("still down"))
    assert p._llm_status()[2] == first_since  # "since" keeps the start of the outage
    p._record_es_ok()
    assert p._llm_status()[0] == "ok"


def test_reason_last_llm_call_failed(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="http://env-llm")
    p.incident_classifier.last_error = "LLM returned HTTP 401"
    p.incident_classifier.last_error_at = time.time()
    status, reason, _ = p._llm_status()
    assert status == "error" and "HTTP 401" in reason


def test_control_tick_applies_profile_and_heartbeat(tmp_path):
    p = tic._pipeline(tmp_path, endpoint="")
    LLMProfileStore(tmp_path / "llm_profiles.json").create(
        name="Zen", endpoint="https://z.io", api_key="k", model="m", activate=True)
    p._last_grouping_heartbeat = 0.0
    p._control_tick()
    runtime = p.grouping_store.load_status()["runtime"]
    assert runtime["llm_status"] == "ok" and runtime["llm_profile_id"]

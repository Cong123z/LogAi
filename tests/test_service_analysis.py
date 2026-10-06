"""On-demand LLM analysis of a whole service."""
from __future__ import annotations

from logai.config import StorageConfig
from logai.incident.requests import add_request, load_requests


def test_add_and_load_requests(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "recharge", 1000.0, now=1000.0)
    assert load_requests(p) == {"recharge": 1000.0}


def test_add_request_prunes_old(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "old", 1.0, now=1.0)
    add_request(p, "new", 90_000.0, now=90_000.0)
    assert load_requests(p) == {"new": 90_000.0}


def test_load_requests_corrupt(tmp_path):
    p = tmp_path / "analysis_requests.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_requests(p) == {}
    p.write_text('{"requests": {"a": "x", "b": 2}}', encoding="utf-8")
    assert load_requests(p) == {"b": 2.0}
    p.write_text('["not", "an", "object"]', encoding="utf-8")
    assert load_requests(p) == {}
    assert load_requests(tmp_path / "missing.json") == {}


def test_storage_defaults():
    assert StorageConfig().service_analysis_file == "service_analysis.json"
    assert StorageConfig().analysis_requests_file == "analysis_requests.json"

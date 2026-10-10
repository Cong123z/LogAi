from __future__ import annotations

import json
import time

from logai.features.template_activity import TemplateActivity, group_rate
from logai.web.app import create_app


def _entry(rate, baseline, rate_1m=0.0):
    return {"rate_1m": rate_1m, "rate_per_min": rate, "baseline_per_min": baseline, "ratio": None}


def test_group_rate_sums_members_and_counts_silent_ones_as_zero():
    assert group_rate([_entry(4.0, 1.0, 2.0), _entry(2.0, None, 1.5)]) == {
        "rate_1m": 3.5, "rate_per_min": 6.0, "baseline_per_min": 1.0, "ratio": 6.0}
    assert group_rate([_entry(0.5, None)])["ratio"] is None
    assert group_rate([]) == {"rate_1m": 0.0, "rate_per_min": 0.0, "baseline_per_min": None, "ratio": None}


def test_rate_1m_slides_the_previous_minute_out():
    activity = TemplateActivity()
    for _ in range(60):
        activity.record("api", "T1", 60.0 * 10 + 5)   # minute 10
    for _ in range(6):
        activity.record("api", "T1", 60.0 * 11 + 5)   # minute 11
    activity.record("web", "T1", 60.0 * 11 + 6)
    assert activity.rate_1m(60.0 * 11 + 15) == {"T1": 52.0}  # 7 + 60 * 0.75
    assert activity.rate_1m(60.0 * 12 + 1) == {"T1": 6.88}   # minute 10 expired
    assert activity.rate_1m(60.0 * 14) == {}


def _app(tmp_path, at):
    (tmp_path / "template_registry.json").write_text(json.dumps({
        "T1": {"template_text": "one", "service": "api", "group_id": "GA"},
        "T2": {"template_text": "two", "service": "api", "group_id": "GA"},
    }))
    (tmp_path / "group_registry.json").write_text(json.dumps({
        "GA": {"template_ids": ["T1", "T2"], "representative_template": "one"},
    }))
    (tmp_path / "template_rates.json").write_text(json.dumps({"at": at, "templates": {
        "T1": {"rate_1m": 2.5, "rate_per_min": 3.0, "baseline_per_min": 0.5, "ratio": 6.0}}}))
    return create_app(data_dir=str(tmp_path)).test_client()


def test_templates_and_groups_show_rates_and_hide_stale_ones(tmp_path):
    client = _app(tmp_path, time.time())
    rows = {t["template_id"]: t for t in client.get("/api/templates?sort=rate_1m").get_json()["items"]}
    assert (rows["T1"]["rate_1m"], rows["T1"]["ratio"]) == (2.5, 6.0)
    assert rows["T2"]["rate_per_min"] == 0.0
    group = client.get("/api/groups").get_json()["items"][0]
    assert (group["rate_1m"], group["rate_per_min"], group["ratio"]) == (2.5, 3.0, 6.0)
    assert [t["rate_per_min"] for t in group["templates"]] == [3.0, 0.0]

    client = _app(tmp_path, time.time() - 3600)
    assert client.get("/api/templates").get_json()["items"][0]["rate_per_min"] is None
    assert client.get("/api/groups").get_json()["items"][0]["rate_per_min"] is None


def test_rate_stream_pushes_the_snapshot_first(tmp_path):
    response = _app(tmp_path, time.time()).get("/api/rates/stream")
    assert response.mimetype == "text/event-stream"
    chunk = next(iter(response.response))
    response.close()
    payload = json.loads(chunk[len("data: "):])
    assert payload["templates"]["T1"]["rate_1m"] == 2.5
    assert payload["groups"]["GA"]["rate_1m"] == 2.5

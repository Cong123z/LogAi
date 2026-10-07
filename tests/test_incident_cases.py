"""Incident history store and signature matching."""
from __future__ import annotations

import pytest

from logai.incident.cases import add_case, delete_case, load_cases, similar_cases


def _case(**overrides):
    return {"service": "api", "group_ids": ["GA"], "template_texts": ["db timeout <*>", "pool exhausted"],
            "title": "DB pool exhausted", "root_cause": "pool too small", "resolution": "raise pool",
            **overrides}


def test_add_load_delete_and_id_sequence(tmp_path):
    path = tmp_path / "incident_cases.json"
    assert load_cases(path) == []
    first = add_case(path, _case(language="vi"))
    second = add_case(path, _case(title="second", language="xx"))
    assert (first["id"], second["id"]) == ("CASE-0001", "CASE-0002")
    assert first["language"] == "vi" and second["language"] == "en"
    assert {c["id"] for c in load_cases(path)} == {"CASE-0001", "CASE-0002"}
    assert delete_case(path, "CASE-0001") and not delete_case(path, "CASE-0001")
    # ids are never reused after a delete
    assert add_case(path, _case())["id"] == "CASE-0003"


def test_validation(tmp_path):
    path = tmp_path / "incident_cases.json"
    for bad in [_case(title=""), _case(root_cause=" "), _case(template_texts=[]),
                _case(title="x" * 201), _case(group_ids="GA")]:
        with pytest.raises(ValueError):
            add_case(path, bad)
    assert load_cases(path) == []
    case = add_case(path, _case(template_texts=["a", "a", *[f"t{i}" for i in range(40)]]))
    assert case["template_texts"][:2] == ["a", "t0"] and len(case["template_texts"]) == 30


def test_corrupt_file_reads_as_empty(tmp_path):
    path = tmp_path / "incident_cases.json"
    path.write_text("{nope")
    assert load_cases(path) == []
    assert add_case(path, _case())["id"] == "CASE-0001"


def test_similar_cases_overlap_threshold_service_and_order():
    cases = [
        {"id": "C1", "service": "api", "template_texts": ["a", "b"], "created_at": 1, "title": "half"},
        {"id": "C2", "service": "api", "template_texts": ["a"], "created_at": 2, "title": "full"},
        {"id": "C3", "service": "api", "template_texts": ["a", "x", "y"], "created_at": 3},
        {"id": "C4", "service": "web", "template_texts": ["a"], "created_at": 4},
    ]
    hits = similar_cases(cases, "api", ["a", "c"])
    assert [(h["id"], h["overlap"]) for h in hits] == [("C2", 1.0), ("C1", 0.5)]
    assert set(hits[0]) == {"id", "title", "root_cause", "resolution", "documentation_id",
                            "occurred_at", "overlap", "comparison", "only_now"}
    assert hits[1]["comparison"][1] == {"text": "b", "then": {"rate_per_min": None, "ratio": None,
                                                              "count_15m": None}, "now": None}
    assert hits[0]["only_now"] == ["c"]
    assert similar_cases(cases, "api", ["a"], k=1)[0]["id"] == "C2"


def test_pattern_numbers_stored_and_compared_then_vs_now(tmp_path):
    path = tmp_path / "incident_cases.json"
    pattern = [
        {"text": "db timeout <*>", "level": "ERROR", "count_15m": 4500, "count_30m": 5200,
         "rate_per_min": 300.0, "baseline_per_min": 7.5, "ratio": 40.0, "age_minutes": 20160,
         "reasons": ["elevated", "alerting"], "junk": "dropped"},
        {"text": "payment failed <*>", "rate_per_min": 12.0, "ratio": float("inf"), "reasons": ["new"]},
        {"text": "db timeout <*>", "ratio": 1.0},  # duplicate text ignored
        "not an entry",
    ]
    case = add_case(path, _case(template_texts=None, pattern=pattern,
                                totals={"events_15m": 31000, "error_events_15m": 4800}))
    assert case["template_texts"] == ["db timeout <*>", "payment failed <*>"]
    assert case["pattern"][0]["ratio"] == 40.0 and "junk" not in case["pattern"][0]
    assert case["pattern"][1]["ratio"] is None  # non-finite numbers are not stored
    assert case["totals"] == {"events_15m": 31000, "error_events_15m": 4800}

    now = [{"text": "db timeout <*>", "rate_per_min": 45.0, "ratio": 6.0, "count_15m": 675},
           {"text": "retry exhausted <*>", "rate_per_min": 3.0, "ratio": 30.0, "count_15m": 45}]
    hit = similar_cases(load_cases(path), "api", now)[0]
    assert hit["overlap"] == 0.5
    assert hit["comparison"] == [
        {"text": "db timeout <*>", "then": {"rate_per_min": 300.0, "ratio": 40.0, "count_15m": 4500},
         "now": {"rate_per_min": 45.0, "ratio": 6.0, "count_15m": 675}},
        {"text": "payment failed <*>", "then": {"rate_per_min": 12.0, "ratio": None, "count_15m": None},
         "now": None},
    ]
    assert hit["only_now"] == ["retry exhausted <*>"]


def test_update_case_rewrites_text_and_pattern_but_keeps_identity(tmp_path):
    from logai.incident.cases import update_case

    path = tmp_path / "incident_cases.json"
    case = add_case(path, _case(occurred_at=42.0, source={"kind": "service"},
                                totals={"events_15m": 10, "error_events_15m": 2}))
    updated = update_case(path, case["id"], {
        "title": "Gateway pool exhausted", "root_cause": "learned: pool size 20 too small",
        "resolution": "raise pool to 80", "pattern": [{"text": "db timeout <*>", "reasons": ["manual"]}],
    })
    assert updated["title"] == "Gateway pool exhausted" and updated["updated_at"]
    assert updated["template_texts"] == ["db timeout <*>"]
    for kept in ("id", "service", "created_at", "occurred_at", "source", "totals"):
        assert updated[kept] == case[kept]
    assert load_cases(path)[0]["root_cause"] == "learned: pool size 20 too small"
    with pytest.raises(ValueError):
        update_case(path, case["id"], {"title": "x", "root_cause": "y", "pattern": []})
    with pytest.raises(KeyError):
        update_case(path, "CASE-0404", {"title": "x", "root_cause": "y", "pattern": [{"text": "a"}]})

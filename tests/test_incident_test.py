"""Pure helpers of scripts/incident_test.py: target resolution and check-1 timing (no network)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "incident_test", Path(__file__).resolve().parent.parent / "scripts" / "incident_test.py")
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

ITEMS = [
    {"service": "recharge", "template_id": "recharge:T00001", "text": "enterd Receiving subscriber incorrect <*>"},
    {"service": "recharge", "template_id": "recharge:T00002", "text": "Input length must be multiple of <NUM>"},
    {"service": "recharge", "template_id": "recharge:T00003", "text": "Input length must be multiple of <NUM> when decrypting"},
]


def test_match_template_single():
    assert it.match_template(ITEMS, r"enterd Receiving subscriber") == "recharge:T00001"
    assert it.match_template(ITEMS, r"multiple of <NUM>$") == "recharge:T00002"


def test_match_template_fails_fast_on_none_or_several():
    with pytest.raises(ValueError, match="no template"):
        it.match_template(ITEMS, r"CHAM_DB_LEVEL")
    with pytest.raises(ValueError, match="2 templates"):
        it.match_template(ITEMS, r"Input length must be multiple of")


def test_check1_deadline_counts_from_first_closed_minute():
    # burst starts 12:00:20 -> first whole burst minute 12:01, third check is minute 12:03, closed 12:04 + delay
    start = 1_800_000_020.0 - 1_800_000_020.0 % 60 + 20
    first = start - 20 + 60
    assert it.check1_deadline(start) == first + 3 * 60 + it.CLOSE_DELAY_SECONDS
    # a burst starting exactly on a minute boundary counts that minute
    assert it.check1_deadline(first) == first + 3 * 60 + it.CLOSE_DELAY_SECONDS


def _sample(ts, state=None, tid="recharge:T00001", doc="DOC-001"):
    rows = [] if state is None else [{"service": "recharge", "kind": "document", "doc_id": doc, "state": state,
                                      "keys": [tid], "templates": [{"template_id": tid, "state": state}],
                                      "totals": []}]
    return {"ts": ts, "phase": "B1", "alerts": rows, "states": {}}


def test_first_template_state_and_check1_pass_fail():
    start = 1_800_000_000.0     # on a minute boundary
    samples = [_sample(start + 5), _sample(start + 120, "WARMING"), _sample(start + 200, "ALERTING")]
    hit = it.first_template_state(samples, "recharge", "recharge:T00001", "ALERTING", start)
    assert hit["ts"] == start + 200
    assert hit["ts"] <= it.check1_deadline(start)
    late = [_sample(start + 5), _sample(it.check1_deadline(start) + 5, "ALERTING")]
    assert it.first_template_state(late, "recharge", "recharge:T00001", "ALERTING", start)["ts"] > it.check1_deadline(start)
    assert it.first_template_state(samples[:2], "recharge", "recharge:T00001", "ALERTING", start) is None
    # samples before the burst started do not count
    early = [_sample(start - 5, "ALERTING")]
    assert it.first_template_state(early, "recharge", "recharge:T00001", "ALERTING", start) is None

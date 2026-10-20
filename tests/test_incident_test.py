"""Pure helpers and report of scripts/incident_test.py (no network)."""
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "incident_test", Path(__file__).resolve().parent.parent / "scripts" / "incident_test.py")
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

SVC, TID, T0 = "recharge", "recharge:T00001", 1_800_000_000.0   # T0 is on a minute boundary
ITEMS = [
    {"service": SVC, "template_id": "recharge:T00001", "text": "enterd Receiving subscriber incorrect <*>"},
    {"service": SVC, "template_id": "recharge:T00002", "text": "Input length must be multiple of <NUM>"},
    {"service": SVC, "template_id": "recharge:T00003", "text": "Input length must be multiple of <NUM> when decrypting"},
]


def doc_row(state, doc="DOC-001", tids=(TID,), doc_key_state=None):
    totals = [] if doc_key_state is None else [{"key": f"doc:{doc}", "state": doc_key_state}]
    return {"service": SVC, "kind": "document", "doc_id": doc, "state": state,
            "keys": list(tids) + [t["key"] for t in totals],
            "templates": [{"template_id": t, "state": state} for t in tids], "totals": totals}


def tpl_row(state, tid="recharge:T00009"):
    return {"service": SVC, "kind": "template", "doc_id": None, "state": state, "keys": [tid],
            "templates": [{"template_id": tid, "state": state}], "totals": []}


def sample(ts, *rows, phase="B1"):
    return {"ts": ts, "phase": phase, "alerts": list(rows), "states": {}}


def test_match_template_single():
    assert it.match_template(ITEMS, r"enterd Receiving subscriber") == "recharge:T00001"
    assert it.match_template(ITEMS, r"multiple of <NUM>$") == "recharge:T00002"


def test_match_template_fails_fast_on_none_or_several():
    with pytest.raises(ValueError, match="no template"):
        it.match_template(ITEMS, r"CHAM_DB_LEVEL")
    with pytest.raises(ValueError, match="2 templates"):
        it.match_template(ITEMS, r"Input length must be multiple of")


def test_check1_deadline_counts_from_first_closed_minute():
    # burst starts 20 s into a minute -> first whole burst minute is the next one
    assert it.check1_deadline(T0 + 20) == T0 + 60 + 3 * 60 + it.CLOSE_DELAY_SECONDS
    # a burst starting exactly on a minute boundary counts that minute
    assert it.check1_deadline(T0) == T0 + 3 * 60 + it.CLOSE_DELAY_SECONDS


def test_onset_inside_phase_and_check1_timing():
    state = lambda s: it.template_state(s, SVC, TID)
    samples = [sample(T0 - 5), sample(T0 + 5), sample(T0 + 120, doc_row("WARMING")), sample(T0 + 200, doc_row("ALERTING"))]
    status, hit = it.onset(samples, T0, T0 + 600, state)
    assert status == "onset" and hit["ts"] == T0 + 200 <= it.check1_deadline(T0)
    assert it.template_row(hit, SVC, TID)["doc_id"] == "DOC-001"
    late = [sample(T0 + 5), sample(it.check1_deadline(T0) + 5, doc_row("ALERTING"))]
    assert it.onset(late, T0, T0 + 600, state)[1]["ts"] > it.check1_deadline(T0)
    assert it.onset(samples[:3], T0, T0 + 600, state) == ("none", None)
    # an onset after the phase end does not count
    assert it.onset(samples, T0, T0 + 100, state) == ("none", None)


def test_onset_carried_over_is_not_counted():
    state = lambda s: it.template_state(s, SVC, TID)
    carried = [sample(T0 - 5, doc_row("ALERTING")), sample(T0 + 5, doc_row("ALERTING"))]
    assert it.onset(carried, T0, T0 + 600, state) == ("carried", None)
    assert it.verdict("carried", True) == "n/a: already alerting at start"
    # cooling before the start, then a fresh ALERTING inside: an onset
    fresh = [sample(T0 - 5, doc_row("COOLING")), sample(T0 + 60, doc_row("ALERTING"))]
    assert it.onset(fresh, T0, T0 + 600, state)[0] == "onset"
    assert (it.verdict("onset", True), it.verdict("none", False)) == ("PASS", "FAIL")


def test_doc_row_and_doc_key_state():
    s = sample(T0, doc_row("ALERTING", doc="DOC-003", doc_key_state="WARMING"))
    assert it.doc_row_state(s, SVC, "DOC-003") == "ALERTING"
    assert it.doc_key_state(s, SVC, "DOC-003") == "WARMING"
    assert it.doc_row_state(s, SVC, "DOC-001") == "NORMAL"
    assert it.doc_key_state(s, SVC, "DOC-001") == "NORMAL"            # key not in totals
    assert it.doc_key_state(s, "other", "DOC-003") == "NORMAL"
    assert it.template_state(s, SVC, "recharge:T00404") == "NORMAL"


def test_row_key():
    assert it.row_key(doc_row("ALERTING")) == "recharge doc DOC-001"
    assert it.row_key({"service": SVC, "kind": "service", "keys": ["*total"]}) == "recharge service"
    assert it.row_key(tpl_row("ALERTING")) == "recharge recharge:T00009"


def test_alert_onsets():
    a, b = tpl_row("ALERTING"), doc_row("ALERTING")
    # held ALERTING across samples is one onset
    assert it.alert_onsets([sample(1, a), sample(2, a), sample(3, a)]) == {"recharge recharge:T00009": 1}
    # carried over from before control: not counted
    assert it.alert_onsets([sample(1, a), sample(2, a)], prev={"recharge recharge:T00009"}) == {}
    # leaves ALERTING and comes back: two onsets; WARMING is not an onset
    assert it.alert_onsets([sample(1, a), sample(2, tpl_row("COOLING")), sample(3, a, b),
                            sample(4, doc_row("WARMING"))]) == {"recharge recharge:T00009": 2, "recharge doc DOC-001": 1}


def test_summarize_synthetic_run(tmp_path):
    targets = {"lixi": {"service": SVC, "template_id": TID, "text": "enterd x", "doc": "DOC-001"},
               "slowdb": {"service": SVC, "template_id": "recharge:T00002", "text": "CHAM", "doc": "DOC-003"},
               "sqltimeout": {"service": SVC, "template_id": "recharge:T00003", "text": "pGet", "doc": "DOC-003"}}
    (tmp_path / "meta.json").write_text(json.dumps({"quick": False, "targets": targets}))
    phases = [("control", "control", T0, []), ("B1", "burst", T0 + 600, ["lixi"]),
              ("B1-recovery", "recovery", T0 + 1200, ["lixi"]),
              ("B2", "burst", T0 + 1800, ["lixi"]), ("B3", "spread", T0 + 2400, ["slowdb", "sqltimeout"])]
    events = [{"event": "phase", "name": n, "kind": k, "start": s, "end": s + 600, "targets": t} for n, k, s, t in phases]
    events.append({"event": "inject", "label": "B3-slowdb", "status": 500})
    (tmp_path / "phases.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    noise, lixi = tpl_row("ALERTING"), doc_row("ALERTING")
    spread = doc_row("ALERTING", doc="DOC-003", tids=("recharge:T00002",), doc_key_state="ALERTING")
    spread["templates"][0]["state"] = "WARMING"   # template stays below ALERTING on its own
    rows = []
    for i in range(600):                          # 5 s samples over 50 min
        ts = T0 + i * 5
        name = next(n for n, _, s, _ in reversed(phases) if ts >= s)
        alerts = ([noise] if name == "control" and 100 < i < 110 else
                  [lixi] if (name == "B1" and ts > T0 + 800) or name in ("B1-recovery", "B2") else
                  [spread] if name == "B3" and ts > T0 + 2700 else [])
        rows.append({"ts": ts, "phase": name, "alerts": alerts,
                     "states": {f"{SVC}|DOC-001": "ALERTING"} if lixi in alerts else {}})
    (tmp_path / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    report = it.summarize(tmp_path)
    assert "| B1 | lixi | 205 s | 240 s | PASS | 205 s (yes) | PASS | DOC-001 | PASS |" in report
    # lixi never recovered, so B2 cannot be judged
    assert "| B2 | lixi | – | 240 s | n/a: already alerting at start | – (yes) | n/a: already alerting at start" in report
    assert "| B1-recovery | lixi | ALERTING | ALERTING |" in report
    assert "| B3 | DOC-003 | 305 s | PASS | none |" in report
    assert "1 ALERTING onsets over 0.17 h of control = **6.0/h**" in report
    assert "B3-slowdb: HTTP 500" in report

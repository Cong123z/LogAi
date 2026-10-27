import json
from datetime import datetime

import pytest
from psycopg.types.json import Jsonb

import scripts.lab.methods as methods
from scripts.lab import load_incidents, test_a
from scripts.lab import run as labrun
from scripts.lab.data import minute_at
from scripts.lab.store import put_result, start_run
from scripts.lab.methods import Spec
from test_lab_load import D1, D2, put
from test_lab_run import DAYS, Rec, seed
from tests.test_links import tmpl

T0 = 1792518662.4          # a phase start, epoch seconds


def make_dir(tmp_path, injects=None, targets=None):
    targets = targets or {"lixi": {"service": "s", "template_id": "s:OLD1", "text": "text A", "doc": "D"},
                          "slow": {"service": "s", "template_id": "s:OLD2", "text": "gone text", "doc": "D"}}
    (tmp_path / "meta.json").write_text(json.dumps({"started_at": T0, "targets": targets}))
    ev = [{"event": "phase", "name": "warmup", "kind": "warmup", "start": T0 - 600, "end": T0, "targets": []},
          {"event": "phase", "name": "B1", "kind": "burst", "start": T0, "end": T0 + 600, "targets": ["lixi"]},
          {"event": "inject", "ts": T0 + 1, "status": 200, "label": "B1-lixi", "pattern": "nomatch", "per_minute": 150},
          {"event": "phase", "name": "B1-recovery", "kind": "recovery", "start": T0 + 600, "end": T0 + 900, "targets": ["lixi"]},
          {"event": "phase", "name": "B2", "kind": "spread", "start": T0 + 900, "end": T0 + 1500, "targets": ["slow", "lixi"]}]
    ev += injects if injects is not None else [
        {"event": "inject", "ts": T0 + 901, "status": 200, "label": "B2-slow", "pattern": "gone", "per_minute": 10},
        {"event": "inject", "ts": T0 + 902, "status": 200, "label": "B2-lixi", "pattern": "x", "per_minute": 10}]
    (tmp_path / "phases.jsonl").write_text("\n".join(json.dumps(e) for e in ev))
    return tmp_path


def incidents(conn):
    return conn.execute("SELECT * FROM lab_incidents WHERE source = 'test-c' ORDER BY start_minute, key").fetchall()


def test_loader_exact_text_regex_ambiguous_and_nonok(conn, tmp_path):
    tmpl(conn, "A", "ERROR"), tmpl(conn, "G1", "ERROR"), tmpl(conn, "G2", "ERROR")
    conn.execute("UPDATE templates SET text = 'the gone one' WHERE template_id = 'G1'")
    conn.commit()
    d = make_dir(tmp_path, targets={"lixi": {"service": "s", "template_id": "s:OLD1", "text": "text A", "doc": "D"},
                                    "slow": {"service": "s", "template_id": "s:OLD2", "text": "no exact", "doc": "D"}})
    n, warns = load_incidents.load(conn, d, "public")
    rows = incidents(conn)
    assert n == len(rows) == 3
    by = {(r["meta"]["phase"], r["meta"]["label"]): r for r in rows}
    b1, b2l, b2s = by["B1", "lixi"], by["B2", "lixi"], by["B2", "slow"]
    assert b1["key"] == "A" and b2l["key"] == "A"                      # exact text beats the (non-matching) pattern
    assert b2s["key"] == "G1"                                         # no exact text -> the one template the regex matches
    assert (b1["type"], b2l["type"], b1["size"], b2l["size"]) == ("burst", "spread", 150, 10)
    assert b1["start_minute"] == int(T0) // 60 and b1["end_minute"] == int(T0 + 600) // 60
    assert b1["service"] == "s" and b1["data_schema"] == "public" and b1["meta"]["run_dir"] == d.name
    assert warns == []
    # ambiguous pattern -> NULL key and a warning
    conn.execute("UPDATE templates SET text = 'the gone two' WHERE template_id = 'G2'")
    conn.commit()
    n, warns = load_incidents.load(conn, d, "public")
    assert len(incidents(conn)) == 3                                  # reload replaced, not added
    assert {r["key"] for r in incidents(conn) if r["meta"]["label"] == "slow"} == {None}
    assert len(warns) == 1 and "slow" in warns[0]


def test_loader_skips_non_200_and_missing_inject(conn, tmp_path):
    tmpl(conn, "A", "ERROR")
    d = make_dir(tmp_path, injects=[{"event": "inject", "ts": T0, "status": 500, "label": "B2-slow", "pattern": "x",
                                     "per_minute": 10}])
    n, warns = load_incidents.load(conn, d, "public")
    assert n == 1 and incidents(conn)[0]["meta"]["label"] == "lixi" and incidents(conn)[0]["meta"]["phase"] == "B1"
    assert any("B2-slow" in w for w in warns) and any("B2-lixi" in w for w in warns)


def test_loader_reload_replaces_only_its_run_dir(conn, tmp_path):
    tmpl(conn, "A", "ERROR")
    (tmp_path / "x").mkdir(), (tmp_path / "y").mkdir()
    a, b = make_dir(tmp_path / "x"), make_dir(tmp_path / "y")
    load_incidents.load(conn, a, "public")
    load_incidents.load(conn, b, "public")
    load_incidents.load(conn, a, "public")
    assert len(incidents(conn)) == 6


# --test C -------------------------------------------------------------------------------------------------------
def args_c():
    return labrun.parse_args(["--site", "laptop", "--test", "C", "--methods", "rec", "--from", "2026-10-01",
                              "--to", "2026-10-14"])


def bump(conn, day, hour, lo, hi, n, key="busy"):
    row = conn.execute("SELECT counts FROM lab_minute_counts WHERE key = %s AND day = %s AND hour = %s",
                       (key, day, hour)).fetchone()["counts"]
    for i in range(lo, hi):
        row[i] = n
    conn.execute("UPDATE lab_minute_counts SET counts = %s WHERE key = %s AND day = %s AND hour = %s",
                 (row, key, day, hour))


def add_inc(conn, day, key="busy", per_minute=30):
    s = minute_at(day, 10 * 60 + 5)
    conn.execute("INSERT INTO lab_incidents (source, service, key, type, size, start_minute, end_minute) "
                 "VALUES ('test-c', 's', %s, 'burst', %s, %s, %s)", (key, per_minute, s, s + 10))


def run_c(monkeypatch, conn, cfg):
    Rec.fits, Rec.days = [], []
    monkeypatch.setattr(methods, "METHODS", {"rec": Rec})
    seed(conn)
    return conn


def test_c_recall_and_delay(monkeypatch, conn, cfg):
    run_c(monkeypatch, conn, cfg)
    bump(conn, DAYS[11], 10, 5, 15, 20)                    # flagged: count above 8
    add_inc(conn, DAYS[11])
    add_inc(conn, DAYS[12])                                # not flagged
    conn.commit()
    assert labrun.run(args_c(), cfg, conn, conn, finish=test_a.finish_c) == 0
    assert conn.execute("SELECT status FROM lab_runs").fetchone()["status"] == "ok"
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM lab_results WHERE scope = 'all'")}
    m = rows["M3"]["metrics"]
    assert m["recall"]["medium"] == 0.5 and m["n_injections"] == 2 and m["by_type"]["burst"]["medium"] == 0.5
    assert m["delay"]["median"] == 0 and rows["M3"]["knob"] == 1.0
    assert "injector_flag" not in m                         # no Test A run to compare with
    assert conn.execute("SELECT count(*) AS n FROM lab_incidents WHERE source = 'test-a'").fetchone()["n"] == 0


class RecDoc(Rec):
    spec = Spec("rec", "T9", "rec", frozenset({"M3", "M5"}), (1.0, 2.0), 1.0, False, False, 1)


def test_c_counts_incidents_in_the_tune_days_and_commits(monkeypatch, conn, cfg):
    run_c(monkeypatch, conn, cfg)
    bump(conn, DAYS[2], 10, 5, 15, 20)                     # tune part (first 7 days)
    add_inc(conn, DAYS[2])
    conn.commit()
    assert labrun.run(args_c(), cfg, conn, conn, finish=test_a.finish_c) == 0
    m = {r["kind"]: r for r in conn.execute("SELECT * FROM lab_results WHERE scope = 'all'")}["M3"]["metrics"]
    assert m["recall"]["medium"] == 1.0 and m["alert_rate"] == 0.0     # the incident is masked from the tune alert rate


def test_c_run_and_gap_are_committed(monkeypatch, conn, cfg):
    """Everything must be visible from a second connection (the runner closes its own without a final commit)."""
    from logai2.db import connect
    from tests.conftest import TEST_DSN
    run_c(monkeypatch, conn, cfg)
    bump(conn, DAYS[11], 10, 5, 15, 20)
    add_inc(conn, DAYS[11])
    a = mk_run(conn, "A")
    row(conn, a, "rec", "M3", 0.8, {"spike": {"medium": 0.1}})
    conn.execute("UPDATE lab_runs SET code_version = %s WHERE id = %s", (labrun.code_version(), a))
    conn.commit()
    assert labrun.run(args_c(), cfg, conn, conn, finish=test_a.finish_c) == 0
    conn.rollback()                                        # what closing without a commit does
    other = connect(TEST_DSN)
    try:
        other.execute(f"SET search_path TO {conn.execute('SHOW search_path').fetchone()['search_path']}")
        r = other.execute("SELECT status FROM lab_runs WHERE test = 'C'").fetchone()
        m = other.execute("SELECT metrics FROM lab_results WHERE scope = 'all' AND kind = 'M3' AND run_id = "
                          "(SELECT id FROM lab_runs WHERE test = 'C')").fetchone()["metrics"]
    finally:
        other.close()
    assert r["status"] == "ok" and m["injector_flag"] is True and m["a_recall"] == 0.1


def test_c_doc_key_hit_counts(monkeypatch, conn, cfg):
    """The incident is on `busy`, which stays quiet; its doc (busy + other) flags: a hit."""
    run_c(monkeypatch, conn, cfg)
    monkeypatch.setattr(methods, "METHODS", {"rec": RecDoc})
    from tests.test_links import doc, link
    tmpl(conn, "busy", "ERROR"), tmpl(conn, "other", "ERROR"), doc(conn, "D1")
    link(conn, "busy", "D1"), link(conn, "other", "D1")
    for d in DAYS:
        for h in range(24):
            put(conn, "other", d, h, [0] * 60)
    bump(conn, DAYS[11], 10, 5, 15, 20, key="other")
    add_inc(conn, DAYS[11])
    conn.commit()
    assert labrun.run(args_c(), cfg, conn, conn, finish=test_a.finish_c) == 0
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM lab_results WHERE scope = 'all'")}
    assert rows["M3"]["metrics"]["recall"]["medium"] == 1.0 and rows["M5"]["metrics"]["recall"]["medium"] == 1.0


def test_c_without_incidents_ends_ok_with_note(monkeypatch, conn, cfg):
    run_c(monkeypatch, conn, cfg)
    conn.commit()
    assert labrun.run(args_c(), cfg, conn, conn, finish=test_a.finish_c) == 0
    run = conn.execute("SELECT * FROM lab_runs").fetchone()
    assert run["status"] == "ok" and "no Test C incidents" in run["kinds"]["note"]
    assert conn.execute("SELECT count(*) AS n FROM lab_results").fetchone()["n"] == 0


# A vs C gap ----------------------------------------------------------------------------------------------------
def mk_run(conn, test, version="v1", status="ok"):
    rid = start_run(conn, site="laptop", test=test, code_version=version, image_tag=None, data_schema="public",
                    window_from=D1, window_to=D2, tune_to=D1, seed=1, params={})
    conn.execute("UPDATE lab_runs SET status = %s WHERE id = %s", (status, rid))
    return rid


def row(conn, rid, method, kind, recall, by_type=None):
    put_result(conn, rid, method, kind, "all", 1.0, "ok", {"recall": {"medium": recall}, "by_type": by_type or {}})


def flag(conn, rid, method, kind):
    return conn.execute("SELECT metrics FROM lab_results WHERE run_id = %s AND method = %s AND kind = %s AND scope = 'all'",
                        (rid, method, kind)).fetchone()["metrics"]


def test_gap_flag(conn):
    a_old, a = mk_run(conn, "A"), mk_run(conn, "A")
    a_other, a_failed = mk_run(conn, "A", version="v2"), mk_run(conn, "A", status="failed")
    a_site = mk_run(conn, "A")
    conn.execute("UPDATE lab_runs SET site = 'company' WHERE id = %s", (a_site,))
    row(conn, a_site, "m", "M3", 0.8, {"spike": {"medium": 0.0}})
    row(conn, a_old, "m", "M3", 0.1, {"spike": {"medium": 0.1}})
    row(conn, a, "m", "M3", 0.8, {"spike": {"medium": 0.9}, "ramp": {"medium": 0.0}})
    row(conn, a, "n", "M3", 0.8, {"spike": {"medium": 0.5}})
    row(conn, a, "o", "M3", 0.8, {"spike": {"medium": 0.5}})
    row(conn, a_other, "m", "M3", 0.8, {"spike": {"medium": 0.0}})
    row(conn, a_failed, "m", "M3", 0.8, {"spike": {"medium": 0.0}})
    c = mk_run(conn, "C")
    row(conn, c, "m", "M3", 0.5)         # |0.5 - 0.9| > 0.10 -> flagged, against the latest ok A run of the same version
    row(conn, c, "n", "M3", 0.55)        # within 0.10
    row(conn, c, "x", "M3", 0.55)        # no Test A row for the method
    conn.commit()
    test_a.flag_gap(conn, c)
    m = flag(conn, c, "m", "M3")
    assert m["injector_flag"] is True and m["a_recall"] == 0.9 and m["c_recall"] == 0.5
    assert flag(conn, c, "n", "M3")["injector_flag"] is False
    assert "injector_flag" not in flag(conn, c, "x", "M3")

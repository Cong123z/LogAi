from datetime import date, timedelta

import pytest

from logai2.web.app import create_app
from tests.conftest import TEST_DSN


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def test_document_crud_revision_and_delete_blocked_while_linked(client, conn):
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('DOC-DB-002', 'seeded', 'x', 'r0'), "
                 "('DOC-007', 'old', 'y', 'r1')")
    conn.commit()
    r = client.post("/api/documents", json={"title": "Pool exhausted", "error": "Pool exhausted for 10.0.0.1",
                                                "handling": "<b>restart</b>", "error_code": "E42"})
    assert r.status_code == 201
    doc = r.get_json()
    assert doc["doc_id"] == "DOC-008" and doc["revision"]
    assert client.post("/api/documents", json={"title": " ", "handling": "x"}).status_code == 400
    assert client.post("/api/documents", json={"title": "t"}).status_code == 400          # no ERROR, no Handling
    assert client.post("/api/documents", json={"title": "t", "error": "x\n" * 51}).status_code == 400
    created = {d["doc_id"]: d for d in client.get("/api/documents").get_json()}["DOC-008"]
    assert created["error_lines"] == ["Pool exhausted for <*>"] and created["embedded"] is False

    stale = client.put("/api/documents/DOC-008", json={"title": "t", "handling": "b", "error_code": "", "revision": "nope"})
    assert stale.status_code == 409 and stale.get_json() == {"error": "stale", "revision": doc["revision"]}
    ok = client.put("/api/documents/DOC-008", json={"title": "t2", "handling": "b2", "error_code": "",
                                                     "revision": doc["revision"]})
    assert ok.status_code == 200 and ok.get_json()["revision"] != doc["revision"]
    assert client.put("/api/documents/NOPE", json={"title": "t", "handling": "b", "revision": "r"}).status_code == 404

    conn.execute("INSERT INTO templates VALUES ('pay', 'pay:T00001', 'ERROR', 'boom', now(), now())")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES ('pay', 'pay:T00001', 'DOC-008')")
    conn.commit()
    listed = {d["doc_id"]: d for d in client.get("/api/documents").get_json()}
    assert listed["DOC-008"]["linked_templates"] == 1 and listed["DOC-008"]["handling"] == "b2"
    assert listed["DOC-DB-002"]["linked_templates"] == 0
    linked = client.get("/api/templates?doc_id=DOC-008").get_json()["items"]
    assert [t["template_id"] for t in linked] == ["pay:T00001"]

    blocked = client.delete("/api/documents/DOC-008")
    assert blocked.status_code == 409 and blocked.get_json() == {"error": "linked", "templates": 1}
    assert client.delete("/api/documents/DOC-007").status_code == 200
    assert client.delete("/api/documents/DOC-007").status_code == 404
    assert sorted(listed) == ["DOC-007", "DOC-008", "DOC-DB-002"]


def add_days(conn, service, days):
    for d in days:
        conn.execute("INSERT INTO day_status (service, day, committed, daytype) VALUES (%s, %s, true, %s)",
                     (service, d, "weekend" if d.weekday() >= 5 else "weekday"))


def test_decision_warns_under_3_days(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0)")
    add_days(conn, "pay", [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 18)])
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                 "VALUES ('pay', '2026-10-12', 9, 10, 'exclude'), ('pay', '2026-10-13', 0, 24, 'keep')")
    conn.commit()
    # 3 committed weekdays (10-18 is a Sunday); 10-12 already excludes hour 9, so after
    # excluding 8-10 on 10-13 hour 8 has 2 days and hour 9 has 1: both warn.
    r = client.post("/api/baseline/decisions",
                    json={"service": "pay", "day": "2026-10-13", "hour_from": 8, "hour_to": 10, "decision": "exclude"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["id"] and body["warning"] == [8, 9]
    keep = client.post("/api/baseline/decisions",
                       json={"service": "pay", "day": "2026-10-14", "hour_from": 20, "hour_to": 24, "decision": "keep"})
    assert keep.status_code == 201 and "warning" not in keep.get_json()

    for bad in ({"hour_from": 5, "hour_to": 5}, {"hour_from": -1}, {"hour_to": 25}, {"decision": "maybe"},
                {"day": "2026-13-01"}, {"hour_from": "3"}):
        payload = {"service": "pay", "day": "2026-10-14", "hour_from": 3, "hour_to": 4, "decision": "exclude", **bad}
        assert client.post("/api/baseline/decisions", json=payload).status_code == 400, bad
    unknown = {"service": "nope", "day": "2026-10-14", "hour_from": 3, "hour_to": 4, "decision": "exclude"}
    assert client.post("/api/baseline/decisions", json=unknown).status_code == 404

    page = client.get("/api/baseline?service=pay").get_json()
    assert [d["day"] for d in page["days"]] == ["2026-10-18", "2026-10-14", "2026-10-13", "2026-10-12"]
    # 4 rows minus the 10-13 keep that the later 10-13 exclude overlapped
    assert len(page["decisions"]) == 3 and page["last_commit"] is None
    assert page["full_after_days"] == {"weekday": 5, "weekend": 4}   # only 4 weekend days are kept
    assert page["commit_time"] == "02:00"
    assert client.delete(f"/api/baseline/decisions/{body['id']}").status_code == 200
    assert client.delete(f"/api/baseline/decisions/{body['id']}").status_code == 404
    assert client.get("/api/baseline?service=nope").status_code == 404


def test_warning_counts_only_the_days_the_rebuild_uses(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0)")
    # 7 committed weekdays; the rebuild uses the last 5: 10-14, 10-13, 10-12, 10-09, 10-08
    add_days(conn, "pay", [date(2026, 10, d) for d in (6, 7, 8, 9, 12, 13, 14)])
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                 "VALUES ('pay', '2026-10-14', 10, 11, 'exclude'), ('pay', '2026-10-13', 10, 11, 'exclude')")
    conn.commit()
    r = client.post("/api/baseline/decisions",
                    json={"service": "pay", "day": "2026-10-12", "hour_from": 10, "hour_to": 12, "decision": "exclude"})
    # hour 10: 5 picked - 3 excluded = 2 (all 7 days would leave 4); hour 11: 5 - 1 = 4
    assert r.get_json()["warning"] == [10]


def test_latest_decision_replaces_overlapping_opposite_ones(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0)")
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) VALUES "
                 "('pay', '2026-10-14', 9, 12, 'exclude'), ('pay', '2026-10-14', 13, 14, 'exclude'), "
                 "('pay', '2026-10-15', 11, 12, 'exclude'), ('pay', '2026-10-14', 10, 11, 'keep')")
    conn.commit()
    r = client.post("/api/baseline/decisions",
                    json={"service": "pay", "day": "2026-10-14", "hour_from": 11, "hour_to": 13, "decision": "keep"})
    assert r.status_code == 201
    rows = conn.execute("SELECT day::text, hour_from, hour_to, decision FROM review_exclusions "
                        "ORDER BY day, hour_from, decision").fetchall()
    assert [tuple(x.values()) for x in rows] == [
        ("2026-10-14", 10, 11, "keep"), ("2026-10-14", 11, 13, "keep"),
        ("2026-10-14", 13, 14, "exclude"), ("2026-10-15", 11, 12, "exclude")]


def test_baseline_suggestions_skip_decided_hours(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0)")
    conn.execute("INSERT INTO alert_changes (service, key, at, from_state, to_state, count) VALUES "
                 "('pay', '*total', '2026-10-14 14:20+07', 'NORMAL', 'WARMING', 1), "
                 "('pay', '*total', '2026-10-14 17:05+07', 'WARMING', 'NORMAL', 1)")
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                 "VALUES ('pay', '2026-10-14', 15, 17, 'keep')")
    conn.commit()
    sugg = client.get("/api/baseline?service=pay").get_json()["suggestions"]
    assert [(s["day"], s["hour_from"], s["hour_to"]) for s in sugg] == [("2026-10-14", 14, 15), ("2026-10-14", 17, 18)]


def test_recompute_enqueues(client, conn):
    r = client.post("/api/baseline/recompute")
    assert r.status_code == 202
    cid = r.get_json()["id"]
    row = conn.execute("SELECT kind FROM commands WHERE id = %s", (cid,)).fetchone()
    assert row["kind"] == "recompute_baseline"
    conn.commit()
    assert client.get(f"/api/commands/{cid}").get_json() == {"id": cid, "kind": "recompute_baseline",
                                                             "done": False, "result": None}
    conn.execute("UPDATE commands SET done_at = now(), result = '{\"baseline_rows\": 3}' WHERE id = %s", (cid,))
    conn.commit()
    assert client.get(f"/api/commands/{cid}").get_json()["result"] == {"baseline_rows": 3}
    assert client.get("/api/commands/999999").status_code == 404


def test_document_kind_error_or_safe(client, conn):
    r = client.post("/api/documents", json={"title": "Send OK", "error": "send ok", "verdict": "safe"})
    doc = r.get_json()
    assert r.status_code == 201
    listed = {d["doc_id"]: d for d in client.get("/api/documents").get_json()}
    assert listed[doc["doc_id"]]["verdict"] == "safe"
    # an update without a verdict keeps it; with one, changes it
    r = client.put(f"/api/documents/{doc['doc_id']}", json={"title": "Send OK", "error": "send ok", "revision": doc["revision"]})
    assert {d["doc_id"]: d for d in client.get("/api/documents").get_json()}[doc["doc_id"]]["verdict"] == "safe"
    r = client.put(f"/api/documents/{doc['doc_id']}", json={"title": "Send OK", "error": "send ok", "verdict": "action",
                                                          "revision": r.get_json()["revision"]})
    assert {d["doc_id"]: d for d in client.get("/api/documents").get_json()}[doc["doc_id"]]["verdict"] == "action"
    assert client.post("/api/documents", json={"title": "t", "error": "e", "verdict": "maybe"}).status_code == 400
    plain = client.post("/api/documents", json={"title": "t", "error": "e"}).get_json()
    assert {d["doc_id"]: d for d in client.get("/api/documents").get_json()}[plain["doc_id"]]["verdict"] == "action"


def test_document_service_shared_by_default_kept_on_update(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0), ('web', 1)")
    conn.commit()
    shared = client.post("/api/documents", json={"title": "t", "error": "e"}).get_json()
    owned = client.post("/api/documents", json={"title": "t", "error": "e", "service": "pay"}).get_json()
    listed = lambda: {d["doc_id"]: d for d in client.get("/api/documents").get_json()}
    assert listed()[shared["doc_id"]]["service"] is None and listed()[owned["doc_id"]]["service"] == "pay"
    r = client.put(f"/api/documents/{owned['doc_id']}", json={"title": "t2", "error": "e", "revision": owned["revision"]})
    assert listed()[owned["doc_id"]]["service"] == "pay"                    # left out: kept
    client.put(f"/api/documents/{owned['doc_id']}", json={"title": "t2", "error": "e", "service": "",
                                                          "revision": r.get_json()["revision"]})
    assert listed()[owned["doc_id"]]["service"] is None                     # "" = shared
    assert client.post("/api/documents", json={"title": "t", "error": "e", "service": "nope"}).status_code == 400
    assert listed()[shared["doc_id"]]["based_on"] == []


def test_document_level_any_by_default_validated_and_kept(client):
    d = client.post("/api/documents", json={"title": "t", "error": "e", "level": "ERROR"}).get_json()
    listed = lambda: {x["doc_id"]: x for x in client.get("/api/documents").get_json()}
    assert listed()[d["doc_id"]]["level"] == "ERROR"
    client.put(f"/api/documents/{d['doc_id']}", json={"title": "t2", "error": "e", "revision": d["revision"]})
    assert listed()[d["doc_id"]]["level"] == "ERROR"
    assert client.post("/api/documents", json={"title": "t", "error": "e", "level": "DEBUG"}).status_code == 400
    plain = client.post("/api/documents", json={"title": "t", "error": "e"}).get_json()
    assert listed()[plain["doc_id"]]["level"] is None


def add_tpl(conn, tid, level):
    conn.execute("INSERT INTO templates VALUES ('pay', %s, %s, 'boom', now(), now())", (tid, level))


def test_baseline_groups_exact_over_500(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0)")
    for n in range(520):
        add_tpl(conn, f"pay:E{n}", "ERROR")
    add_tpl(conn, "pay:W1", "WARNING")
    conn.commit()
    g = client.get("/api/baseline/groups?service=pay").get_json()
    assert g["all"] == 521 and g["ERROR"] == 520 and g["WARN"] == 1 and g["INFO"] == 0
    assert client.get("/api/baseline/groups").status_code == 400


def test_baseline_groups_verdict_counts_linked_active_docs(client, conn):
    conn.execute("INSERT INTO services VALUES ('pay', 0)")
    add_tpl(conn, "pay:T1", "ERROR")
    add_tpl(conn, "pay:T2", "ERROR")
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision, verdict, status) VALUES "
                 "('D-SAFE', 's', 'x', 'r', 'safe', 'active'), ('D-ACT', 'a', 'x', 'r', 'action', 'active')")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES "
                 "('pay', 'pay:T1', 'D-SAFE', 'manual'), ('pay', 'pay:T2', 'D-ACT', 'draft')")
    conn.commit()
    g = client.get("/api/baseline/groups?service=pay").get_json()
    assert g["safe"] == 1 and g["action"] == 0

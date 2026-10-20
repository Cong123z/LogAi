from datetime import date, timedelta

import pytest

from logai2.web.app import create_app
from tests.conftest import TEST_DSN


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}"
    return create_app(cfg).test_client()


def test_document_crud_revision_and_delete_blocked_while_linked(client, conn):
    conn.execute("INSERT INTO documents (doc_id, title, body, revision) VALUES ('DOC-DB-002', 'seeded', 'x', 'r0'), "
                 "('DOC-007', 'old', 'y', 'r1')")
    conn.commit()
    r = client.post("/api/documents", json={"title": "Pool exhausted", "body": "<b>restart</b>", "error_code": "E42"})
    assert r.status_code == 201
    doc = r.get_json()
    assert doc["doc_id"] == "DOC-008" and doc["revision"]
    assert client.post("/api/documents", json={"title": " ", "body": "x"}).status_code == 400

    stale = client.put("/api/documents/DOC-008", json={"title": "t", "body": "b", "error_code": "", "revision": "nope"})
    assert stale.status_code == 409 and stale.get_json() == {"error": "stale", "revision": doc["revision"]}
    ok = client.put("/api/documents/DOC-008", json={"title": "t2", "body": "b2", "error_code": "",
                                                     "revision": doc["revision"]})
    assert ok.status_code == 200 and ok.get_json()["revision"] != doc["revision"]
    assert client.put("/api/documents/NOPE", json={"title": "t", "body": "b", "revision": "r"}).status_code == 404

    conn.execute("INSERT INTO templates VALUES ('pay', 'pay:T00001', 'ERROR', 'boom', now(), now())")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES ('pay', 'pay:T00001', 'DOC-008')")
    conn.commit()
    listed = {d["doc_id"]: d for d in client.get("/api/documents").get_json()}
    assert listed["DOC-008"]["linked_templates"] == 1 and listed["DOC-008"]["body"] == "b2"
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
    assert len(page["decisions"]) == 4 and page["last_commit"] is None
    assert client.delete(f"/api/baseline/decisions/{body['id']}").status_code == 200
    assert client.delete(f"/api/baseline/decisions/{body['id']}").status_code == 404
    assert client.get("/api/baseline?service=nope").status_code == 404


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

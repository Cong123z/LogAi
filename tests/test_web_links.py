"""Layered links through the web API: confirm / reject, apply to similar, clusters, badges."""
import pytest

from logai2.links import apply_links
from logai2.web.app import create_app
from tests.conftest import TEST_DSN
from tests.test_links import doc, tmpl, vec


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def url(tid, suffix=""):
    return f"/api/templates/s/{tid}/link{suffix}"


def source(conn, tid):
    row = conn.execute("SELECT doc_id, source FROM template_doc_links WHERE template_id = %s", (tid,)).fetchone()
    conn.commit()
    return (row["source"], row["doc_id"]) if row else None


def test_confirm_and_reject_each_automatic_source(client, conn, cfg):
    doc(conn, "D1"), doc(conn, "DR1", status="draft"), doc(conn, "DR2", status="draft")
    for tid, d, src in [("A", "D1", "inherited"), ("B", "D1", "corpus"), ("C", "D1", "llm"), ("E", "DR1", "draft"),
                        ("F", "D1", "inherited"), ("G", "D1", "llm"), ("H", "DR2", "draft"), ("M", "D1", "manual")]:
        tmpl(conn, tid)
        conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('s', %s, %s, %s)",
                     (tid, d, src))
    conn.commit()
    for tid in "ABCE":
        assert client.post(url(tid, "/confirm")).status_code == 200
        assert source(conn, tid)[0] == "manual"
    assert conn.execute("SELECT status FROM documents WHERE doc_id = 'DR1'").fetchone()["status"] == "active"
    assert client.post(url("F", "/reject")).get_json() == {"source": "manual"}
    assert source(conn, "F") == ("manual", None)
    assert client.post(url("G", "/reject")).get_json() == {"source": "rejected"}
    assert client.post(url("H", "/reject")).get_json() == {"source": "rejected"}
    assert source(conn, "H") == ("rejected", "DR2")
    assert conn.execute("SELECT status FROM documents WHERE doc_id = 'DR2'").fetchone()["status"] == "rejected"
    assert client.post(url("M", "/confirm")).status_code == 409          # nothing automatic to confirm
    assert client.post(url("NOPE", "/reject")).status_code == 404
    # the sticky rows survive the linker
    with conn.transaction():
        apply_links(conn, cfg.documentation)
    assert source(conn, "F") == ("manual", None) and source(conn, "G") == ("rejected", "D1")
    assert conn.execute("SELECT count(*) AS n FROM commands").fetchone()["n"] == 7


def test_apply_to_similar_lists_pre_ticks_and_links_in_one_request(client, conn):
    doc(conn, "D1")
    tmpl(conn, "A", v=vec((0, 1.0)))
    tmpl(conn, "B", v=vec((0, 0.9), (5, 0.43589)))       # 0.90 >= ERROR inherit 0.85: pre-ticked
    tmpl(conn, "C", v=vec((0, 0.82), (6, 0.57236)))      # 0.82: listed, not ticked
    tmpl(conn, "D", v=vec((0, 0.5), (7, 0.86603)))       # under suggest 0.80
    tmpl(conn, "I", "INFO", v=vec((0, 1.0)))             # other class
    tmpl(conn, "O", v=vec((0, 1.0)), service="other")
    conn.commit()
    listed = client.get("/api/templates/s/A/similar").get_json()
    assert listed["embedded"] and [(i["template_id"], i["pre_ticked"]) for i in listed["items"]] == [
        ("B", True), ("C", False)]
    r = client.put(url("A"), json={"doc_id": "D1", "also": ["B", "C"]})
    assert r.get_json() == {"doc_id": "D1", "linked": 3}
    assert [source(conn, t) for t in "ABC"] == [("manual", "D1")] * 3
    assert client.put(url("A"), json={"doc_id": "D1", "also": ["O"]}).status_code == 404   # other service
    assert client.put(url("A"), json={"doc_id": "D1", "also": "B"}).status_code == 400
    assert client.get("/api/templates/s/NOPE/similar").status_code == 404


def test_alert_clusters_badges_and_templates_filter(client, conn):
    doc(conn, "D1"), doc(conn, "DR", status="draft")
    conn.execute("UPDATE documents SET verdict = 'safe' WHERE doc_id = 'DR'")
    for tid, v in [("A", vec((0, 1.0))), ("B", vec((0, 0.9), (5, 0.43589))), ("C", vec((9, 1.0))), ("L", vec((7, 1.0)))]:
        tmpl(conn, tid, v=v)
        conn.execute("INSERT INTO alert_state VALUES ('s', %s, 'ALERTING', now(), 3, 2.0, 9, 0, 3)", (tid,))
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source, similarity) "
                 "VALUES ('s', 'L', 'D1', 'corpus', 0.91), ('s', 'C', 'DR', 'draft', NULL)")
    conn.commit()
    rows = client.get("/api/alerts").get_json()
    kinds = {r["kind"]: r for r in rows}
    assert sorted(kinds) == ["cluster", "document", "template"]
    assert sorted(kinds["cluster"]["keys"]) == ["A", "B"]
    line = kinds["document"]["templates"][0]
    assert line["link"]["source"] == "corpus" and line["link"]["similarity"] == pytest.approx(0.91)
    assert kinds["template"]["templates"][0]["link"]["verdict"] == "safe"   # a draft shows, drives nothing

    items = client.get("/api/templates?link_source=corpus").get_json()["items"]
    assert [(i["template_id"], i["link_source"], i["embedded"]) for i in items] == [("L", "corpus", True)]
    assert {i["template_id"] for i in client.get("/api/templates?link_source=none").get_json()["items"]} == {"A", "B"}
    assert client.get("/api/templates?link_source=bogus").status_code == 400


def test_pipeline_shows_the_linker(client, conn):
    assert client.get("/api/pipeline").get_json()["linker_stale"] is True
    conn.execute("INSERT INTO worker_status VALUES ('linker', %s, now())",
                 ('{"heartbeat": 9999999999, "pending": {"templates": 2, "documents": 0}}',))
    conn.commit()
    body = client.get("/api/pipeline").get_json()
    assert body["linker_stale"] is False and body["linker"]["pending"]["templates"] == 2


def test_delete_on_a_draft_link_rejects_its_document(client, conn):
    doc(conn, "DR", status="draft")
    tmpl(conn, "A")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('s', 'A', 'DR', 'draft')")
    conn.commit()
    assert client.delete(url("A")).status_code == 200 and source(conn, "A") is None
    assert conn.execute("SELECT status FROM documents WHERE doc_id = 'DR'").fetchone()["status"] == "rejected"


def test_a_manual_link_over_a_draft_rejects_the_draft(client, conn):
    doc(conn, "D1"), doc(conn, "DR", status="draft")
    tmpl(conn, "A")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('s', 'A', 'DR', 'draft')")
    conn.commit()
    assert client.put(url("A"), json={"doc_id": "D1"}).status_code == 200
    assert conn.execute("SELECT status FROM documents WHERE doc_id = 'DR'").fetchone()["status"] == "rejected"

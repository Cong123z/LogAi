import pytest

from logai2.embed import embed
from logai2.linker import Linker
from tests.fake_http import FakeHttp, axis, embeddings_handler


@pytest.fixture
def server():
    s = FakeHttp(embeddings_handler({"timeout <*>": axis(0), "Timeout <*>": axis(0), "pool <*>": axis(5)}))
    yield s
    s.close()


def q(conn, sql):
    rows = conn.execute(sql).fetchall()
    conn.commit()
    return rows


def test_cycle_embeds_links_and_asks_the_engine_to_reload(conn, cfg, server):
    cfg.embedding.url = server.url
    conn.execute("INSERT INTO templates VALUES ('s', 's:T1', 'ERROR', 'timeout <*>', now(), now())")
    conn.execute("INSERT INTO documents (doc_id, title, error, handling, revision) "
                 "VALUES ('D1', 't', 'Timeout 42', 'restart', 'r1'), ('D2', 't', '', 'incident runbook', 'r1')")
    conn.commit()
    status = Linker(cfg, conn).cycle()
    assert (status["embedded_templates"], status["embedded_documents"], status["links_changed"]) == (1, 2, True)
    assert q(conn, "SELECT doc_id, n, text FROM document_errors") == [{"doc_id": "D1", "n": 0, "text": "Timeout <*>"}]
    assert q(conn, "SELECT template_id, doc_id, source FROM template_doc_links") == [
        {"template_id": "s:T1", "doc_id": "D1", "source": "corpus"}]
    assert [r["kind"] for r in q(conn, "SELECT kind FROM commands")] == ["reload_links"]
    worker = q(conn, "SELECT status FROM worker_status WHERE worker = 'linker'")[0]["status"]
    assert worker["pending"] == {"templates": 0, "documents": 0} and worker["last_error"] is None

    calls = len(server.calls)
    conn.execute("UPDATE documents SET handling = 'restart the pool', revision = 'r2' WHERE doc_id = 'D1'")
    conn.commit()
    status = Linker(cfg, conn).cycle()
    assert status["embedded_documents"] == 1 and len(server.calls) == calls + 1   # ERROR re-embedded once
    assert not status["links_changed"] and len(q(conn, "SELECT 1 FROM commands")) == 1


def test_a_text_changed_meanwhile_is_not_overwritten(conn, cfg, server):
    cfg.embedding.url = server.url

    def embed_then_change(url, model, texts, *args):
        conn.execute("UPDATE templates SET text = 'pool <*>' WHERE template_id = 's:T1'")   # the engine's write
        return embed(url, model, texts)

    conn.execute("INSERT INTO templates VALUES ('s', 's:T1', 'ERROR', 'timeout <*>', now(), now())")
    conn.commit()
    Linker(cfg, conn, embed_fn=embed_then_change).cycle()
    assert q(conn, "SELECT embedding IS NULL AS e FROM templates")[0]["e"]


def test_service_down_is_reported_and_links_still_run(conn, cfg):
    down = FakeHttp(lambda p, b: (503, {"error": "loading"}))
    cfg.embedding.url, cfg.embedding.retry_backoff_seconds = down.url, 0
    conn.execute("INSERT INTO templates VALUES ('s', 's:T1', 'ERROR', 'timeout <*>', now(), now())")
    conn.commit()
    status = Linker(cfg, conn).cycle()
    down.close()
    assert status["last_error"].startswith("embedding: ") and status["pending"]["templates"] == 1
    cfg.embedding.url = ""
    assert Linker(cfg, conn).cycle()["warnings"] == ["embedding.url is empty: nothing is embedded"]


def test_linker_skips_off_templates(conn, cfg, server):
    cfg.embedding.url = server.url
    conn.execute("INSERT INTO services VALUES ('sa', 0), ('sb', 1)")
    conn.execute("UPDATE services SET monitored = false WHERE service = 'sb'")
    conn.execute("INSERT INTO templates VALUES ('sa', 'sa:T1', 'ERROR', 'timeout <*>', now(), now()), "
                 "('sb', 'sb:T1', 'ERROR', 'timeout <*>', now(), now())")
    conn.commit()
    status = Linker(cfg, conn).cycle()
    assert status["embedded_templates"] == 1 and status["pending"]["templates"] == 0
    assert q(conn, "SELECT service FROM templates WHERE embedding IS NOT NULL") == [{"service": "sa"}]


def test_resolver_skips_off_templates(conn, cfg):
    from logai2.resolver import Resolver
    from tests.test_links import tmpl, vec
    conn.execute("INSERT INTO llm_profiles (name, base_url, model, api_key, active, revision) "
                 "VALUES ('p', 'http://x', 'm', 'k', true, 'r')")
    conn.execute("INSERT INTO services VALUES ('sa', 0), ('sb', 1)")
    conn.execute("UPDATE services SET monitored = false WHERE service = 'sb'")
    tmpl(conn, "A", v=vec((0, 1.0)), service="sa")
    tmpl(conn, "B", v=vec((1, 1.0)), service="sb")
    conn.commit()
    seen = []

    def chat(profile, system, user):
        seen.append(user)
        return {"title": "t", "handling": "h"}

    Resolver(cfg, conn, chat=chat).run({"warnings": [], "last_error": None})
    conn.commit()
    assert seen and all('"service": "sb"' not in s for s in seen)

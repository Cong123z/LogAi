"""An Off service (services.monitored = false) is hidden from every page, API and picker; its data stays."""
import pytest

from logai2.db import ensure_partitions
from logai2.web.app import create_app
from tests.conftest import TEST_DSN
from tests.test_module_alerts import chg, mins, seed_module, t, tmpl as mtmpl
from tests.test_web_module_alerts import get as module_alerts_get


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    for svc, mod, monitored in (("sa", "MA", True), ("sb", "MB", True)):
        seed_module(conn, mod, svc)
        mtmpl(conn, svc, f"{svc}:T1", text=f"{svc} text", level="ERROR" if svc == "sa" else "OFFLEVEL")
        conn.execute("INSERT INTO template_modules VALUES (%s, %s, %s, now(), now())", (svc, f"{svc}:T1", mod))
        conn.execute("INSERT INTO alert_state VALUES (%s, %s, 'ALERTING', now(), 3, 2.0, 9, 0, 3)", (svc, f"{svc}:T1"))
        conn.execute("INSERT INTO hourly_counts VALUES (%s, %s, '2026-10-14', 8, 10, 100)", (svc, f"{svc}:T1"))
        conn.execute("INSERT INTO service_hours VALUES (%s, '2026-10-14', 8, 60)", (svc,))
        chg(conn, svc, f"{svc}:T1", t(8, 2), "NORMAL", "WARMING")
        chg(conn, svc, f"{svc}:T1", t(8, 3), "WARMING", "ALERTING")
        mins(conn, svc, f"{svc}:T1", mod, t(8, 0), 9, 10)
        conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES (%s, 'doc', 'h', 'r')", (f"D-{svc.upper()}",))
        conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES (%s, %s, %s)",
                     (svc, f"{svc}:T1", f"D-{svc.upper()}"))
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('D-shared', 'shared', 'h', 'r')")
    for svc in ("sa", "sb"):   # one shared document linked to both services
        conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES (%s, %s, 'D-shared', 'manual')",
                     (svc, f"{svc}:T3"))
    conn.execute("UPDATE services SET monitored = false WHERE service = 'sb'")
    conn.commit()
    return create_app(cfg).test_client()


LISTS = ["/api/services", "/api/alerts", "/api/templates", "/api/templates?service=sb", "/api/pipeline",
         "/api/retrain", "/api/documents", "/api/documents?status=draft", "/api/template-levels",
         "/api/template-levels?service=sb"]


@pytest.mark.parametrize("url", LISTS)
def test_off_service_absent_from_lists(client, url):
    text = client.get(url).get_data(as_text=True)
    assert "sb" not in text and "OFFLEVEL" not in text


def test_monitored_service_still_listed(client):
    assert client.get("/api/services").get_json() == ["sa"]
    assert client.get("/api/templates").get_json()["total"] == 1
    assert [a["service"] for a in client.get("/api/alerts").get_json()] == ["sa"]
    assert [r["service"] for r in client.get("/api/pipeline").get_json()["services"]] == ["sa"]
    docs = {d["doc_id"]: d for d in client.get("/api/documents").get_json()}
    assert docs["D-SA"]["linked_templates"] == 1 and docs["D-shared"]["linked_templates"] == 1


@pytest.mark.parametrize("url", ["/api/baseline?service=sb", "/api/baseline/groups?service=sb",
                                 "/api/modules?service=sb", "/api/baseline/matrix?service=sb",
                                 "/api/baseline/inspect?service=sb", "/api/baseline/series?service=sb",
                                 "/api/baseline/incident?service=sb", "/api/templates/sb/sb:T1/similar"])
def test_off_service_routes_404(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("method,suffix", [("put", "/link"), ("delete", "/link"),
                                           ("post", "/link/confirm"), ("post", "/link/reject")])
def test_off_service_link_routes_404(client, method, suffix):
    r = getattr(client, method)(f"/api/templates/sb/sb:T1{suffix}", json={"doc_id": None})
    assert r.status_code == 404


def test_monitored_service_routes_work(client):
    assert client.get("/api/baseline?service=sa").status_code == 200
    assert client.get("/api/baseline/groups?service=sa").get_json()["all"] == 1
    assert client.get("/api/modules?service=sa").get_json()[0]["module"] == "MA"
    assert client.get("/api/templates/sa/sa:T1/similar").status_code == 200


def test_template_levels_and_module_alerts_skip_off(client):
    assert [r["level"] for r in client.get("/api/template-levels").get_json()] == ["ERROR"]
    assert client.get("/api/template-levels?service=sb").get_json() == []
    assert len(module_alerts_get(client, "MA").get_json()["templates"]) == 1
    assert module_alerts_get(client, "MB").get_json()["templates"] == []


def test_documents_service_field_rejects_off_service(client):
    body = {"title": "t", "handling": "h", "service": "sb"}
    assert client.post("/api/documents", json=body).status_code == 400
    assert client.post("/api/documents", json={**body, "service": "sa"}).status_code == 201

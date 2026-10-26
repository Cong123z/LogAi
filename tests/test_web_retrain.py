import time
from datetime import datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from logai2.timeutil import TZ
from logai2.web.app import create_app
from tests.conftest import TEST_DSN


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def today():
    return datetime.now(TZ).date()


def reread(**kw):
    d = {"kind": "reread", "services": None, "days": [today().isoformat()], "hour_from": 0, "hour_to": 24}
    d.update(kw)
    return d


def add_run(conn, kind="reread", state="done", log=None, started=None):
    return conn.execute(
        "INSERT INTO retrain_runs (kind, trigger, scope, state, started_at, log) "
        "VALUES (%s, 'manual', '{}', %s, to_timestamp(%s), %s) RETURNING id",
        (kind, state, started or time.time(), Jsonb(log or [{"m": "x"}]))).fetchone()["id"]


def commands(conn):
    return conn.execute("SELECT kind, payload FROM commands ORDER BY id").fetchall()


def test_get_retrain_defaults(client, conn, cfg):
    conn.execute("INSERT INTO services VALUES ('b', 0), ('a', 1)")
    conn.commit()
    b = client.get("/api/retrain").get_json()
    assert b["schedule"] == {"enabled": True, "time": cfg.baseline.commit_time, "weekdays": list(range(7))}
    assert b["revision"] is None and b["saved"] is False and b["next_run_at"] is None
    assert b["timezone"] == "Asia/Ho_Chi_Minh" and b["current"] is None and b["runs"] == []
    assert b["es_oldest"] == {} and b["services"] == ["a", "b"] and b["llm_active"] is False
    assert b["engine_stale"] is True and b["default_rate"] == cfg.retrain.default_rate


def test_get_retrain_current_and_runs_order_without_log(client, conn):
    old = add_run(conn, started=time.time() - 100)
    cur = add_run(conn, state="running", started=time.time() - 50)
    new = add_run(conn, kind="rebuild", started=time.time())
    conn.execute("INSERT INTO worker_status (worker, status) VALUES ('engine', %s)",
                 (Jsonb({"next_commit_at": 123.0, "es_oldest": {"p": 5}, "heartbeat": time.time()}),))
    conn.commit()
    b = client.get("/api/retrain").get_json()
    assert [r["id"] for r in b["runs"]] == [new, cur, old]
    assert all("log" not in r for r in b["runs"])
    assert b["current"]["id"] == cur and b["current"]["state"] == "running"
    assert isinstance(b["runs"][0]["started_at"], float) and b["runs"][0]["finished_at"] is None
    assert set(b["runs"][0]) == {"id", "kind", "trigger", "scope", "state", "started_at", "finished_at", "steps",
                                 "result", "error", "error_type", "failed_step"}
    assert b["next_run_at"] == 123.0 and b["es_oldest"] == {"p": 5} and b["engine_stale"] is False


def test_run_detail_has_log(client, conn):
    rid = add_run(conn, log=[{"m": "hello"}])
    conn.commit()
    r = client.get(f"/api/retrain/runs/{rid}").get_json()
    assert r["id"] == rid and r["log"] == [{"m": "hello"}]
    assert client.get("/api/retrain/runs/9999").status_code == 404


def test_schedule_save_and_409(client, conn):
    s = {"enabled": False, "time": "03:30", "weekdays": [5, 1, 3]}
    r = client.put("/api/retrain/schedule", json={"schedule": s, "revision": None})
    assert r.status_code == 200
    b = r.get_json()
    assert b["schedule"] == {"enabled": False, "time": "03:30", "weekdays": [1, 3, 5]} and b["saved"] is True
    assert commands(conn) == []
    assert client.get("/api/retrain").get_json()["revision"] == b["revision"]
    stale = client.put("/api/retrain/schedule", json={"schedule": s, "revision": None})
    assert stale.status_code == 409 and stale.get_json()["revision"] == b["revision"]
    assert client.put("/api/retrain/schedule", json={"schedule": s, "revision": b["revision"]}).status_code == 200


@pytest.mark.parametrize("patch", [
    {"time": "24:00"}, {"time": "7:5"}, {"time": "12:60"}, {"time": 5}, {"weekdays": []}, {"weekdays": [1, 1]},
    {"weekdays": [7]}, {"weekdays": [-1]}, {"weekdays": [True]}, {"weekdays": "1"}, {"enabled": "yes"},
    {"enabled": 1}])
def test_schedule_validation_400(client, patch):
    s = {"enabled": True, "time": "02:00", "weekdays": [0, 1]} | patch
    r = client.put("/api/retrain/schedule", json={"schedule": s, "revision": None})
    assert r.status_code == 400 and "error" in r.get_json()


def test_run_rebuild_enqueues_recompute(client, conn):
    r = client.post("/api/retrain/runs", json={"kind": "rebuild"})
    assert r.status_code == 202
    assert commands(conn) == [{"kind": "recompute_baseline", "payload": {}}]
    assert r.get_json()["command_id"]


def test_run_reread_enqueues_scope(client, conn):
    conn.execute("INSERT INTO services VALUES ('a', 0)")
    conn.commit()
    d = (today() - timedelta(days=7)).isoformat()
    r = client.post("/api/retrain/runs", json=reread(services=["a"], days=[d, today().isoformat()],
                                                     hour_from=3, hour_to=5))
    assert r.status_code == 202 and r.get_json()["command_id"]
    assert commands(conn) == [{"kind": "retrain_run", "payload": {
        "services": ["a"], "days": [d, today().isoformat()], "hour_from": 3, "hour_to": 5}}]


def test_run_reread_validation_400(client, conn):
    conn.execute("INSERT INTO services VALUES ('a', 0)")
    conn.commit()
    t = today()
    bad = [{"days": []}, {"days": [(t + timedelta(days=1)).isoformat()]},
           {"days": [(t - timedelta(days=8)).isoformat()]}, {"days": ["26-10-2026"]},
           {"days": [t.isoformat(), t.isoformat()]}, {"days": "x"}, {"hour_from": -1}, {"hour_from": 5, "hour_to": 5},
           {"hour_to": 25}, {"hour_from": "1"}, {"services": []}, {"services": ["zzz"]}, {"services": "a"}]
    for patch in bad:
        for url in ("/api/retrain/runs", "/api/retrain/estimate"):
            r = client.post(url, json=reread(**patch))
            assert r.status_code == 400 and "error" in r.get_json(), (url, patch)
    assert client.post("/api/retrain/runs", json={"kind": "x"}).status_code == 400
    assert commands(conn) == []


def test_run_reread_409_while_running(client, conn):
    add_run(conn, state="running")
    conn.commit()
    r = client.post("/api/retrain/runs", json=reread())
    assert r.status_code == 409 and r.get_json() == {"error": "a retrain is running"}
    assert commands(conn) == []
    assert client.post("/api/retrain/estimate", json=reread()).status_code == 202


def test_estimate_enqueues(client, conn):
    r = client.post("/api/retrain/estimate", json=reread(hour_from=1, hour_to=2))
    assert r.status_code == 202 and r.get_json()["command_id"]
    assert commands(conn)[0]["kind"] == "retrain_estimate"
    assert commands(conn)[0]["payload"]["hour_to"] == 2


def test_cancel_404_409_202(client, conn):
    assert client.post("/api/retrain/runs/999/cancel").status_code == 404
    done = add_run(conn, state="done")
    rebuild = add_run(conn, kind="rebuild", state="running")
    run = add_run(conn, state="running")
    conn.commit()
    assert client.post(f"/api/retrain/runs/{done}/cancel").status_code == 409
    assert client.post(f"/api/retrain/runs/{rebuild}/cancel").status_code == 409
    r = client.post(f"/api/retrain/runs/{run}/cancel")
    assert r.status_code == 202 and r.get_json()["command_id"]
    assert commands(conn) == [{"kind": "retrain_cancel", "payload": {"run_id": run}}]


def test_llm_active_flag(client, conn):
    conn.execute("INSERT INTO llm_profiles (name, base_url, model, active, revision) VALUES ('p', 'u', 'm', false, 'r')")
    conn.commit()
    assert client.get("/api/retrain").get_json()["llm_active"] is False
    conn.execute("UPDATE llm_profiles SET active = true")
    conn.commit()
    assert client.get("/api/retrain").get_json()["llm_active"] is True


def test_engine_stale_flag(client, conn):
    conn.execute("INSERT INTO worker_status (worker, status) VALUES ('engine', %s)", (Jsonb({"heartbeat": time.time() - 3600}),))
    conn.commit()
    assert client.get("/api/retrain").get_json()["engine_stale"] is True
    conn.execute("UPDATE worker_status SET status = %s", (Jsonb({"heartbeat": time.time()}),))
    conn.commit()
    assert client.get("/api/retrain").get_json()["engine_stale"] is False

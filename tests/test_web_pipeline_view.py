from datetime import UTC, date, datetime, timedelta

import pytest

from logai2.web.app import create_app
from logai2.web.pipeline_view import services, stage_states, volume
from tests.conftest import TEST_DSN
from tests.test_web_baseline_view import base, committed, hours, service

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC).timestamp()
AT_27H_AGO = (datetime.fromtimestamp(NOW, UTC) - timedelta(hours=27)).isoformat()
ZERO_EPS_5 = [{"t": NOW - 60 * i, "lag": 5, "eps": 0} for i in range(5)]
ENGINE_OK = {"lag_seconds": 30, "detection_enabled": True, "flush_seconds": 0.2, "flush_seconds_max": 0.5,
             "last_commit": {"at": (datetime.fromtimestamp(NOW, UTC) - timedelta(hours=3)).isoformat(),
                             "today": "2026-10-21"},
             "warnings": [], "history": [{"t": NOW - 60 * i, "lag": 5, "eps": 40} for i in range(5)],
             "indices": {"a": {"caught_up": True, "failing": False}, "b": {"caught_up": True, "failing": False}},
             "miss_ratio": {"recharge": 0.004}}
LINKER_OK = {"heartbeat": NOW, "last_error": None, "warnings": [], "pending": {"templates": 0, "documents": 0},
             "llm_profiles": {"g": {"down_until": None}}, "llm_today": 12}


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


@pytest.mark.parametrize("engine, stale, linker, lstale, stage, expected", [
    ({"lag_seconds": 301, "detection_enabled": True}, False, LINKER_OK, False, "detection", "warn"),
    ({"lag_seconds": 30, "detection_enabled": False}, False, LINKER_OK, False, "detection", "warn"),
    (ENGINE_OK, True, LINKER_OK, False, "flush", "bad"),
    (ENGINE_OK, True, LINKER_OK, False, "reader", "bad"),
    (ENGINE_OK, True, LINKER_OK, False, "elasticsearch", "warn"),
    ({**ENGINE_OK, "flush_seconds_max": 10.5}, False, LINKER_OK, False, "flush", "warn"),
    ({**ENGINE_OK, "last_commit": {"at": AT_27H_AGO, "today": "2026-10-20"}}, False, LINKER_OK, False, "flush", "warn"),
    ({**ENGINE_OK, "warnings": ["high miss ratio: pay 62%"]}, False, LINKER_OK, False, "reader", "warn"),
    ({**ENGINE_OK, "history": ZERO_EPS_5, "indices": {"a": {"caught_up": False, "failing": False}}}, False, LINKER_OK, False, "reader", "bad"),
    ({**ENGINE_OK, "indices": {"a": {"failing": True}, "b": {"caught_up": True}}}, False, LINKER_OK, False, "elasticsearch", "warn"),
    ({**ENGINE_OK, "indices": {"a": {"failing": True}}}, False, LINKER_OK, False, "elasticsearch", "bad"),
    (ENGINE_OK, False, None, True, "linker", "bad"),
    (ENGINE_OK, False, {**LINKER_OK, "llm_profiles": {"g": {"down_until": NOW + 60}, "o": {"down_until": None}}}, False, "llm", "warn"),
    (ENGINE_OK, False, {**LINKER_OK, "llm_profiles": {"g": {"down_until": NOW + 60}}}, False, "llm", "bad"),
    (ENGINE_OK, False, LINKER_OK, False, "templates", "ok"),
])
def test_stage_states(engine, stale, linker, lstale, stage, expected):
    s = stage_states(engine, stale, linker, lstale, 0, NOW)[stage]
    assert s["status"] == expected and (s["reason"] is None) == (expected == "ok")


def test_unparsed_lines_degrade_templates():
    assert stage_states(ENGINE_OK, False, LINKER_OK, False, 3, NOW)["templates"]["status"] == "warn"


def seed(conn):
    for s in ("notify", "recharge", "quiet"):
        service(conn, s)
    committed(conn, "recharge", [date(2026, 10, d) for d in (20, 19, 16, 15, 14)])
    committed(conn, "notify", [date(2026, 10, d) for d in (20, 19, 16, 15, 14)])
    for h in (14, 15):
        base(conn, "recharge", "*total", "weekday", h, 540, 57)
    base(conn, "notify", "*total", "weekday", 14, 10, 1)
    hours(conn, "recharge", "*total", date(2026, 10, 21), {14: (600, 0, 20)})
    hours(conn, "recharge", "*total", date(2026, 10, 14), {14: (1500, 0, 60)})
    hours(conn, "recharge", "recharge:unparsed", date(2026, 10, 21), {14: (2, 0, 20)})
    hours(conn, "notify", "*total", date(2026, 10, 21), {14: (1000, 0, 20)})
    conn.execute("INSERT INTO templates VALUES ('recharge', 'recharge:T1', 'INFO', 'x', now(), now()), "
                 "('recharge', 'recharge:T2', 'INFO', 'y', now() - interval '3 days', now())")
    conn.execute("INSERT INTO alert_state VALUES ('recharge', '*total', 'ALERTING', now(), 1, 1, 1, NULL, NULL), "
                 "('recharge', '*error', 'NORMAL', now(), 0, 0, 0, NULL, NULL)")
    conn.commit()


def test_services_rate_last_week_z_and_order(conn, cfg):
    seed(conn)
    rows = services(conn, cfg, ENGINE_OK, date(2026, 10, 21), 14)
    assert [r["service"] for r in rows] == ["notify", "recharge", "quiet"]   # |z| desc, null last
    r = rows[1]
    assert r["rate"] == 30.0 and r["last_week"] == 25.0 and r["unparsed_1h"] == 2
    assert (r["templates"], r["new_24h"], r["not_normal"], r["miss_ratio"]) == (2, 1, 1, 0.004)
    assert round(r["z"], 1) == -8.9 and rows[2]["z"] is None and rows[2]["rate"] is None


def test_volume_today_hours_with_band(conn, cfg):
    seed(conn)
    v = {x["service"]: x for x in volume(conn, cfg, date(2026, 10, 21), 14)}["recharge"]["hours"]
    assert len(v) == 15 and v[14]["band_high"] == 711.0 and v[3]["rate"] is None
    assert v[14]["rate"] == 30.0 and v[14]["last_week"] == 25.0 and v[3]["band_high"] is None


def test_pipeline_route_has_new_fields(client):
    body = client.get("/api/pipeline").get_json()
    assert {"services", "volume", "stages", "status", "cursors", "stale", "linker", "linker_stale"} <= set(body)


def test_pipeline_lite_and_llm_cap(client):
    lite = client.get("/api/pipeline?lite=1").get_json()
    assert {"status", "cursors", "stale", "linker", "linker_stale"} <= set(lite)
    assert not {"services", "volume", "stages"} & set(lite)
    assert client.get("/api/pipeline").get_json()["llm_daily_max"] == 0


def test_malformed_last_commit_is_not_overdue():
    engine = {**ENGINE_OK, "last_commit": {"at": "garbage", "today": "2026-10-21"}}
    assert stage_states(engine, False, LINKER_OK, False, 0, NOW)["flush"]["status"] == "ok"

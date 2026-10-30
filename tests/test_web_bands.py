import threading
import time
from datetime import date, datetime, timedelta

import pytest
from test_bands import committed, exclude, hours, service

from logai2.bands import band, history, training, tune
from logai2.db import ensure_partitions
from logai2.timeutil import TZ
from logai2.web import bands_view
from logai2.web.app import create_app
from logai2.web.bands_view import bands_series, eval_keys
from logai2.web.baseline_view import UnknownKey
from tests.conftest import TEST_DSN

TODAY = date(2026, 10, 21)                                   # Wed
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=TZ).timestamp()
TRAIN = [date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19, 20)]
METHODS = ["sigma", "nb", "median", "ewma"]


@pytest.fixture
def client(conn, cfg, schema):
    cfg.database.dsn = f"{TEST_DSN}?options=-csearch_path%3D{schema}%2Cpublic"
    return create_app(cfg).test_client()


def cell(s, n=60):
    return (s, round(n * ((s / n) ** 2 + 4)), n)


def seed(conn, svc="s", key="*total", days=TRAIN, today_hours=range(0, 16)):
    service(conn, svc)
    for i, d in enumerate(days):
        hours(conn, svc, key, d, {h: cell(600 + 30 * ((i + h) % 5)) for h in range(24)})
    hours(conn, svc, key, TODAY, {h: cell(900) for h in today_hours})
    committed(conn, svc, days)


def test_bands_shape(conn, cfg):
    seed(conn)
    r = bands_series(conn, cfg, "s", "*total", 7, TODAY, NOW)
    assert len(r["points"]) == 6 * 24 + 16
    assert [m["id"] for m in r["methods"]] == METHODS
    assert all(len(m["bands"]) == len(r["points"]) and m["k"] == 3 and not m["tuned"] for m in r["methods"])
    assert r["tuned"] is False and r["kind"] == "*total" and r["run_at"] is None
    assert len(bands_series(conn, cfg, "s", "*total", 1, TODAY, NOW)["points"]) == 16
    p = next(p for p in r["points"] if p["day"] == "2026-10-21" and p["hour"] == 3)
    assert p["rate"] == 15.0


def test_bands_use_tuned_k(conn, cfg):
    seed(conn)
    res = tune(conn, cfg, TODAY, test_days=1)
    r = bands_series(conn, cfg, "s", "*total", 7, TODAY, NOW)
    nb = r["methods"][1]
    assert nb["tuned"] and nb["k"] == res["kinds"]["*total"]["methods"]["nb"]["k"]
    assert r["tuned"] and r["test_days"] == res["test_days"] and r["run_at"] == res["run_at"]
    assert nb["coverage"] == res["kinds"]["*total"]["methods"]["nb"]["coverage"]


def test_each_day_fitted_on_days_before_it(conn, cfg):
    seed(conn)
    r = bands_series(conn, cfg, "s", "*total", 7, TODAY, NOW)
    hist = history(conn, "s", "*total", date(2026, 10, 1), TODAY)
    i = next(i for i, p in enumerate(r["points"]) if (p["day"], p["hour"]) == ("2026-10-20", 3))
    want = band("sigma", training(hist, date(2026, 10, 20), 3, "sigma"), 3)
    got = r["methods"][0]["bands"][i]
    assert (got["low"], got["high"]) == pytest.approx((want.low, want.high))
    assert got["day"] == "2026-10-20" and got["hour"] == 3


def test_outside_counts_low_only_total(conn, cfg):
    for key, want in (("*total", 2), ("*error", 1)):
        seed(conn, svc=key[1:], key=key, today_hours=[])
        hours(conn, key[1:], key, TODAY, {1: cell(6000), 2: (0, 0, 60), 3: cell(610)})
        r = bands_series(conn, cfg, key[1:], key, 1, TODAY, NOW)
        assert r["methods"][0]["outside"] == want


def test_bands_no_history(conn, cfg):
    service(conn, "s")
    r = bands_series(conn, cfg, "s", "*total", 7, TODAY, NOW)
    assert all(m["missing"] == "not enough days" for m in r["methods"]) and len(r["points"]) == 160
    assert all(p["rate"] is None for p in r["points"])


def test_bands_errors(conn, cfg, client):
    seed(conn)
    conn.execute("INSERT INTO templates VALUES ('s', 'T1', 'INFO', 'hello', now() - interval '1 hour', now())")
    conn.commit()
    assert client.get("/api/eval/bands?service=s&key=*total&days=14").status_code == 400
    assert client.get("/api/eval/bands?service=s&key=doc:x&days=7").status_code == 404
    assert client.get("/api/eval/bands?service=s&key=T1&days=7").status_code == 404
    assert client.get("/api/eval/bands?service=nope&key=*total&days=7").status_code == 404
    conn.execute("UPDATE services SET monitored = false WHERE service = 's'")
    conn.commit()
    assert client.get("/api/eval/bands?service=s&key=*total&days=7").status_code == 404
    assert client.get("/api/eval/keys?service=s").status_code == 404
    with pytest.raises(UnknownKey):
        bands_series(conn, cfg, "s", "T1", 7, TODAY, NOW)


def test_eval_keys_scope_and_order(conn):
    seed(conn)
    old = "now() - interval '3 days'"
    for tid, lvl, text, ts in (("T2", "INFO", "zeta", old), ("T3", "ERROR", "beta", old), ("T4", "ERROR", "alpha", old),
                               ("T5", "WARN", "mid", old), ("T6", "INFO", "new", "now() - interval '1 hour'"),
                               ("doc:1", "INFO", "d", old)):
        conn.execute(f"INSERT INTO templates VALUES ('s', %s, %s, %s, {ts}, now())", (tid, lvl, text))
    conn.commit()
    ks = eval_keys(conn, "s", datetime.now(TZ))
    assert [k["key"] for k in ks] == ["*total", "*error", "*warn", "*info", "T4", "T3", "T5", "T2"]
    assert set(ks[0]) == {"key", "label", "kind"} and ks[0]["kind"] == "*total" and ks[2]["kind"] == "*warn/*info"
    assert "alpha" in ks[4]["label"]
    conn.execute("UPDATE services SET monitored = false")
    conn.commit()
    assert eval_keys(conn, "s", datetime.now(TZ)) == []


def test_retune_conflict_and_old_result_kept(conn, cfg, client, monkeypatch):
    seed(conn)
    old = tune(conn, cfg, TODAY, test_days=1)
    go, started = threading.Event(), threading.Event()

    def slow(c, cfg, today, test_days=3, on_key=None):
        started.set()
        on_key(1, 4)
        go.wait(5)
        return {}
    monkeypatch.setattr(bands_view, "tune", slow)
    try:
        assert client.post("/api/eval/tune").status_code == 202
        assert started.wait(5)
        assert client.post("/api/eval/tune").status_code == 409
        s = client.get("/api/eval/tune").get_json()
        assert s["status"] == "running" and s["result"]["run_at"] == old["run_at"] and s["error"] is None
        assert s["progress"] == {"done": 1, "total": 4}
    finally:
        go.set()
    for _ in range(50):
        if client.get("/api/eval/tune").get_json()["status"] == "idle":
            break
        time.sleep(0.1)
    assert client.get("/api/eval/tune").get_json()["status"] == "idle"


def test_retune_error_caught(conn, client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(bands_view, "tune", boom)
    assert client.post("/api/eval/tune").status_code == 202
    for _ in range(50):
        s = client.get("/api/eval/tune").get_json()
        if s["status"] == "idle":
            break
        time.sleep(0.1)
    assert s["status"] == "idle" and "nope" in s["error"] and s["result"] is None


def test_no_lab_routes(client):
    for p in ("runs", "scoreboard", "series", "sweep", "choices"):
        assert client.get(f"/api/eval/{p}").status_code == 404


def test_bands_timing(conn, cfg):
    ensure_partitions(conn, date(2026, 9, 1), date(2026, 9, 30))
    conn.commit()
    days = [TODAY - timedelta(days=i) for i in range(1, 31)]
    seed(conn, days=sorted(days), today_hours=range(0, 16))
    tune(conn, cfg, TODAY, test_days=1)
    t0 = time.monotonic()
    bands_series(conn, cfg, "s", "*total", 7, TODAY, NOW)
    assert time.monotonic() - t0 < 0.3

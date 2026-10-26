import logging

from logai2.jobs import runs


def test_migration_008(conn):
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'retrain_runs' "
        "AND table_schema = current_schema()")}
    assert cols == {"id", "kind", "trigger", "scope", "state", "started_at", "finished_at", "pos_ms", "steps",
                    "result", "error", "error_type", "failed_step", "log"}


def test_runs_step_upserts_by_key(conn):
    rid = runs.start(conn, "rebuild", "manual")
    runs.step(conn, rid, "a", "A", {"x": 1}, 1.0, None)
    runs.step(conn, rid, "b", "B", {}, 2.0, 3.0)
    runs.step(conn, rid, "a", "A", {"x": 2}, 1.0, 4.0)
    row = conn.execute("SELECT state, steps FROM retrain_runs WHERE id = %s", (rid,)).fetchone()
    assert row["state"] == "running"
    assert [s["key"] for s in row["steps"]] == ["a", "b"]
    assert row["steps"][0] == {"key": "a", "label": "A", "started_at": 1.0, "finished_at": 4.0, "detail": {"x": 2}}


def test_runs_finish_sets_state_and_log(conn):
    rid = runs.start(conn, "rebuild", "schedule", {"k": 1})
    runs.finish(conn, rid, "failed", {"n": 1}, error="boom", error_type="RuntimeError", failed_step="rebuild",
                log=[{"message": "m"}])
    row = conn.execute("SELECT * FROM retrain_runs WHERE id = %s", (rid,)).fetchone()
    assert (row["state"], row["error"], row["error_type"], row["failed_step"]) == \
        ("failed", "boom", "RuntimeError", "rebuild")
    assert row["finished_at"] is not None and row["result"] == {"n": 1} and row["log"] == [{"message": "m"}]
    assert row["scope"] == {"k": 1}


def test_run_log_keeps_warnings():
    h = runs.RunLog()
    h.attach()
    try:
        lg = logging.getLogger("logai2.x")
        lg.info("quiet")
        lg.warning("w %s", 1)
        lg.error("e")
        for i in range(60):
            lg.warning("n%d", i)
    finally:
        h.detach()
    logging.getLogger("logai2.x").warning("after detach")
    assert len(h.lines) == 50
    assert h.lines[0]["message"] == "n10" and h.lines[-1]["message"] == "n59"
    assert h.lines[0]["level"] == "WARNING" and h.lines[0]["logger"] == "logai2.x"
    assert isinstance(h.lines[0]["ts"], float)

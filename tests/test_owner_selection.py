"""Owner keeps only active services: Off is not loaded, zero-filled, judged or alerting (pm3)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from logai2.config import load_config
from logai2.owner.miners import Miners
from logai2.owner.owner import Owner

T0 = int(datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 10:00 local, Tuesday
M = T0 // 60_000
KNOWN = "payment failed for user <*>"
NOW = T0 / 1000 + 6 * 3600


def rows(conn, q, *args):
    out = conn.execute(q, args or None).fetchall()
    conn.rollback()   # flush needs an idle connection
    return out


def seed(conn, extra_pairs=()):
    """Pairs (a, sa) and (b, sb); both services have a template, baseline rows, a blob and an ALERTING state."""
    seen = datetime.fromtimestamp((M - 1440) * 60, timezone.utc)
    miners = Miners(load_config(None).drain3, {})
    for s in ("sa", "sb"):
        miners.match(s, KNOWN)
    blobs = miners.dirty()
    with conn.transaction():
        for p, s in [("a", "sa"), ("b", "sb"), *extra_pairs]:
            conn.execute("INSERT INTO index_services VALUES (%s, %s)", (p, s))
        for s in ("sa", "sb"):
            conn.execute("INSERT INTO services VALUES (%s, 0)", (s,))
            conn.execute("INSERT INTO drain3_state (service, blob) VALUES (%s, %s)", (s, blobs[s]))
            conn.execute("INSERT INTO templates VALUES (%s, %s, 'INFO', %s, %s, %s)",
                         (s, f"{s}:T00001", KNOWN, seen, seen))
            for dt in ("weekday", "weekend"):
                for h in range(24):
                    conn.execute("INSERT INTO baseline VALUES (%s, %s, %s, %s, 5, 1, 5)", (s, f"{s}:T00001", dt, h))
            for d in range(5):
                conn.execute("INSERT INTO day_status VALUES (%s, %s, true, 'weekday')",
                             (s, date(2026, 10, 12) + timedelta(days=d)))
        conn.execute("INSERT INTO alert_state VALUES ('sb', 'sb:T00001', 'ALERTING', now(), 0, 2.0, 9, 1, 4)")
        conn.execute("INSERT INTO alert_state VALUES ('sa', 'sa:T00001', 'ALERTING', now(), 0, 2.0, 9, 1, 4)")


def count(service, minute, n=1, template_id=None, text=KNOWN):
    return {"service": service, "template_id": template_id or (None if text else f"{service}:T00001"),
            "text": text, "level": "INFO", "module": "", "minute": minute, "n": n, "sample": "x"}


def batch(counts, watermark_ms, sources=()):
    return {"counts": counts, "watermark_ms": watermark_ms, "events": len(counts), "positions": {},
            "sources": list(sources)}


def drive(o, until_ms):
    """Close up to until_ms in bites, flushing like the engine."""
    while o.minutes.next_to_close < until_ms // 60_000:
        o.handle(batch([], until_ms))
        o.flush({"a": (o.boundary_ms, T0)}, {})


def make(conn, cfg, enabled={"a"}, disabled=frozenset({"b"}), **kw):
    seed(conn, **kw)
    return Owner(conn, cfg, M, now=lambda: NOW, enabled=enabled, disabled=disabled)


def test_off_service_not_loaded(conn, cfg):
    o = make(conn, cfg)
    assert o.active == {"sa"} and "sb" not in o.services
    assert not o.detector.baseline.has("sb", "sb:T00001") and ("sb", "sb:T00001") not in o.levels
    assert o.detector.baseline.has("sa", "sa:T00001")
    assert "sb" not in o.miners._miners and ("sb", "sb:T00001") not in o.detector.states
    assert o.detector.baseline.days.keys() <= {("sa", "weekday")}


def test_default_selection_loads_every_pattern(conn, cfg):
    seed(conn)
    o = Owner(conn, cfg, M, now=lambda: NOW)
    assert o.active == {"sa", "sb"} and o.skipped_off == 0


def test_removed_pattern_services_not_loaded(conn, cfg):
    seed(conn)
    o = Owner(conn, cfg, M, now=lambda: NOW, enabled={"a"}, disabled=frozenset())   # b is neither On nor Off
    assert o.active == {"sa"} and "sb" not in o.services


def test_disable_slowest_pattern_jump_writes_no_zeros(conn, cfg):
    cfg.flush.max_closed_minutes = 60
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    o.handle(batch([count("sa", M, 2), count("sb", M, 3)], (M + 5) * 60_000))
    o.flush({"a": (o.boundary_ms, T0)}, {})
    before = rows(conn, "SELECT count(*) AS n FROM hourly_counts WHERE service = 'sb'")[0]["n"]
    hours = rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours WHERE service = 'sb'")[0]["n"]
    assert before and hours == 5
    o.set_selection({"a"}, {"b"})
    jump = (M + 180) * 60_000
    o.handle(batch([count("sa", M + 10, 4), count("sa", M + 150, 5)], jump))
    o.flush({"a": (o.boundary_ms, T0)}, {})
    drive(o, jump)
    assert rows(conn, "SELECT count(*) AS n FROM hourly_counts WHERE service = 'sb'")[0]["n"] == before
    assert rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours WHERE service = 'sb'")[0]["n"] == hours
    assert rows(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE service = 'sa' AND key = '*total'")[0]["n"] == 11
    assert rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours WHERE service = 'sa'")[0]["n"] == 180


def test_disable_resets_alerts(conn, cfg):
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    assert o.detector.states[("sb", "sb:T00001")].state == "ALERTING"
    o.set_selection({"a"}, {"b"})
    o.flush({}, {})
    assert rows(conn, "SELECT state, streak FROM alert_state WHERE service = 'sb'")[0] == {"state": "NORMAL", "streak": 0}
    (ch,) = rows(conn, "SELECT from_state, to_state, count FROM alert_changes WHERE service = 'sb'")
    assert (ch["from_state"], ch["to_state"], ch["count"]) == ("ALERTING", "NORMAL", 9)
    assert rows(conn, "SELECT state FROM alert_state WHERE service = 'sa'")[0]["state"] == "ALERTING"   # untouched
    assert ("sb", "sb:T00001") not in o.detector.states   # pruned once persisted


def test_disable_drops_open_minutes(conn, cfg):
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    o.handle(batch([count("sa", M + 1), count("sb", M + 1), count("sb", M + 2)], M * 60_000))
    assert any(k[0] == "sb" for cm in o.minutes._open.values() for k in cm.keys)
    o.minutes.hour_keys[(date(2026, 10, 20), 10)] = {("sb", "x"), ("sa", "y")}
    o.set_selection({"a"}, {"b"})
    assert all(k[0] != "sb" for cm in o.minutes._open.values() for k in cm.keys)
    assert all(k[0] != "sb" for cm in o.minutes._open.values() for k in cm.modules)
    assert o.minutes.hour_keys[(date(2026, 10, 20), 10)] == {("sa", "y")}
    assert any(k[0] == "sa" for cm in o.minutes._open.values() for k in cm.keys)


def test_unload_keeps_closed_pending_rows(conn, cfg):
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    # a new template in a minute that closes before the switch; nothing flushed yet
    o.handle(batch([count("sb", M, 3, text="brand new shape of line 1 2 3 foo")], (M + 2) * 60_000))
    (tid,) = {k[1] for k in o._templates if k[0] == "sb"}
    assert tid != "sb:T00001"
    o.set_selection({"a"}, {"b"})
    o.flush({"a": (o.boundary_ms, T0)}, {})
    assert rows(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE service = 'sb' AND key = %s", tid)[0]["n"] == 3
    assert rows(conn, "SELECT level FROM templates WHERE service = 'sb' AND template_id = %s", tid)


def test_unloaded_miner_state_survives_a_reload_before_the_flush(conn, cfg):
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    tid = o.miners.match("sb", "brand new shape of line 1 2 3 foo")[0]
    assert tid != "sb:T00001"
    o.set_selection({"a"}, {"b"})
    o.set_selection({"a", "b"}, set())   # the database blob is still the old one: the pending blob must win
    assert o.miners.match("sb", "brand new shape of line 1 2 3 foo")[0] == tid


def test_off_wins_over_on_pair(conn, cfg):
    o = make(conn, cfg, extra_pairs=[("a", "sb")])
    o.handle(batch([count("sb", M, 7), count("sa", M, 2)], (M + 2) * 60_000))
    assert "sb" not in o.active and o.skipped_off == 7
    assert "sb" not in o.services and all(k[0] != "sb" for cm in o.minutes._open.values() for k in cm.keys)


def test_reenable_loads_state_and_keeps_template_ids(conn, cfg):
    o = make(conn, cfg)
    o.set_selection({"a", "b"}, set())
    assert o.active == {"sa", "sb"}
    assert o.detector.baseline.has("sb", "sb:T00001") and o.detector.baseline.days[("sb", "weekday")] == 5
    assert o.miners.match("sb", "payment failed for user 7")[0] == "sb:T00001"
    assert o.levels[("sb", "sb:T00001")] == "INFO"
    assert o.detector.states[("sb", "sb:T00001")].state == "ALERTING"


def test_reenable_zero_fill_starts_at_first_count(conn, cfg):
    o = make(conn, cfg)
    o.handle(batch([count("sa", M, 1)], (M + 30) * 60_000))
    o.set_selection({"a", "b"}, set())
    assert "sb" not in o.services      # the gap stays a gap: the first real count sets the first minute
    o.handle(batch([count("sb", M + 35, 1)], (M + 40) * 60_000, sources=[("b", "sb")]))
    o.flush({"a": (o.boundary_ms, T0)}, {})
    assert rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours WHERE service = 'sb'")[0]["n"] == 5


def test_reenable_while_other_pattern_lags_writes_no_zeros(conn, cfg):
    cfg.flush.max_closed_minutes = 60
    o = make(conn, cfg)
    o.set_selection({"a", "b"}, set())          # a lags: the owner is 2 h behind real time
    o.handle(batch([count("sb", M + 120, 1)], (M + 121) * 60_000, sources=[("b", "sb")]))
    drive(o, (M + 121) * 60_000)
    assert rows(conn, "SELECT count(*) AS n FROM hourly_counts WHERE service = 'sb' AND key = '*total' AND sum = 0")[0]["n"] == 0
    assert rows(conn, "SELECT sum(n_minutes) AS n FROM service_hours WHERE service = 'sb'")[0]["n"] == 1


def test_unpaired_service_not_active_with_selection(conn, cfg):
    seed(conn)
    with conn.transaction():
        conn.execute("INSERT INTO services VALUES ('sz', 0)")
    o = Owner(conn, cfg, M, now=lambda: NOW, enabled=set(), disabled={"a", "b"})
    assert o.active == set() and "sz" not in o.services
    drive(o, (M + 180) * 60_000)
    assert rows(conn, "SELECT count(*) AS n FROM service_hours WHERE service = 'sz'")[0]["n"] == 0
    o.handle(batch([count("sz", M + 181, 1)], (M + 182) * 60_000))   # first count: lazy load
    assert "sz" in o.active
    assert Owner(conn, cfg, M, now=lambda: NOW).active >= {"sz"}      # no selection: today's behaviour


def test_inactive_at_start_alerts_reset_on_first_flush(conn, cfg):
    o = make(conn, cfg)
    o.flush({}, {})
    assert rows(conn, "SELECT state FROM alert_state WHERE service = 'sb'")[0]["state"] == "NORMAL"
    (ch,) = rows(conn, "SELECT from_state, to_state FROM alert_changes WHERE service = 'sb'")
    assert (ch["from_state"], ch["to_state"]) == ("ALERTING", "NORMAL")
    assert rows(conn, "SELECT state FROM alert_state WHERE service = 'sa'")[0]["state"] == "ALERTING"
    o.set_selection({"a", "b"}, set())
    assert o.detector.states[("sb", "sb:T00001")].state == "NORMAL"
    o.set_selection({"a"}, {"b"})
    o.flush({}, {})
    assert rows(conn, "SELECT count(*) AS n FROM alert_changes WHERE service = 'sb'")[0]["n"] == 1   # no second reset


def test_normal_state_with_streak_resets_without_a_change(conn, cfg):
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    from logai2.owner.detect import AlertRow
    o.detector.states[("sb", "sb:T00001")] = AlertRow("sb", "sb:T00001", "NORMAL", 2, 0.0)
    o.set_selection({"a"}, {"b"})
    assert o._changes == [] and o.detector.states[("sb", "sb:T00001")].streak == 0


def test_lane_only_service_inserted_unmonitored(conn, cfg):
    o = make(conn, cfg)
    o._services["sx"] = 3           # as add_lane does for a service the live owner does not hold
    o.flush({}, {})
    assert rows(conn, "SELECT monitored FROM services WHERE service = 'sx'")[0]["monitored"] is False


def test_new_service_in_on_pattern_activates(conn, cfg):
    o = make(conn, cfg)
    o.handle(batch([count("sc", M, 2)], (M + 2) * 60_000, sources=[("a", "sc")]))
    assert "sc" in o.active and o.skipped_off == 0
    o.flush({"a": (o.boundary_ms, T0)}, {})
    assert {r["service"]: r["monitored"] for r in rows(conn, "SELECT * FROM services")} == {
        "sa": True, "sb": False, "sc": True}


def test_removed_pattern_service_comes_back_through_a_broader_pattern(conn, cfg):
    seed(conn)
    o = Owner(conn, cfg, M, now=lambda: NOW, enabled={"a"}, disabled=frozenset())   # b removed
    o.handle(batch([count("sb", M, 2)], (M + 2) * 60_000, sources=[("a", "sb")]))
    assert "sb" in o.active and o.miners.match("sb", KNOWN)[0] == "sb:T00001"


def test_monitored_column_follows_selection(conn, cfg):
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    o.flush({}, {})
    monitored = lambda: {r["service"]: r["monitored"] for r in rows(conn, "SELECT * FROM services")}   # noqa: E731
    assert monitored() == {"sa": True, "sb": True}
    o.set_selection({"a"}, {"b"})
    o.flush({}, {})
    assert monitored() == {"sa": True, "sb": False}
    o.set_selection({"a", "b"}, set())
    o.flush({}, {})
    assert monitored() == {"sa": True, "sb": True}


def test_start_writes_monitored_for_an_off_service(conn, cfg):
    o = make(conn, cfg)
    o.flush({}, {})
    assert {r["service"]: r["monitored"] for r in rows(conn, "SELECT * FROM services")} == {"sa": True, "sb": False}


def test_off_service_is_not_judged_after_switch(conn, cfg):
    """Detection on: an Off service's *total drop (zero minutes) must not alert; it was reset NORMAL."""
    from logai2.db import put_setting
    put_setting(conn, "detection_enabled", True)
    conn.commit()
    o = make(conn, cfg, enabled={"a", "b"}, disabled=frozenset())
    o.set_selection({"a"}, {"b"})
    o.handle(batch([], (M + 10) * 60_000))
    o.flush({}, {})
    assert rows(conn, "SELECT state FROM alert_state WHERE service = 'sb'")[0]["state"] == "NORMAL"
    assert rows(conn, "SELECT count(*) AS n FROM alert_changes WHERE service = 'sb' AND to_state <> 'NORMAL'")[0]["n"] == 0


def test_reload_baseline_loads_active_only(conn, cfg):
    o = make(conn, cfg)
    o.reload_baseline()
    assert o.detector.baseline.has("sa", "sa:T00001") and not o.detector.baseline.has("sb", "sb:T00001")


def test_max_closed_minutes_must_be_positive(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("flush:\n  max_closed_minutes: 0\n")
    with pytest.raises(ValueError, match="max_closed_minutes"):
        load_config(str(p))

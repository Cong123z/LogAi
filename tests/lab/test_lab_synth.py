import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from logai2.timeutil import TZ
from tests.conftest import TEST_DSN
from scripts.lab.data import minute_at
from scripts.lab.load import load_service
from scripts.lab.synth import generate, run_engine, write
from scripts.lab.world import build_world

ROOT = Path(__file__).resolve().parents[2]
END = date(2026, 10, 20)
LEVELS = ["ERROR", "WARN", "INFO"]


def mkworld(n=6, mean=2.0, var=None):
    ts = [{"template_id": f"T{i}", "text": f"conn <*> failed in step{i} now", "level": LEVELS[i % 3],
           "doc_id": "D1" if i == 0 else None, "mean": [mean + (i == 0) * 18 + (h % 3) * 0.1 for h in range(24)],
           "var": [var if var is not None else mean] * 24, "mirrored": [False] * 24} for i in range(n)]
    return {"services": {"orders": ts}}


def scen(**kw):
    s = {"seed": 7, "days": 4, "services": {"count": 2, "scale": [1.0, 0.5], "subset": 1.0, "phase_minutes": [0, 20]},
         "day_factors": {"weekday": 1.0, "jitter": 0.0, "sat": 0.75, "sun": 0.65},
         "holiday": {"start_day": 0, "length": 0, "factor": 0.4}, "trend": {"per_week": 0.0},
         "dispersion": "poisson", "new_templates": {"per_service_day": 0, "error_share": 0.3, "rate": 1.0},
         "gaps": [], "incidents": []}
    s.update(kw)
    return s


@pytest.fixture
def lab_schema(conn):
    name = f"lab_s_{uuid.uuid4().hex[:8]}"
    yield name
    conn.rollback()
    conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    conn.commit()


def rows(conn, schema, sql, args=()):
    return conn.execute(sql.replace("S.", f"{schema}."), args).fetchall()


def test_same_seed_same_world_other_seed_differs():
    w = mkworld()
    a, b, c = generate(w, scen(), END), generate(w, scen(), END), generate(w, scen(seed=8), END)
    assert a.digest() == b.digest() != c.digest()


def test_invariants_and_hourly(conn, lab_schema):
    s = scen(days=2, gaps=[{"day": 0, "hour_from": 3, "hour_to": 5}],
             incidents=[{"type": "spike", "size": 3, "minutes": 10, "every_days": 1}],
             new_templates={"per_service_day": 1, "error_share": 0.5, "rate": 1.0})
    gen = generate(mkworld(), s, END)
    for sv in gen.services:
        tids = [t["template_id"] for t in sv.templates]
        for d in range(2):
            for i in range(1440):
                tot = sum(sv.counts[t][d][i] for t in tids)
                assert sv.counts["*total"][d][i] == tot
                assert sum(sv.counts[k][d][i] for k in ("*error", "*warn", "*info")) == tot
                gap = d == 0 and 180 <= i < 300
                assert sv.counts["*minutes"][d][i] == (0 if gap else 1)
                assert not gap or tot == 0
    write(conn, lab_schema, gen)
    conn.commit()
    sv = gen.services[0]
    day0 = gen.days[0]
    for key in ("*total", "T0", "*error"):
        for h in (0, 4, 10):
            a = sv.counts[key][0][h * 60:(h + 1) * 60]
            r = rows(conn, lab_schema, "SELECT sum, sumsq FROM S.hourly_counts WHERE service=%s AND key=%s "
                     "AND day=%s AND hour=%s", (sv.name, key, day0, h))
            lm = rows(conn, lab_schema, "SELECT counts FROM S.lab_minute_counts WHERE service=%s AND key=%s "
                      "AND day=%s AND hour=%s", (sv.name, key, day0, h))
            if sum(a) == 0:
                assert not r and not lm
            else:
                assert (r[0]["sum"], r[0]["sumsq"]) == (sum(a), sum(x * x for x in a)) and lm[0]["counts"] == a.tolist()
    n = {r["hour"]: r["n_minutes"] for r in rows(conn, lab_schema, "SELECT hour, n_minutes FROM S.service_hours "
                                                 "WHERE service=%s AND day=%s", (sv.name, day0))}
    assert n[2] == 60 and 3 not in n and 4 not in n
    assert not rows(conn, lab_schema, "SELECT 1 FROM S.hourly_counts WHERE key = '*minutes'")
    ds = {r["day"]: r["committed"] for r in rows(conn, lab_schema, "SELECT day, committed FROM S.day_status "
                                                 "WHERE service=%s", (sv.name,))}
    assert ds == {gen.days[0]: True}
    assert rows(conn, lab_schema, "SELECT 1 FROM S.template_doc_links WHERE source='manual' AND doc_id='D1'")


def test_day_factors_and_holiday():
    s = scen(days=28, services={"count": 1, "scale": [1.0], "subset": 1.0, "phase_minutes": [0]},
             holiday={"start_day": 15, "length": 2, "factor": 0.4})
    s["day_factors"].update(sat=0.75, sun=0.65)
    gen = generate(mkworld(), s, END)
    tot = [sum(gen.services[0].counts["*total"][d]) for d in range(28)]
    wd = lambda d: gen.days[d].weekday()
    hol = {15, 16}
    week = [tot[d] for d in range(28) if wd(d) < 5 and d not in hol]
    wk = sum(week) / len(week)
    sat = [tot[d] for d in range(28) if wd(d) == 5 and d not in hol]
    assert abs(sum(sat) / len(sat) / wk - 0.75) < 0.05 * 0.75
    h = [tot[d] for d in hol if wd(d) < 5]
    assert h and abs(sum(h) / len(h) / wk - 0.4) < 0.05 * 0.4 + 0.01


def test_incidents(conn, lab_schema):
    s = scen(days=6, services={"count": 1, "scale": [1.0], "subset": 1.0, "phase_minutes": [0]},
             day_factors={"weekday": 1.0, "jitter": 0.0, "sat": 1.0, "sun": 1.0},
             incidents=[{"type": "spike", "size": 5, "minutes": 30, "every_days": 2, "key": "T0"},
                        {"type": "drop", "size": 0.3, "minutes": 30, "every_days": 3},
                        {"type": "mix_swap", "minutes": 20, "every_days": 4}])
    gen = generate(mkworld(), s, END)
    sv = gen.services[0]
    inc = sorted(sv.incidents, key=lambda i: i["start_minute"])
    assert {i["type"] for i in inc} == {"spike", "drop", "mix_swap"}
    for a, b in zip(inc, inc[1:]):
        assert a["end_minute"] <= b["start_minute"]
    d0 = minute_at(gen.days[0], 0)
    mean_t0 = 20.0 + 0.1 * 1.0   # about; hours cycle 0.0-0.2
    for i in inc:
        a, b = i["start_minute"] - d0, i["end_minute"] - d0
        if i["type"] == "spike":
            got = sum(sv.counts["T0"][a // 1440][a % 1440:b % 1440 or 1440])
            assert abs(got / (5 * mean_t0 * 30) - 1) < 0.1
        if i["type"] == "drop":
            assert i["key"] == "*total"
            got = sum(sv.counts["*total"][a // 1440][a % 1440:b % 1440 or 1440])
            assert abs(got / (0.3 * (mean_t0 + 5 * 2.1) * 30) - 1) < 0.15
        if i["type"] == "mix_swap":
            assert i["key"] is None and set(i["meta"]) >= {"a", "b"}
    write(conn, lab_schema, gen)
    r = rows(conn, lab_schema, "SELECT * FROM S.lab_incidents ORDER BY start_minute")
    assert len(r) == len(inc) and {x["source"] for x in r} == {"synthetic"} and {x["data_schema"] for x in r} == {lab_schema}
    assert [x["start_minute"] for x in r] == [i["start_minute"] for i in inc]


def test_new_templates(conn, lab_schema):
    s = scen(days=3, new_templates={"per_service_day": 2, "error_share": 0.5, "rate": 3.0})
    gen = generate(mkworld(), s, END)
    sv = gen.services[0]
    new = [t for t in sv.templates if ":N" in t["template_id"]]
    assert len(new) == 6 and sorted(t["template_id"] for t in new)[0].startswith(f"{sv.name}:N0")
    d0 = minute_at(gen.days[0], 0)
    for t in new:
        b = t["first_seen"]
        flat = [x for d in range(3) for x in sv.counts[t["template_id"]][d]]
        assert sum(flat[:b - d0]) == 0 and sum(flat[b - d0:]) > 0
        assert t["level"] in ("ERROR", "INFO") and t["text"] != mkworld()["services"]["orders"][0]["text"]
    write(conn, lab_schema, gen)
    r = {x["template_id"]: x["first_seen"] for x in rows(conn, lab_schema, "SELECT template_id, first_seen "
                                                         "FROM S.templates WHERE service=%s", (sv.name,))}
    for t in new:
        assert r[t["template_id"]].timestamp() == t["first_seen"] * 60
    assert r["T1"].timestamp() == (d0 - 30 * 1440) * 60


def test_write_touches_only_scenario_schema(conn, lab_schema):
    def snap():
        out = {}
        for t in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall():
            out[t["tablename"]] = conn.execute(f'SELECT count(*) AS n FROM public."{t["tablename"]}"').fetchone()["n"]
        return out
    before = snap()
    other = conn.execute("SELECT current_schema() AS s").fetchone()["s"]     # migrated, on the path behind the scenario
    versions = lambda sch: [r["version"] for r in conn.execute(f"SELECT version FROM {sch}.schema_migrations ORDER BY 1")]
    other_v = versions(other)
    write(conn, lab_schema, generate(mkworld(), scen(days=2), END))
    conn.commit()
    assert versions(lab_schema) == other_v and versions(other) == other_v
    assert not conn.execute(f"SELECT 1 FROM {other}.templates").fetchall()
    assert len(conn.execute(f"SELECT 1 FROM pg_tables WHERE schemaname='{lab_schema}'").fetchall()) > 30
    assert snap() == before
    assert rows(conn, lab_schema, "SELECT count(*) AS n FROM S.lab_minute_counts")[0]["n"] > 0


def test_load_service_roundtrip(conn, lab_schema):
    s = scen(days=3, gaps=[{"day": 1, "hour_from": 6, "hour_to": 7}])
    gen = generate(mkworld(), s, END)
    write(conn, lab_schema, gen)
    conn.commit()
    sv = gen.services[1]
    conn.execute(f"SET search_path TO {lab_schema}, public")
    data = load_service(conn, sv.name, gen.days[0], gen.days[-1], 0)
    for key in ("*total", "T0", "T2", "*error"):
        for d, day in enumerate(gen.days):
            want = [x if o else -1 for x, o in zip(sv.counts[key][d], sv.counts["*minutes"][d])]
            assert list(data.minutes[key][day]) == want


def mk_source(conn):
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    conn.execute("INSERT INTO services VALUES ('a', 0)")
    for t, lvl in (("x", "ERROR"), ("y", "INFO"), ("z", "INFO")):
        conn.execute("INSERT INTO templates VALUES ('a', %s, %s, %s, now(), now())", (t, lvl, f"text {t}"))
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('D', 't', '', 'r')")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('a','x','D','manual')")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('a','y','D','draft')")
    for d in (d1, d2):
        for h in (0, 1, 5):     # hour 5: observed but silent
            conn.execute("INSERT INTO service_hours VALUES ('a', %s, %s, 60)", (d, h))
    conn.execute("INSERT INTO service_hours VALUES ('a', %s, 7, 20)", (d1,))     # active but < 30 minutes: not used
    # x: hour 0 -> sums 120 and 240 over 120 minutes; hour 1 -> only day 1, 60
    for d, h, s, q in ((d1, 0, 120, 400), (d2, 0, 240, 1000), (d1, 1, 60, 100)):
        conn.execute("INSERT INTO hourly_counts VALUES ('a','x',%s,%s,%s,%s)", (d, h, s, q))
        conn.execute("INSERT INTO hourly_counts VALUES ('a','*total',%s,%s,%s,%s)", (d, h, s, q))
    conn.execute("INSERT INTO hourly_counts VALUES ('a','*total','2026-10-01',7,50,50)")
    conn.execute("INSERT INTO hourly_counts VALUES ('a','z','2026-10-01',0,0,0)")   # all zero: dropped
    conn.commit()


def test_world_from_source(conn, capsys):
    mk_source(conn)
    w = build_world(conn)
    assert w["active_hours"]["a"] == [0, 1]
    assert "warning" in capsys.readouterr().err.lower()      # fewer than 8 active hours
    ts = {t["template_id"]: t for t in w["services"]["a"]}
    assert set(ts) == {"x"}   # y, z have no counts
    x = ts["x"]
    assert x["doc_id"] == "D" and x["level"] == "ERROR" and x["text"] == "text x"
    assert x["mean"][0] == pytest.approx(360 / 120)
    assert x["var"][0] == pytest.approx(1400 / 120 - 3.0 ** 2)
    assert x["mean"][1] == pytest.approx(60 / 120)
    assert not x["mirrored"][0] and not x["mirrored"][1]
    assert x["mirrored"][23] and x["mean"][23] == x["mean"][0]       # 23 - 23 = 0
    assert x["mirrored"][22] and x["mean"][22] == x["mean"][1]
    over = (360 / 120 + 60 / 120) / 2
    for h in (5, 7, 12):                                             # silent, too short, not observed
        assert x["mean"][h] == pytest.approx(over) and x["mirrored"][h]
    # a draft link is not an alerting source
    conn.execute("INSERT INTO hourly_counts VALUES ('a','y','2026-10-01',0,5,25)")
    conn.commit()
    assert {t["template_id"]: t["doc_id"] for t in build_world(conn)["services"]["a"]}["y"] is None


def test_tiny_end_to_end(conn, lab_schema):
    big = mkworld(n=170)
    s = yaml.safe_load((ROOT / "lab/scenarios/tiny.yaml").read_text())
    t0 = time.time()
    gen = generate(big, s, END)
    write(conn, lab_schema, gen)
    conn.commit()
    assert time.time() - t0 < 10
    assert len(gen.services) == 1 and len([t for t in gen.services[0].templates if ":N" not in t["template_id"]]) == 5
    assert len(gen.services[0].incidents) == 1
    assert yaml.safe_load((ROOT / "lab/scenarios/default.yaml").read_text())["days"] == 28


def flat(sv, key, d=None):
    days = range(len(sv.counts[key])) if d is None else [d]
    return [x for dd in days for x in sv.counts[key][dd]]


def weekday_scen(**kw):
    kw.setdefault("day_factors", {"weekday": 1.0, "jitter": 0.0, "sat": 1.0, "sun": 1.0})
    kw.setdefault("services", {"count": 1, "scale": [1.0], "subset": 1.0, "phase_minutes": [0]})
    return scen(**kw)


def test_dispersion_world_vs_poisson():
    w = mkworld(mean=5.0, var=25.0)
    ratio = {}
    for disp in ("world", "poisson"):
        x = flat(generate(w, weekday_scen(days=4, dispersion=disp), END).services[0], "T1")
        m = sum(x) / len(x)
        ratio[disp] = sum((v - m) ** 2 for v in x) / len(x) / m
    assert 3.5 < ratio["world"] < 6.5 and ratio["poisson"] < 1.3


def test_trend_and_phase():
    s = weekday_scen(days=14, trend={"per_week": 0.5})
    sv = generate(mkworld(), s, END).services[0]
    t = [sum(sv.counts["*total"][d]) for d in range(14)]
    assert abs(t[13] / t[0] / (1 + 0.5 * 13 / 7) - 1) < 0.05
    w = mkworld(n=2)
    for tm in w["services"]["orders"]:
        tm["mean"] = [1.0 + 20 * (h == 10) for h in range(24)]
        tm["var"] = tm["mean"]
    for phase, hour in ((0, 10), (60, 9)):
        sv = generate(w, weekday_scen(days=1, services={"count": 1, "scale": [1.0], "subset": 1.0,
                                                         "phase_minutes": [phase]}), END).services[0]
        tot = flat(sv, "*total")
        assert sum(tot[hour * 60:(hour + 1) * 60]) > 5 * max(sum(tot[:hour * 60]) / hour, 1)


def incident_sum(gen, key, o):
    d0 = minute_at(gen.days[0], 0)
    a = o["start_minute"] - d0
    return flat(gen.services[0], key)[a:o["end_minute"] - d0]


def test_ramp_levelshift_mixswap_shapes():
    s = weekday_scen(days=4, incidents=[{"type": "ramp", "size": 4, "minutes": 120, "every_days": 4, "key": "T0"},
                                        {"type": "level_shift", "size": 1.5, "minutes": 240, "every_days": 2, "key": "T0"}])
    gen = generate(mkworld(), s, END)
    for o in gen.services[0].incidents:
        w = incident_sum(gen, "T0", o)
        n = len(w)
        if o["type"] == "ramp":
            first, last = sum(w[:n // 3]), sum(w[-n // 3:])
            assert abs(last / first / (3.5 / 1.5) - 1) < 0.15
        else:
            assert abs(sum(w) / (1.5 * 20.1 * n) - 1) < 0.06
    s = weekday_scen(days=4, incidents=[{"type": "mix_swap", "minutes": 120, "every_days": 3}])
    gen = generate(mkworld(), s, END)
    (o,) = gen.services[0].incidents
    mean = {t["template_id"]: sum(t["mean"]) / 24 for t in mkworld()["services"]["orders"]}
    for me, other in ((o["meta"]["a"], o["meta"]["b"]), (o["meta"]["b"], o["meta"]["a"])):
        got = sum(incident_sum(gen, me, o))
        assert abs(got / (mean[other] * 120) - 1) < 0.25


def sv_rate(w, minute):
    return w['services']['orders'][0]['mean'][minute // 60]


def test_incidents_avoid_silent_windows_and_gaps():
    w = mkworld()
    for tm in w["services"]["orders"]:
        tm["mean"] = [10.0 if 8 <= h < 12 else 0.0 for h in range(24)]
        tm["var"] = tm["mean"]
    s = weekday_scen(days=8, gaps=[{"day": d, "hour_from": 9, "hour_to": 10} for d in range(8)],
                     incidents=[{"type": t, "size": 3, "minutes": 30, "every_days": 1}
                                for t in ("spike", "mix_swap")] +
                               [{"type": "drop", "size": 0.3, "minutes": 30, "every_days": 1}])
    gen = generate(w, s, END)
    incs = gen.services[0].incidents
    assert len(incs) >= 12
    d0 = minute_at(gen.days[0], 0)
    for o in incs:
        a, b = (o["start_minute"] - d0) % 1440, (o["end_minute"] - d0 - 1) % 1440 + 1
        assert b > 480 and a < 720 and not (a < 600 and b > 540)
        assert sum(sv_rate(w, m) for m in range(a, b)) >= 10


def test_unplaceable_incident_warns_and_is_skipped(capsys):
    w = mkworld(mean=0.01)
    for tm in w["services"]["orders"]:
        tm["mean"] = [0.01] * 24
    gen = generate(w, weekday_scen(days=2, incidents=[{"type": "spike", "size": 3, "minutes": 10, "every_days": 1}]), END)
    assert gen.services[0].incidents == [] and "warning" in capsys.readouterr().err.lower()


def test_bad_incident_specs():
    for bad in ({"type": "spike", "size": 3, "minutes": 10, "every_days": 1, "key": "*error"},
                {"type": "spike", "size": 3, "minutes": 1440, "every_days": 1}):
        with pytest.raises(ValueError):
            generate(mkworld(), weekday_scen(incidents=[bad]), END)


def test_new_template_ids_unique():
    gen = generate(mkworld(), scen(days=3, new_templates={"per_service_day": 12, "error_share": 0.3, "rate": 1.0}), END)
    for sv in gen.services:
        ids = [t["template_id"] for t in sv.templates]
        assert len(ids) == len(set(ids)) == 6 + 36


def test_jitter_shared_by_services():
    s = scen(days=5, day_factors={"weekday": 1.0, "jitter": 0.3, "sat": 1.0, "sun": 1.0},
             services={"count": 2, "scale": [1.0, 1.0], "subset": 1.0, "phase_minutes": [0, 0]})
    gen = generate(mkworld(mean=20.0), s, END)
    a, b = ([sum(sv.counts["*total"][d]) for d in range(5)] for sv in gen.services)
    assert all(abs(x / y - 1) < 0.05 for x, y in zip(a, b))


# --- engine mode (bk12) ----------------------------------------------------------------------------------

TEXTS = ["connection <*> refused by upstream gateway", "payment declined for account <*> reason expired",
         "cache miss on shard <*>", "user <*> logged in from <*> region", "timeout waiting for reply from <*> after retries"]


def engine_world():
    ts = [{"template_id": f"w{i}", "text": TEXTS[i], "level": LEVELS[i % 3], "doc_id": None,
           "mean": [1.5 + i * 0.5] * 24, "var": [2.0] * 24, "mirrored": [False] * 24} for i in range(5)]
    ts[0]["mean"] = [4.0] * 24
    return {"services": {"orders": ts}}


def one_service(**kw):
    return scen(services={"count": 1, "scale": [1.0], "subset": 1.0, "phase_minutes": [0]}, **kw)


@pytest.fixture
def two_schemas(conn):
    names = [f"lab_s_{uuid.uuid4().hex[:8]}" for _ in range(2)]
    yield names
    conn.rollback()
    for n in names:
        conn.execute(f"DROP SCHEMA IF EXISTS {n} CASCADE")
    conn.commit()


def lab_cells(conn, schema, days):
    """(key text-or-name, day, minute) -> count, nonzero cells only; template keys named by their text."""
    text = {r["template_id"]: r["text"] for r in rows(conn, schema, "SELECT template_id, text FROM S.templates")}
    out = {}
    for r in rows(conn, schema, "SELECT key, day, hour, counts FROM S.lab_minute_counts"):
        if r["day"] not in days:
            continue
        for i, c in enumerate(r["counts"]):
            if c:
                out[text.get(r["key"], r["key"]), r["day"], r["hour"] * 60 + i] = c
    return out


def test_engine_mode_matches_counts_mode(conn, two_schemas, monkeypatch):
    """2 history days + 2 engine days: the second engine day starts after a 02:00 commit and a day rollover."""
    from scripts.lab import synth
    import logai2.jobs.commit as commit_mod
    monkeypatch.setattr(synth, "HISTORY_DAYS", 2)
    seen = []
    real = commit_mod.commit

    def spy(c, cfg, today, *args, **kw):
        if not seen:        # the seed commit of run_engine, before the engine's first tick
            seen.append([r["day"] for r in c.execute("SELECT DISTINCT day FROM day_status WHERE committed")])
        return real(c, cfg, today, *args, **kw)
    monkeypatch.setattr(commit_mod, "commit", spy)
    a, b = two_schemas
    s, scale = one_service(days=4), 0.5
    gen = generate(engine_world(), s, END, scale)
    write(conn, a, gen)
    conn.commit()
    t0 = time.time()
    summary = run_engine(engine_world(), s, END, b, TEST_DSN, scale)
    took = time.time() - t0
    eng_days = set(gen.days[2:])
    ca, cb = lab_cells(conn, a, eng_days), lab_cells(conn, b, eng_days)
    keys = set(ca) | set(cb)
    bad = [k for k in keys if ca.get(k, 0) != cb.get(k, 0)]
    print(f"\nENGINE-vs-COUNTS engine days only: cells={len(keys)} mismatches={len(bad)} seconds={took:.0f} summary={summary}")
    assert len(keys) > 10_000 and len(bad) <= len(keys) * 0.001, sorted(bad)[:10]
    assert not [k for k in bad if k[0] == "*total"], sorted(bad)[:10]
    assert summary["events"] == sum(c for k, c in ca.items() if k[0] == "*total")
    assert took < 240
    # only history days were committed before the engine ran; the engine's own commits did the rest
    assert seen[0] and max(seen[0]) <= gen.days[1], seen
    done = {r["day"] for r in rows(conn, b, "SELECT day FROM S.day_status WHERE committed")}
    assert done == set(gen.days[:3]), done           # the last engine day is still open
    assert rows(conn, b, "SELECT 1 FROM S.baseline LIMIT 1")


def test_engine_mode_writes_incidents_and_alerts(conn, two_schemas):
    _, b = two_schemas
    public = lambda: {r["tablename"]: conn.execute(f'SELECT count(*) AS n FROM public."{r["tablename"]}"').fetchone()["n"]
                      for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()}
    before = public()
    s = one_service(days=7, incidents=[{"type": "spike", "size": 8, "minutes": 10, "every_days": 7, "key": "w0"}])
    run_engine(engine_world(), s, END, b, TEST_DSN)
    inc = rows(conn, b, "SELECT * FROM S.lab_incidents")
    assert len(inc) == 1 and inc[0]["type"] == "spike" and inc[0]["data_schema"] == b and inc[0]["source"] == "synthetic"
    alerts = rows(conn, b, "SELECT key, to_state, extract(epoch FROM at)::float8 AS at FROM S.alert_changes")
    assert alerts, "the planted spike must raise alert_changes"
    assert any(inc[0]["start_minute"] * 60 <= a["at"] <= (inc[0]["end_minute"] + 15) * 60 for a in alerts)
    assert public() == before       # public untouched

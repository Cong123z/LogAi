import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from logai2.timeutil import TZ
from scripts.lab.data import minute_at
from scripts.lab.load import load_service
from scripts.lab.synth import generate, write
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
    assert ds == {gen.days[0]: True, gen.days[1]: False}
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
    write(conn, lab_schema, generate(mkworld(), scen(days=2), END))
    conn.commit()
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
        for h in (0, 1):
            conn.execute("INSERT INTO service_hours VALUES ('a', %s, %s, 60)", (d, h))
    # x: hour 0 -> sums 120 and 240 over 120 minutes; hour 1 -> only day 1, 60
    for d, h, s, q in ((d1, 0, 120, 400), (d2, 0, 240, 1000), (d1, 1, 60, 100)):
        conn.execute("INSERT INTO hourly_counts VALUES ('a','x',%s,%s,%s,%s)", (d, h, s, q))
    conn.execute("INSERT INTO hourly_counts VALUES ('a','*total',%s,0,1,1)", (d1,))
    conn.execute("INSERT INTO hourly_counts VALUES ('a','z',%s,0,0,0)", (d1,))   # all zero: dropped
    conn.commit()


def test_world_from_source(conn):
    mk_source(conn)
    w = build_world(conn)
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
    assert x["mean"][12] == pytest.approx((360 / 120 + 60 / 120) / 2) and x["mirrored"][12]   # both missing
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


def test_engine_mode_rejected(capsys):
    from scripts.lab import synth
    with pytest.raises(SystemExit):
        synth.main(["--mode", "engine", "--scenario", "x"])

"""The trust gate: the lab's sigma at k = 3 gives the same alert transitions as the real engine (spec 6.4)."""
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from logai2.config import load_config
from logai2.db import connect
from logai2.engine import Engine
from logai2.timeutil import TZ
from scripts.lab.data import minute_at
from scripts.lab.episodes import Tracker
from scripts.lab.load import load_service
from scripts.lab.methods.sigma import Sigma
from scripts.stress_modules import schema_dsn
from scripts.stress_pipeline import Clock, GenSource, Run, Stream, base_streams, build_world, setup
from tests.conftest import TEST_DSN

DAY0 = date(2026, 10, 23)                       # Friday: Sat 24 and Sun 25 follow; history = Thu, Wed, Sun, Sat, Fri
DAYS = 3
T0 = datetime(2026, 10, 23, 0, 0, tzinfo=TZ).timestamp()
KEY_SERVICE = "svc00"


def at(day: int, hh: int, mm: int = 0) -> int:      # ms of local day DAY0 + day
    return int(datetime(2026, 10, 23 + day, hh, mm, tzinfo=TZ).timestamp() * 1000)


def burst(world, tpl, day, hh, mm, minutes, per_min):
    return Stream(f"b-{tpl.tid}-{day}-{hh}", KEY_SERVICE, at(day, hh, mm), at(day, hh, mm) + minutes * 60_000, per_min, [tpl])


def drive_engine(schema):
    """The real Engine over a synthetic source for DAYS local days with the recorder on. Returns (alert_changes,
    commit instants as sim epoch seconds)."""
    a = SimpleNamespace(dsn=TEST_DSN, services=1, templates=20, eps=1, seed="parity", es_ms=2.5, es_us_per_hit=10.0,
                        poll_interval=15.0, flush_seconds=2.0, storm_templates=0)
    cfg = load_config(None)
    cfg.lab.record = True
    cfg.elasticsearch.index = "stress-*"
    cfg.database.dsn = schema_dsn(TEST_DSN, schema)
    world = build_world(a)
    tpls = world[KEY_SERVICE]
    err0, err1 = tpls[0], tpls[1]                    # the two ERROR templates; err0 is linked to doc:stress by setup()
    t_end = T0 + DAYS * 86400
    # per day: a burst on the doc's template, one on the other ERROR template, a WARN template burst; day 2 has a drop
    extra, cuts = [], []
    for d in range(DAYS):
        extra += [burst(world, err0, d, 9, 10, 8, 14), burst(world, err1, d, 14, 30, 6, 12),
                  burst(world, tpls[3], d, 19, 0, 7, 20)]
    for d in (1, 2):                                          # straddles the 02:00 commit: the tolerance window is used
        extra.append(burst(world, err1, d, 1, 57, 7, 12))
    cuts.append((at(1, 5, 0), at(1, 5, 12), {"rpm": 6}))      # *total far under its band: a Saturday with 2 weekend days is young, so neither side may alert (negative parity)
    span = (int(T0 * 1000), int((t_end + 600) * 1000))
    streams = base_streams(world, a, KEY_SERVICE, *span, cuts) + extra
    setup(a, cfg, world, schema, list(range(24)), err0, T0, t0=T0, day0=DAY0)
    clock = Clock(T0)
    source = GenSource(clock, streams, cfg.elasticsearch.batch_size)
    engine = Engine(cfg, connect(cfg.database.dsn), source, now=clock)
    commits = []
    real = engine._commit

    def spy(*args, **kw):
        commits.append(clock.t)
        return real(*args, **kw)
    engine._commit = spy
    engine.start()
    run = Run(a, engine, clock, source, T0)
    run.wrap()
    try:
        run.until(t_end)
        engine._flush()
    finally:
        for c in (engine.conn, engine.db):
            c.close()
    assert not run.errors and not run.stalled, run.errors
    return commits


def lab_transitions(c):
    """Sigma k=3 through a Tracker over days 2..N, every observed minute, every walked key."""
    days = [DAY0 + timedelta(d) for d in range(DAYS)]
    cfg = load_config(None)
    sd = load_service(c, KEY_SERVICE, days[0], days[-1], 35)
    m, out, minutes = Sigma(), set(), 0
    trackers = {key: Tracker(cfg.alert) for key in sd.keys() if key != "*minutes"}     # one per key: state crosses midnight
    prev = m.fit(sd, days[0], cfg)
    for d in days[1:]:
        model = m.fit(sd, d, cfg)
        for key, tr in trackers.items():
            o = m.day(model, prev, sd, key, d, (3.0,), cfg)[3.0]
            for slot in range(1440):
                if sd.observed[d][slot]:
                    tr.step(minute_at(d, slot), bool(o.high_flag[slot]), o.score[slot])
            minutes += 1
        prev = model
    for key, tr in trackers.items():
        out |= {(key, mn, st) for mn, st in tr.changes}
    return out, minutes, days


def test_lab_sigma_equals_engine_alerts():
    schema = f"parity_{int(time.time() * 1000) % 10**9}"
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            t0 = time.time()
            commits = drive_engine(schema)
            engine_s = time.time() - t0
            with psycopg.connect(schema_dsn(TEST_DSN, schema), row_factory=dict_row) as c:
                lab, n_keys, days = lab_transitions(c)
                changes = c.execute("SELECT key, extract(epoch FROM at)::float8 AS at, to_state FROM alert_changes "
                                    "WHERE service = %s", (KEY_SERVICE,)).fetchall()
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    first = minute_at(days[1], 0)
    eng = {(r["key"], int(r["at"]) // 60 - 1, r["to_state"]) for r in changes if int(r["at"]) // 60 - 1 >= first}
    assert len(commits) == DAYS - 1, commits                                       # 02:00 of days 2 and 3
    window = [(int(t) // 60 - 2, int(t) // 60 + 10) for t in commits]            # minutes: [commit - 2, commit + 10]
    inside = lambda m: any(lo <= m <= hi for lo, hi in window)                   # noqa: E731
    diff = lab ^ eng
    outside = sorted(x for x in diff if not inside(x[1]))
    in_win = [x for x in diff if inside(x[1])]
    print(f"\nPARITY engine_s={engine_s:.0f} keys*days={n_keys} lab={len(lab)} engine={len(eng)} "
          f"matched={len(lab & eng)} in_commit_windows={sum(inside(x[1]) for x in lab | eng)} "
          f"mismatch_in_windows={len(in_win)} mismatch_outside={len(outside)} commits={len(commits)}")
    assert len(eng) > 20, "the world must produce alerts, or the comparison proves nothing"
    assert {"doc:stress", "*error"} <= {k for k, _, _ in eng}, "doc and *error must alert"
    assert {"ALERTING", "COOLING", "NORMAL", "WARMING"} <= {st for _, _, st in eng}
    assert not outside, outside[:20]
    for lo, hi in window:                                                           # at most 10 per commit window
        assert sum(lo <= x[1] <= hi for x in in_win) <= 10, (lo, hi)

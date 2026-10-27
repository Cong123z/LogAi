"""Synthetic world, `counts` mode: write what the engine would have recorded for a made-up multi-week history,
from the world file's hourly shapes, into its own schema. Pure `generate`, then `write`.
`engine` mode: the same `generate` result, sent as log lines through the real Engine on an injected clock."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
import zlib
from array import array
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import exp
from pathlib import Path
from types import SimpleNamespace

import yaml

from logai2.config import load_config
from logai2.db import connect, ensure_partitions, migrate
from logai2.links import level_class
from logai2.timeutil import TZ
from scripts.lab.data import minute_at
from scripts.stress_pipeline import mix

MIN_DAY = 1440
DERIVED = ("*total", "*error", "*warn", "*info")


@dataclass
class Svc:
    name: str
    templates: list[dict]                       # template_id, text, level, doc_id, first_seen (minute)
    counts: dict[str, dict[int, array]]         # key (templates, derived, '*minutes') -> day index -> 1440 ints
    incidents: list[dict] = field(default_factory=list)


@dataclass
class Generated:
    days: list[date]
    services: list[Svc]

    def digest(self) -> str:
        h = hashlib.sha256()
        for s in self.services:
            h.update(repr((s.name, s.templates, s.incidents)).encode())
            for k in sorted(s.counts):
                h.update(k.encode())
                for d in sorted(s.counts[k]):
                    h.update(s.counts[k][d].tobytes())
        return h.hexdigest()


def poisson(rng: random.Random, lam: float) -> int:
    if lam < 1e-9:
        return 0
    if lam >= 30:   # exact: a sum of independent Poissons
        return poisson(rng, lam / 2) + poisson(rng, lam - lam / 2)
    u, p = rng.random(), exp(-lam)
    c, n = p, 0
    while u > c and n < 200:
        n += 1
        p *= lam / n
        c += p
    return n


def draw(rng: random.Random, lam: float, nb: float) -> int:
    """Poisson, or gamma-Poisson with variance/mean = 1 + nb (nb = 0: plain Poisson)."""
    if nb > 0 and lam > 0:
        lam = rng.gammavariate(lam / nb, nb)
    return poisson(rng, lam)


PLACE_TRIES = 50


def _place_incidents(rng, name, tpl, specs, days, d_start, expected, gap_minutes):
    """expected(template or None for the service total, d, lo, hi) = expected count in minutes [lo, hi) of day d."""
    ids = {t["template_id"] for t in tpl}
    busy = sorted(tpl, key=lambda t: -sum(t["mean"]))
    busy = busy[:max(1, len(busy) // 5)]
    out: list[dict] = []
    for sp in specs:
        every, length, typ = int(sp["every_days"]), int(sp["minutes"]), sp["type"]
        if not 1 <= length < MIN_DAY:
            raise ValueError(f"incident minutes must be 1..1439: {sp}")
        if sp.get("key") is not None and typ in ("spike", "ramp", "level_shift") and sp["key"] not in ids:
            raise ValueError(f"incident key {sp['key']!r} is not a template of the service: {sp}")
        if sp.get("key") is not None and typ == "drop" and sp["key"] != "*total":
            raise ValueError(f"drop works on *total only: {sp}")
        for j in range(1, days // every + 1):
            d = j * every - 1
            for _ in range(PLACE_TRIES):
                lo = rng.randrange(MIN_DAY - length)
                start = d_start[d] + lo
                if any(m in gap_minutes[d] for m in range(lo, lo + length)):
                    continue
                if any(start < o["end_minute"] and o["start_minute"] < start + length for o in out):
                    continue
                key, meta = sp.get("key"), None
                if typ == "drop":
                    key, targets, floor = "*total", [None], 50
                elif typ == "mix_swap":
                    a, b = rng.sample([t["template_id"] for t in tpl], 2)
                    key, meta, targets, floor = None, {"a": a, "b": b}, [info for info in tpl if info["template_id"] in (a, b)], 10
                else:
                    key = key or rng.choice(busy)["template_id"]
                    targets, floor = [t for t in tpl if t["template_id"] == key], 10
                if all(expected(t, d, lo, lo + length) >= floor for t in targets):
                    out.append({"service": name, "type": typ, "key": key, "size": sp.get("size"), "meta": meta,
                                "start_minute": start, "end_minute": start + length})
                    break
            else:
                print(f"warning: {name}: no window for {typ} on day {d} after {PLACE_TRIES} tries; skipped",
                      file=sys.stderr)
    return sorted(out, key=lambda o: o["start_minute"])


def generate(world: dict, scenario: dict, end: date, lam_scale: float = 1.0) -> Generated:
    """`lam_scale` multiplies every lambda (the --scale of engine mode); 1.0 changes nothing."""
    sc, rng = scenario, random.Random(scenario["seed"])
    n_days = sc["days"]
    days = [end - timedelta(n_days - 1 - i) for i in range(n_days)]
    d_start = [minute_at(d, 0) for d in days]
    window_start = d_start[0]
    nbw = sc["dispersion"] == "world"
    sv_cfg, df, hol, nt = sc["services"], sc["day_factors"], sc["holiday"], sc["new_templates"]
    src_names = sorted(world["services"])
    jitter = [rng.uniform(-df["jitter"], df["jitter"]) for _ in days]      # one per day, shared by the services
    facs = []
    for d, day in enumerate(days):
        dayf = df["weekday"] * (1 + jitter[d]) if day.weekday() < 5 else df["sat"] if day.weekday() == 5 else df["sun"]
        facs.append(dayf * (hol["factor"] if hol["start_day"] <= d < hol["start_day"] + hol["length"] else 1.0))
    gap_minutes = [{i for g in sc["gaps"] if g["day"] == d for i in range(g["hour_from"] * 60, g["hour_to"] * 60)}
                   for d in range(n_days)]
    out = []
    for si in range(sv_cfg["count"]):
        name = f"synth{si + 1}"
        base = world["services"][src_names[si % len(src_names)]]
        keep = sorted(rng.sample(range(len(base)), max(1, round(len(base) * sv_cfg["subset"]))))
        tpl = [base[i] for i in keep]
        scale, phase = sv_cfg["scale"][si % len(sv_cfg["scale"])] * lam_scale, sv_cfg["phase_minutes"][si % len(sv_cfg["phase_minutes"])]
        hour_of = [((i + phase) % MIN_DAY) // 60 for i in range(MIN_DAY)]
        def expected(t, d, lo, hi, scale=scale, hour_of=hour_of):
            k = scale * facs[d] * (1 + sc["trend"]["per_week"] * d / 7)
            return k * sum(sum(x["mean"][hour_of[i]] for i in range(lo, hi)) if t is None or x is t else 0
                           for x in (tpl if t is None else [t]))
        incs = _place_incidents(rng, name, tpl, sc["incidents"], n_days, d_start, expected, gap_minutes)
        info = {t["template_id"]: t for t in tpl}
        # rate table: template -> (mean per hour, extra variance ratio per hour)
        rates = {t["template_id"]: (t["mean"], [max(v / m - 1, 0.0) if nbw and m > 0 else 0.0
                                                for m, v in zip(t["mean"], t["var"])]) for t in tpl}
        templates = [{"template_id": t["template_id"], "text": t["text"], "level": t["level"],
                      "doc_id": t["doc_id"], "first_seen": window_start - 30 * MIN_DAY} for t in tpl]
        counts: dict[str, dict[int, array]] = {k: {} for k in [*info, *DERIVED, "*minutes"]}
        born: dict[str, int] = {}
        for d, day in enumerate(days):
            d0 = d_start[d]
            fac = facs[d]
            for n in range(nt["per_service_day"]):
                tid = f"{name}:N{d:02d}{n:02d}"
                words = rng.choice(base)["text"].split()
                at = rng.choice([i for i, w in enumerate(words) if not w.startswith("<")] or range(len(words)))
                words[at] = f"x{rng.randrange(10 ** 6)}"
                born[tid] = d0 + rng.randrange(MIN_DAY)
                templates.append({"template_id": tid, "text": " ".join(words), "doc_id": None, "first_seen": born[tid],
                                  "level": "ERROR" if rng.random() < nt["error_share"] else "INFO"})
                counts[tid] = {dd: array("i", [0]) * MIN_DAY for dd in range(d)}
            obs = array("i", [1]) * MIN_DAY
            for i in gap_minutes[d]:
                obs[i] = 0
            counts["*minutes"][d] = obs
            tot, cls = array("i", [0]) * MIN_DAY, {c: array("i", [0]) * MIN_DAY for c in DERIVED[1:]}
            today = [o for o in incs if o["end_minute"] > d0 and o["start_minute"] < d0 + MIN_DAY]
            trend = 1 + sc["trend"]["per_week"] * d / 7
            for t in templates:
                tid = t["template_id"]
                is_new = tid in born
                mult, swap = {}, {}
                for o in today:
                    lo, hi = max(o["start_minute"], d0) - d0, min(o["end_minute"], d0 + MIN_DAY) - d0
                    ty = o["type"]
                    if ty == "drop":
                        f = lambda i: o["size"]
                    elif ty == "mix_swap":
                        if tid in (o["meta"]["a"], o["meta"]["b"]):
                            other = o["meta"]["b"] if tid == o["meta"]["a"] else o["meta"]["a"]
                            for i in range(lo, hi):
                                swap[i] = other
                        continue
                    elif o["key"] != tid:
                        continue
                    elif ty == "ramp":
                        f = lambda i, o=o: 1 + (o["size"] - 1) * (d0 + i - o["start_minute"] + 1) / (o["end_minute"] - o["start_minute"])
                    else:
                        f = lambda i, o=o: o["size"]
                    for i in range(lo, hi):
                        mult[i] = mult.get(i, 1.0) * f(i)
                arr = array("i", [0]) * MIN_DAY
                first = max(born[tid] - d0, 0) if is_new else 0
                if is_new:
                    lam_h = [nt["rate"] * lam_scale * fac] * 24
                    nbh = [0.0] * 24
                else:
                    mean, nbh = rates[tid]
                    k = scale * fac * trend
                    lam_h = [m * k for m in mean]
                cls_arr = cls["*" + level_class(t["level"]).lower()]
                for i in range(first, MIN_DAY):
                    if not obs[i]:
                        continue
                    h = hour_of[i]
                    if swap and i in swap:
                        mean_o, nb_o = rates[swap[i]]
                        lam, nb = mean_o[h] * scale * fac * trend, nb_o[h]
                    else:
                        lam, nb = lam_h[h], nbh[h]
                    if mult:
                        lam *= mult.get(i, 1.0)
                    c = draw(rng, lam, nb) if lam > 0 else 0
                    if c:
                        arr[i] = c
                        tot[i] += c
                        cls_arr[i] += c
                counts[tid][d] = arr
            counts["*total"][d] = tot
            for c, a in cls.items():
                counts[c][d] = a
        out.append(Svc(name, templates, counts, incs))
    return Generated(days, out)


# --- writing ---------------------------------------------------------------------------------------------

def write(conn, schema: str, gen: Generated) -> None:
    """Drop and recreate `schema`, migrate it, load `gen`. Nothing outside the schema is touched."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema) or schema == "public":
        raise ValueError(f"bad scenario schema name: {schema}")
    old = conn.execute("SHOW search_path").fetchone()["search_path"]
    conn.rollback()
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
        conn.execute(f"SET search_path TO {schema}, {old}")    # the caller's path stays behind (pgvector lives in public)
        conn.commit()
        migrate(conn)
        ensure_partitions(conn, gen.days[0], gen.days[-1])
        end_ts = lambda m: datetime.fromtimestamp(m * 60, TZ)
        window_end = minute_at(gen.days[-1], 0) + MIN_DAY
        with conn.transaction(), conn.cursor() as cur:
            def copy(table, rows):
                with cur.copy(f"COPY {table} FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
            docs = {t["doc_id"] for s in gen.services for t in s.templates if t["doc_id"]}
            for d in sorted(docs):
                cur.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES (%s, %s, '', 'synthetic') "
                            "ON CONFLICT DO NOTHING", (d, d))
            copy("services (service, partition)", [(s.name, i) for i, s in enumerate(gen.services)])
            copy("templates (service, template_id, level, text, first_seen, last_seen)",
                 [(s.name, t["template_id"], t["level"], t["text"], end_ts(t["first_seen"]), end_ts(window_end))
                  for s in gen.services for t in s.templates])
            copy("template_doc_links (service, template_id, doc_id, source)",
                 [(s.name, t["template_id"], t["doc_id"], "manual") for s in gen.services for t in s.templates if t["doc_id"]])
            copy("day_status (service, day, committed, daytype)",
                 [(s.name, day, True, "weekend" if day.weekday() >= 5 else "weekday")
                  for s in gen.services for day in gen.days[:-1]])     # the last day is still open, as in the engine
            lab, hourly, hours = [], [], []
            for s in gen.services:
                for key, per_day in s.counts.items():
                    for d, a in per_day.items():
                        for h in range(24):
                            c = a[h * 60:(h + 1) * 60]
                            if not any(c):
                                continue
                            c = c.tolist()
                            lab.append((s.name, key, gen.days[d], h, c))
                            if key != "*minutes":
                                hourly.append((s.name, key, gen.days[d], h, sum(c), sum(x * x for x in c)))
                            else:
                                hours.append((s.name, gen.days[d], h, sum(c)))
            copy("lab_minute_counts (service, key, day, hour, counts)", lab)
            copy("hourly_counts (service, key, day, hour, sum, sumsq)", hourly)
            copy("service_hours (service, day, hour, n_minutes)", hours)
            for s in gen.services:
                for o in s.incidents:
                    cur.execute("INSERT INTO lab_incidents (source, data_schema, service, key, type, size, "
                                "start_minute, end_minute, meta) VALUES ('synthetic', %s, %s, %s, %s, %s, %s, %s, %s)",
                                (schema, s.name, o["key"], o["type"], o["size"], o["start_minute"], o["end_minute"],
                                 json.dumps(o["meta"]) if o["meta"] else None))
        conn.commit()
    finally:
        conn.rollback()
        conn.execute(f"SET search_path TO {old}")
        conn.commit()


# --- engine mode -----------------------------------------------------------------------------------------

HISTORY_DAYS = 7        # days before the engine's first day that are seeded from the counts (sigma needs a baseline)
RENDER_SEED = 0x5EED


def render(text: str, h: int) -> str:
    """The template's `<*>` wildcards filled with seeded numbers (the normalizer turns them back into `<*>`)."""
    return "".join(p + (str(mix(h ^ i) % 10 ** 8) if i < text.count("<*>") else "")
                   for i, p in enumerate(text.split("<*>")))


class Events:
    """Sorted events of one service, shaped like stress_pipeline.Stream for GenSource: event k at ts[k] (ms) is
    template tpl[k] of `templates` [(level, text, module)]; its tokens come from mix(seed ^ k)."""

    def __init__(self, service: str, ts: array, tpl: array, templates: list[tuple]):
        self.sid, self.service, self.ts, self.tpl, self.templates = f"ev-{service}", service, ts, tpl, templates
        self.a_ms, self.b_ms = (ts[0], ts[-1] + 1) if ts else (0, 0)
        self.delay_ms, self.index = 0, f"stress-{service}"
        self.seed = mix(RENDER_SEED ^ zlib.crc32(service.encode()))

    def count(self, a_ms: int, b_ms: int) -> int:
        return bisect_left(self.ts, b_ms) - bisect_left(self.ts, a_ms)

    def hits(self, from_ms: int, upper_ms: int, limit: int, now_ms: int) -> list[dict]:
        hi, out, k = min(upper_ms, now_ms), [], bisect_left(self.ts, from_ms)
        while k < len(self.ts) and len(out) < limit and self.ts[k] <= hi:
            ts, h = self.ts[k], mix(self.seed ^ k)
            level, text, module = self.templates[self.tpl[k]]
            when = datetime.fromtimestamp(ts // 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")
            out.append({"_id": f"{self.sid}-{k}", "sort": [ts, k], "_source": {
                "@timestamp": ts, "service_code2": self.service, "moduleCode": module,
                "message": f"{when},{ts % 1000:03d} {level} [main] {render(text, h)}"}})
            k += 1
        return out


def _pretrain(cfg, gen: Generated) -> tuple[dict, dict, dict]:
    """Every template through Drain3 once, as stress_pipeline.pretrain does: (blobs, {service: {world id: engine
    id}}, {service: {engine id: text}})."""
    from logai2.owner.miners import Miners
    from logai2.reader.normalize import normalize
    miners = Miners(cfg.drain3, {})
    line = lambda t: normalize(f"2026-10-21 10:00:00,000 {t['level']} [main] " + render(t["text"], RENDER_SEED))
    ids, texts = {}, {}
    for _ in range(2):      # the second pass reads the final text of each cluster (a later template may widen it)
        for s in gen.services:
            ids[s.name], texts[s.name] = {}, {}
            for t in s.templates:
                tid, text, _new = miners.match(s.name, line(t))
                ids[s.name][t["template_id"]], texts[s.name][tid] = tid, text
            if len(set(ids[s.name].values())) < len(s.templates):
                raise ValueError(f"{s.name}: templates merged in Drain3 (ids {ids[s.name]}); engine mode needs "
                                 "templates the miner keeps apart")
    return miners.dirty(), ids, texts


def _rename(gen: Generated, ids: dict, texts: dict, keep_days: int) -> Generated:
    """`gen` with the engine's template ids and texts and only counts of days below keep_days."""
    out = []
    for s in gen.services:
        m = ids[s.name]
        tpl = [{**t, "template_id": m[t["template_id"]], "text": texts[s.name][m[t["template_id"]]]} for t in s.templates]
        counts = {m.get(k, k): {d: a for d, a in pd.items() if d < keep_days} for k, pd in s.counts.items()}
        inc = [{**o, "key": m.get(o["key"], o["key"]), "meta": {k: m.get(v, v) for k, v in o["meta"].items()} if o["meta"] else None}
               for o in s.incidents]
        out.append(Svc(s.name, tpl, counts, inc))
    return Generated(gen.days, out)


def _events(gen: Generated, first_day: int, seed: int) -> list[Events]:
    # ponytail: every event of the run is held in memory (12 bytes each); chunk per day if --scale 1.0 runs get too big
    rng, out = random.Random(f"{seed}-engine"), []
    for s in gen.services:
        tpl = [(t["level"], t["text"], f"mod{i % 3}") for i, t in enumerate(s.templates)]
        ev = []
        for i, t in enumerate(s.templates):
            for d in range(first_day, len(gen.days)):
                for slot, c in enumerate(s.counts[t["template_id"]][d]):
                    base = (minute_at(gen.days[d], 0) + slot) * 60_000
                    ev += [(base + rng.randrange(60_000), i) for _ in range(c)]
        ev.sort()
        out.append(Events(s.name, array("q", [e[0] for e in ev]), array("i", [e[1] for e in ev]), tpl))
    return out


def run_engine(world: dict, scenario: dict, end: date, schema: str, dsn: str, scale: float = 1.0) -> dict:
    """The real Engine over the generated lines for the last days of the scenario (the earlier ones, up to
    HISTORY_DAYS, are written from the counts as history). Gaps are not unobserved here: the engine closes
    every minute, so a gap is only silent."""
    import psycopg
    from psycopg.rows import dict_row
    from logai2.db import put_setting
    from logai2.engine import Engine
    from logai2.jobs.commit import commit
    from scripts.stress_modules import schema_dsn
    from scripts.stress_pipeline import Clock, GenSource, Run
    t_start = time.time()
    gen = generate(world, scenario, end, scale)
    hist = min(HISTORY_DAYS, len(gen.days) - 1)
    cfg = load_config(None)
    cfg.lab.record, cfg.elasticsearch.index, cfg.database.dsn = True, "stress-*", schema_dsn(dsn, schema)
    blobs, ids, texts = _pretrain(cfg, gen)
    # the engine's ids and texts, with what the counts say for the history days (lab_incidents for all days)
    admin = connect(dsn)
    try:
        write(admin, schema, _rename(gen, ids, texts, hist))
    finally:
        admin.close()
    events = _events(_rename(gen, ids, texts, len(gen.days)), hist, scenario["seed"])
    day0 = gen.days[hist]
    t0 = minute_at(day0, 0) * 60
    t_end = t0 + (len(gen.days) - hist) * 86400
    with psycopg.connect(cfg.database.dsn, autocommit=False, row_factory=dict_row) as c:
        ensure_partitions(c, gen.days[0], gen.days[-1] + timedelta(days=2))
        with c.transaction(), c.cursor() as cur:
            cur.executemany("INSERT INTO drain3_state (service, blob) VALUES (%s, %s)", list(blobs.items()))
            cur.executemany("INSERT INTO modules (module_code, service) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            [(m, e.service) for e in events for _, _, m in e.templates])
        commit(c, cfg, day0)
        put_setting(c, "detection_enabled", True)   # as the engine does after its first catch-up
        put_setting(c, "bootstrap", {"since": t0, "until": t0})
    a = SimpleNamespace(dsn=dsn, es_ms=2.5, es_us_per_hit=10.0, poll_interval=15.0)
    clock = Clock(t0)
    source = GenSource(clock, events, cfg.elasticsearch.batch_size)
    engine = Engine(cfg, connect(cfg.database.dsn), source, now=clock)
    engine.start()
    run = Run(a, engine, clock, source, t0)
    run.wrap()
    try:
        run.until(t_end + 600)
        engine._flush()
    finally:
        for c in (engine.conn, engine.db):
            c.close()
    if run.errors or run.stalled:
        raise RuntimeError(f"engine run failed: {run.errors} stalled={run.stalled}")
    with connect(cfg.database.dsn) as c:
        changes = c.execute("SELECT count(*) AS n FROM alert_changes").fetchone()["n"]
    return {"schema": schema, "scale": scale, "history_days": hist, "minutes": (len(gen.days) - hist) * MIN_DAY,
            "events": sum(len(e.ts) for e in events), "seconds": round(time.time() - t_start, 1),
            "alert_changes": changes}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", required=True, choices=["counts", "engine"])
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--world", default="lab/world.json")
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    ap.add_argument("--end", type=date.fromisoformat, default=None)
    ap.add_argument("--scale", type=float, default=1.0, help="engine mode: multiplies every lambda")
    a = ap.parse_args(argv)
    t0 = time.time()
    scenario = yaml.safe_load(Path(a.scenario).read_text())
    schema = a.schema or f"lab_s_{Path(a.scenario).stem}" + ("_engine" if a.mode == "engine" else "")
    end = a.end or datetime.now(TZ).date() - timedelta(days=1)
    world = json.loads(Path(a.world).read_text())
    if a.mode == "engine":
        print(run_engine(world, scenario, end, schema, a.dsn or load_config(None).database.dsn, a.scale))
        return
    gen = generate(world, scenario, end)
    t1 = time.time()
    conn = connect(a.dsn or load_config(None).database.dsn)
    write(conn, schema, gen)
    conn.execute(f"SET search_path TO {schema}, public")
    n = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in (
        "lab_minute_counts", "hourly_counts", "service_hours", "day_status", "templates", "template_doc_links",
        "lab_incidents")}
    conn.rollback()
    print(f"{schema}: generated {t1 - t0:.0f}s, written {time.time() - t1:.0f}s, total {time.time() - t0:.0f}s; "
          + ", ".join(f"{k} {v}" for k, v in n.items()))


if __name__ == "__main__":
    main()

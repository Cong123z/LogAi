"""Synthetic world, `counts` mode: write what the engine would have recorded for a made-up multi-week history,
from the world file's hourly shapes, into its own schema. Pure `generate`, then `write`."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
from array import array
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import exp
from pathlib import Path

import yaml

from logai2.config import load_config
from logai2.db import MIGRATIONS_DIR, connect, ensure_partitions, migrate
from logai2.links import level_class
from logai2.timeutil import TZ
from scripts.lab.data import minute_at

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
    if lam >= 30:   # ponytail: normal approximation above 30 (error well under 1 %); exact sampler if it matters
        return max(0, int(rng.gauss(lam, lam ** 0.5) + 0.5))
    u, p = rng.random(), exp(-lam)
    c, n = p, 0
    while u > c and n < 200:
        n += 1
        p *= lam / n
        c += p
    return n


def draw(rng: random.Random, lam: float, nb: float) -> int:
    """Poisson, or gamma-Poisson with variance/mean = 1 + nb (nb = 0: plain Poisson)."""
    if nb > 0 and lam >= 0.05:   # below that the overdispersion is invisible
        lam = rng.gammavariate(lam / nb, nb)
    return poisson(rng, lam)


def _place_incidents(rng, sv_name, tpl, specs, days, d_start):
    busy = sorted(tpl, key=lambda t: -sum(t["mean"]))
    busy = busy[:max(1, len(busy) // 5)]
    out: list[dict] = []
    for sp in specs:
        every, length = int(sp["every_days"]), int(sp["minutes"])
        for j in range(1, days // every + 1):
            for _ in range(20):
                start = d_start + (j * every - 1) * MIN_DAY + rng.randrange(MIN_DAY - length)
                if all(start + length <= o["start_minute"] or o["end_minute"] <= start for o in out):
                    break
            else:
                continue
            typ, meta, key = sp["type"], None, sp.get("key")
            if typ == "drop":
                key = "*total"
            elif typ == "mix_swap":
                a, b = rng.sample([t["template_id"] for t in tpl], 2)
                key, meta = None, {"a": a, "b": b}
            elif key not in {t["template_id"] for t in tpl}:
                key = rng.choice(busy)["template_id"]
            out.append({"service": sv_name, "type": typ, "key": key, "size": sp.get("size"), "meta": meta,
                        "start_minute": start, "end_minute": start + length})
    return sorted(out, key=lambda o: o["start_minute"])


def generate(world: dict, scenario: dict, end: date) -> Generated:
    sc, rng = scenario, random.Random(scenario["seed"])
    n_days = sc["days"]
    days = [end - timedelta(n_days - 1 - i) for i in range(n_days)]
    d_start = [minute_at(d, 0) for d in days]
    window_start = d_start[0]
    nbw = sc["dispersion"] == "world"
    sv_cfg, df, hol, nt = sc["services"], sc["day_factors"], sc["holiday"], sc["new_templates"]
    src_names = sorted(world["services"])
    out = []
    for si in range(sv_cfg["count"]):
        name = f"synth{si + 1}"
        base = world["services"][src_names[si % len(src_names)]]
        keep = sorted(rng.sample(range(len(base)), max(1, round(len(base) * sv_cfg["subset"]))))
        tpl = [base[i] for i in keep]
        scale, phase = sv_cfg["scale"][si % len(sv_cfg["scale"])], sv_cfg["phase_minutes"][si % len(sv_cfg["phase_minutes"])]
        hour_of = [((i + phase) % MIN_DAY) // 60 for i in range(MIN_DAY)]
        incs = _place_incidents(rng, name, tpl, sc["incidents"], n_days, window_start)
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
            if day.weekday() < 5:
                dayf = df["weekday"] * (1 + rng.uniform(-df["jitter"], df["jitter"]))
            else:
                dayf = df["sat"] if day.weekday() == 5 else df["sun"]
            holf = hol["factor"] if hol["start_day"] <= d < hol["start_day"] + hol["length"] else 1.0
            fac = dayf * holf
            for n in range(nt["per_service_day"]):
                tid = f"{name}:N{d}{n}"
                words = rng.choice(base)["text"].split()
                at = rng.choice([i for i, w in enumerate(words) if not w.startswith("<")] or range(len(words)))
                words[at] = f"x{rng.randrange(10 ** 6)}"
                born[tid] = d0 + rng.randrange(MIN_DAY)
                templates.append({"template_id": tid, "text": " ".join(words), "doc_id": None, "first_seen": born[tid],
                                  "level": "ERROR" if rng.random() < nt["error_share"] else "INFO"})
                counts[tid] = {dd: array("i", [0]) * MIN_DAY for dd in range(d)}
            obs = array("i", [1]) * MIN_DAY
            for g in sc["gaps"]:
                if g["day"] == d:
                    for i in range(g["hour_from"] * 60, g["hour_to"] * 60):
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
                    lam_h = [nt["rate"] * fac] * 24
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
        conn.execute(f"SET search_path TO {schema}, public")    # public: pgvector
        conn.commit()
        # migrate() looks schema_migrations up on the whole path: with a migrated `public` it would skip everything
        conn.execute((MIGRATIONS_DIR / "001_init.sql").read_text())
        conn.execute("INSERT INTO schema_migrations (version) VALUES (1)")
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
                 [(s.name, day, day != gen.days[-1], "weekend" if day.weekday() >= 5 else "weekday")
                  for s in gen.services for day in gen.days])
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


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", required=True, choices=["counts", "engine"])
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--world", default="lab/world.json")
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    ap.add_argument("--end", type=date.fromisoformat, default=None)
    a = ap.parse_args(argv)
    if a.mode == "engine":
        ap.error("--mode engine is not implemented yet (task bk12); use --mode counts")
    t0 = time.time()
    scenario = yaml.safe_load(Path(a.scenario).read_text())
    schema = a.schema or f"lab_s_{Path(a.scenario).stem}"
    end = a.end or datetime.now(TZ).date() - timedelta(days=1)
    gen = generate(json.loads(Path(a.world).read_text()), scenario, end)
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

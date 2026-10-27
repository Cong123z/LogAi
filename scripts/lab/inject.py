"""Test A injections (judging criterion 3, spec 8): plant incidents into an in-memory copy of one service's counts.

Every change is made on a template's counts; `*total`, the template's level key and every `doc:` key that contains it get
the same delta on the same minutes (`*mix` shares `*total`'s arrays), so `*total` = sum of templates keeps holding. Arrays an
injection does not touch are shared with the clean data. Injections go only into the test part, never within NEAR minutes
of a planted incident, and at least GAP minutes apart per key. Fully seeded.
"""
from __future__ import annotations

import random
from array import array
from dataclasses import dataclass, replace

from psycopg.types.json import Jsonb

from logai2.links import level_class
from logai2.timeutil import daytype
from scripts.lab.data import UNOBSERVED, Incident, ServiceData, minute_at, slot_of
from scripts.lab.methods.sigma import rebuild_stats
from scripts.lab.score import LEVEL_KEY

PASSES = 5
NEAR, GAP, HIT = 15, 180, 15       # minutes: planted-incident margin, spacing per key, hit window after the start
ATTEMPTS = 60
SIZES = ("small", "medium", "large")
MIX = "*mix"
# type -> (target kinds, factor per size, minutes, minimum expected count over the window)
TYPES = {
    "spike": ({"M2", "M3", "M4", "M5"}, (2, 3, 5), 10, 10),
    "drop": ({"M1"}, (0.5, 0.3, 0.1), 30, 50),
    "ramp": ({"M1", "M2", "M3", "M4"}, (1.5, 2, 3), 60, 10),
    "level_shift": ({"M1", "M2", "M3", "M4"}, (1.3, 1.5, 2), 240, 10),
    "burst": ({"M6"}, (5, 20, 50), 15, 0),
    "mix_swap": ({"M7"}, (2, 4, 8), 60, 0),
}


@dataclass(frozen=True)
class Injection:
    type: str
    size_name: str
    size: float
    key: str                  # the key the incident is on (what a hit is looked for on)
    kind: str                 # the monitor kind of `key`
    start: int
    end: int                  # exclusive
    carrier: str = ""         # the template whose counts change (`key` itself for a template; "" = all, for a drop)
    add: tuple = ()           # per-minute counts added to `carrier` (spike, ramp, level_shift, burst)
    pair: tuple = ()          # mix_swap: the two templates whose counts are exchanged


class Expect:
    """σ's profile mean per minute (rebuild_stats of the clean data); keys it has no stat for use the mean of the same
    daytype and hour over the window's own clean days. A doc key is the sum of its templates."""

    def __init__(self, sd: ServiceData):
        self.sd = sd
        self.stats = rebuild_stats(sd, sd.days[-1]).stats
        self._memo: dict = {}

    def mean(self, key, day, hour) -> float:
        sd = self.sd
        if key.startswith("doc:"):
            return sum(self.mean(t, day, hour) for t in sd.docs.get(key, []))
        dt = daytype(day)
        for d in (dt, "weekday"):
            s = self.stats.get((sd.service, key, d, hour))
            if s is not None:
                return s.mean
        if (key, dt, hour) not in self._memo:
            tot = n = 0
            for d, a in sd.minutes.get(key, {}).items():
                if daytype(d) == dt:
                    seg = [x for x in a[hour * 60:hour * 60 + 60] if x != UNOBSERVED]
                    tot, n = tot + sum(seg), n + len(seg)
            self._memo[key, dt, hour] = tot / n if n else 0.0
        return self._memo[key, dt, hour]

    def window(self, key, start, n) -> list[float]:
        out = []
        for m in range(start, start + n):
            day, slot = slot_of(m)
            out.append(self.mean(key, day, slot // 60))
        return out


def _near(a, b, gap) -> bool:
    return a[0] < b[1] + gap and b[0] < a[1] + gap


def plan(sd: ServiceData, kinds: dict, part, seed: int, exp: Expect) -> list[Injection]:
    """One pass: about PER_TYPE (= one per size) injections of every type. A candidate that breaks a rule is redrawn."""
    rng = random.Random(seed)
    lo, hi = minute_at(sd.days[0], 0), minute_at(sd.days[-1], 0) + 1440
    tmpl = sorted(k for k in sd.minutes if not k.startswith(("*", "doc:")))
    planted = [(i.start - NEAR, i.end + NEAR) for i in sd.incidents if i.source in ("synthetic", "test-c")]
    busy: dict[str, list] = {}
    out: list[Injection] = []
    errors = [t for t in tmpl if LEVEL_KEY[level_class(sd.levels.get(t))] == "*error"]
    by_kind = {typ: sorted(k for k, kd in kinds.items() if kd in tk and not k.startswith("inj:"))
               for typ, (tk, _, _, _) in TYPES.items()}

    def carrier_of(key, start, dur):
        pool = {"*total": tmpl, "*error": errors}.get(key) or sd.docs.get(key) or [key]
        pool = [t for t in pool if t in sd.minutes]
        return max(pool, key=lambda t: (sum(exp.window(t, start, dur)), t), default="")

    def draw(typ, name, size):
        _, _, dur, min_exp = TYPES[typ]
        start = rng.randrange(lo, hi - dur)
        end = start + dur
        if any(start < b and a < end for a, b in planted):
            return None
        if typ == "burst":
            key = f"inj:{seed}:{len(out)}:{rng.randrange(10 ** 6)}"      # redrawn with the attempt: a by-keys split may put a name in tune
            if part(key, slot_of(start)[0]) != "test" or part(key, slot_of(end - 1)[0]) != "test":
                return None
            return Injection(typ, name, size, key, "M6", start, end, key, add=(size,) * dur), (key,)
        if typ == "mix_swap":
            pair = _pair(start, dur, size)
            if not pair or part(MIX, slot_of(start)[0]) != "test" or part(MIX, slot_of(end - 1)[0]) != "test":
                return None
            return Injection(typ, name, size, MIX, "M7", start, end, pair=pair), (MIX, *pair)
        if not by_kind[typ]:
            return None
        key = rng.choice(by_kind[typ])
        if part(key, slot_of(start)[0]) != "test" or part(key, slot_of(end - 1)[0]) != "test":
            return None
        e = exp.window(key, start, dur)
        if sum(e) < min_exp:
            return None
        if typ == "drop":
            return Injection(typ, name, size, key, kinds[key], start, end), (key,)
        carrier = carrier_of(key, start, dur)
        if not carrier:
            return None
        if typ == "ramp":
            add = tuple(int((size - 1) * (i + 1) / dur * x + 0.5) for i, x in enumerate(e))
        else:
            add = tuple(int((size - 1) * x + 0.5) for x in e)
        return (Injection(typ, name, size, key, kinds[key], start, end, carrier, add) if sum(add) else None), (key, carrier)

    def _pair(start, dur, ratio):
        rates = sorted(((sum(exp.window(t, start, dur)), t) for t in tmpl), reverse=True)
        big = [r for r in rates if r[0] >= 10]
        if not big:
            return None
        hi_rate, hi_t = rng.choice(big)
        small = [r for r in rates if 0 < r[0] <= hi_rate / ratio]
        return (hi_t, small[0][1]) if small else None

    for typ, (_, sizes, dur, _) in TYPES.items():
        for name, size in zip(SIZES, sizes):
            for _ in range(ATTEMPTS):
                got = draw(typ, name, size)
                if not got or not got[0]:
                    continue
                inj, touched = got
                if any(_near((inj.start, inj.end), w, GAP) for k in touched for w in busy.get(k, [])):
                    continue
                if not _observed(sd, inj.start, inj.end):
                    continue
                for k in touched:
                    busy.setdefault(k, []).append((inj.start, inj.end))
                out.append(inj)
                break
    return out


def _observed(sd, start, end) -> bool:
    for m in range(start, end):
        day, slot = slot_of(m)
        if day not in sd.observed or not sd.observed[day][slot]:
            return False
    return True


def apply(sd: ServiceData, injs: list[Injection]) -> ServiceData:
    """A copy of `sd` with the injections in it; arrays no injection touches are shared with `sd`."""
    minutes, owned_keys, owned = dict(sd.minutes), set(), set()
    levels, first, incidents = dict(sd.levels), dict(sd.first_seen), list(sd.incidents)

    def arr(key, day):
        if (key, day) not in owned:
            if key not in owned_keys:
                minutes[key] = dict(minutes[key])
                owned_keys.add(key)
            minutes[key][day] = array("i", minutes[key][day])
            owned.add((key, day))
        return minutes[key][day]

    docs_of: dict[str, list] = {}
    for d, ts in sd.docs.items():
        for t in ts:
            docs_of.setdefault(t, []).append(d)

    def derived(t):
        lvl = LEVEL_KEY[level_class(levels.get(t))]
        return ["*total"] + ([lvl] if lvl in minutes else []) + [d for d in docs_of.get(t, []) if d in minutes]

    def change(t, minute, fn):
        day, slot = slot_of(minute)
        a = arr(t, day)
        old = a[slot]
        if old == UNOBSERVED:
            return
        new = max(fn(old), 0)
        if new == old:
            return
        a[slot] = new
        for k in derived(t):
            b = arr(k, day)
            if b[slot] != UNOBSERVED:
                b[slot] += new - old

    for inj in injs:
        span = range(inj.start, inj.end)
        if inj.type == "burst":
            minutes[inj.key] = {d: array("i", [0 if o else UNOBSERVED for o in sd.observed[d]]) for d in sd.days}
            owned_keys.add(inj.key)
            owned |= {(inj.key, d) for d in sd.days}
            levels[inj.key], first[inj.key] = "INFO", inj.start
        if inj.type == "drop":
            for t in sorted(k for k in minutes if not k.startswith(("*", "doc:"))):
                for m in span:
                    change(t, m, lambda o: int(o * inj.size + 1e-9))
        elif inj.type == "mix_swap":
            a, b = inj.pair
            for m in span:
                day, slot = slot_of(m)
                va, vb = minutes[a][day][slot], minutes[b][day][slot]
                if UNOBSERVED not in (va, vb):
                    change(a, m, lambda o, v=vb: v)
                    change(b, m, lambda o, v=va: v)
        else:
            for m, n in zip(span, inj.add):
                change(inj.carrier, m, lambda o, n=n: o + n)
        incidents.append(Incident(inj.key, inj.start, inj.end, "test-a"))
        extra = [inj.carrier] if inj.carrier and inj.carrier != inj.key and inj.key != "*total" else list(inj.pair)
        incidents += [Incident(t, inj.start, inj.end, "test-a") for t in extra]
    if MIX in minutes:
        minutes[MIX] = minutes["*total"]
    return replace(sd, minutes=minutes, levels=levels, first_seen=first, incidents=incidents)


def passes(sd: ServiceData, kinds: dict, part, seed: int, n: int = PASSES):
    """[(injected ServiceData, kinds with the burst keys as M6, injections)] for seeds seed .. seed+n-1."""
    exp, out = Expect(sd), []
    for p in range(n):
        injs = plan(sd, kinds, part, seed + p, exp)
        out.append((apply(sd, injs), {**kinds, **{i.key: "M6" for i in injs if i.type == "burst"}}, injs))
    return out


def record(conn, run_id, schema, service, pass_injs) -> None:
    """lab_incidents rows (source 'test-a') of every pass; pass_injs: list of injection lists."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO lab_incidents (source, data_schema, run_id, service, key, type, size, start_minute, end_minute, "
            "meta) VALUES ('test-a', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(schema or "public", run_id, service, i.key, i.type, i.size, i.start, i.end,
              Jsonb({"pass": p, "size_name": i.size_name, "kind": i.kind, "carrier": i.carrier, "pair": list(i.pair)}))
             for p, injs in enumerate(pass_injs) for i in injs])

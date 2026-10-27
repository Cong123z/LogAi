from array import array
from dataclasses import replace
from datetime import date, timedelta

import pytest

from scripts.lab.data import Incident, ServiceData, minute_at, slot_of
from scripts.lab.inject import GAP, NEAR, Expect, Injection, apply, passes, plan
from scripts.lab.kinds import assign
from scripts.lab.methods import DayOut, Spec
from scripts.lab.run import RunCtx
from scripts.lab.score import InjectSink

DAYS = [date(2026, 10, 1) + timedelta(n) for n in range(10)]
TUNE = DAYS[:5]
RATES = {"A": 8, "B": 4, "C": 2, "D": 1}       # A ERROR; B, C, D INFO
LEVELS = {"A": "ERROR", "B": "INFO", "C": "INFO", "D": "INFO"}


def part(key, day):
    return "tune" if day in TUNE else "test"


def flat(n):
    return array("i", [n] * 1440)


def world(incidents=()):
    """Constant counts: *total = sum of templates, *error = A, *info = B + C + D, doc:d1 = A + B; *mix shares *total."""
    m = {t: {d: flat(r) for d in DAYS} for t, r in RATES.items()}
    m["*total"] = {d: flat(sum(RATES.values())) for d in DAYS}
    m["*error"] = {d: flat(RATES["A"]) for d in DAYS}
    m["*info"] = {d: flat(RATES["B"] + RATES["C"] + RATES["D"]) for d in DAYS}
    m["doc:d1"] = {d: flat(RATES["A"] + RATES["B"]) for d in DAYS}
    m["*mix"] = m["*total"]
    first = {t: minute_at(DAYS[0], 0) - 99999 for t in RATES}
    return ServiceData("s", DAYS, m, {d: flat(1) for d in DAYS}, {}, {}, set(), set(), dict(LEVELS), first,
                       {"doc:d1": ["A", "B"]}, list(incidents))


def check_consistent(sd):
    tmpl = [k for k in sd.minutes if not k.startswith(("*", "doc:"))]
    for d in sd.days:
        for i in range(1440):
            col = {k: sd.minutes[k][d][i] for k in sd.minutes}
            assert min(col.values()) >= 0
            assert col["*total"] == sum(col[t] for t in tmpl)
            assert col["*error"] == col["A"]
            assert col["*info"] == sum(col[t] for t in tmpl if t != "A")
            assert col["doc:d1"] == col["A"] + col["B"]


def planted():
    t0 = minute_at(DAYS[6], 12 * 60)
    return [Incident("A", t0, t0 + 30, "synthetic"), Incident(None, t0 + 3000, t0 + 3030, "test-c"),
            Incident("B", t0 - 5000, t0 - 4990, "test-c")]


def build(seed=3, incidents=None):
    sd = world(planted() if incidents is None else incidents)
    kinds = assign(sd, TUNE)
    return sd, kinds, plan(sd, kinds, seed, Expect(sd))


# 1 ---------------------------------------------------------------------------------------------------------------
def test_one_spike_one_hit_with_expected_delay(cfg):
    sd = world()
    start = minute_at(DAYS[7], 600)
    inj = Injection("spike", "medium", 3, "B", "M3", start, start + 10, "B", add=(8,) * 10)
    inj_sd = apply(sd, [inj])
    assert inj_sd.minutes["B"][DAYS[7]][600] == 12 and sd.minutes["B"][DAYS[7]][600] == 4      # clean copy untouched
    kinds = assign(sd, TUNE)
    spec = Spec("fake", "T", "fake", frozenset({"M3"}), (1.0, 2.0), 1.0, False, False, 1)
    ctx = RunCtx(DAYS, TUNE, "days", part, cfg, inj_sd, [inj])
    sink = InjectSink(None, 0, "s", "fake", spec, kinds, ctx)
    for day in DAYS:
        for key in ("B", "C"):
            outs = {}
            for k in (1.0, 2.0):
                flag = bytearray(1440)
                if key == "B" and day == DAYS[7]:
                    lo = 603 if k == 1.0 else 700                # knob 1.0 flags 3 min late, knob 2.0 misses
                    flag[lo:lo + 6] = b"\1" * 6
                outs[k] = DayOut(None, None, None, array("d", [0.0] * 1440), flag)
            sink.add(key, day, outs)
    s = sink.summary()
    cell = s["1.0"]["M3"]["spike"]["medium"]
    assert cell == {"n": 1, "hits": 1, "delays": [3]}
    assert s["2.0"]["M3"]["spike"]["medium"] == {"n": 1, "hits": 0, "delays": []}


# 2 ---------------------------------------------------------------------------------------------------------------
def test_consistency_after_every_injection_type():
    sd, kinds, injs = build()
    assert {i.type for i in injs} == {"spike", "drop", "ramp", "level_shift", "burst", "mix_swap"}
    out = apply(sd, injs)
    check_consistent(out)
    assert out.minutes["*mix"] is out.minutes["*total"]
    assert any(out.minutes[k][d] is not sd.minutes[k][d] for k in sd.minutes for d in DAYS)
    untouched = sum(out.minutes["D"][d] is sd.minutes["D"][d] for d in DAYS)
    assert untouched >= 5                                         # unchanged arrays are shared, not copied
    for one in injs:                                              # and each type alone
        check_consistent(apply(sd, [one]))


def test_drop_floors_at_zero_and_rounds_down():
    sd = world()
    start = minute_at(DAYS[7], 100)
    out = apply(sd, [Injection("drop", "large", 0.1, "*total", "M1", start, start + 30)])
    assert out.minutes["A"][DAYS[7]][100] == 0 and out.minutes["B"][DAYS[7]][100] == 0
    assert out.minutes["*total"][DAYS[7]][100] == 0
    check_consistent(out)


# 3 ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_placement(seed):
    sd, kinds, injs = build(seed)
    assert len(injs) >= 12
    busy = {}
    for i in injs:
        for m in (i.start, i.end - 1):
            assert slot_of(m)[0] in DAYS[5:]                     # test part only
        for p in sd.incidents:
            assert i.end + NEAR <= p.start or p.end + NEAR <= i.start
        for k in {i.key, *([i.carrier] if i.carrier else []), *i.pair}:
            for s, e in busy.get(k, []):
                assert i.start >= e + GAP or s >= i.end + GAP
            busy.setdefault(k, []).append((i.start, i.end))
    assert injs == build(seed)[2]                                 # same seed, same set
    assert injs != build(seed + 100)[2]


def test_passes_use_distinct_seeds_and_record_incidents():
    sd = world()
    kinds = assign(sd, TUNE)
    ps = passes(sd, kinds, 7)
    assert len(ps) == 5
    assert len({tuple(i) for _, _, i in ps}) == 5
    for p_sd, p_kinds, injs in ps:
        assert sum(x.source == "test-a" for x in p_sd.incidents) >= len(injs)
        assert not [x for x in sd.incidents if x.source == "test-a"]          # the clean data keeps no injection
        assert {i.key for i in injs if i.type == "burst"} <= {k for k, v in p_kinds.items() if v == "M6"}


# 5 ---------------------------------------------------------------------------------------------------------------
def test_burst_creates_m6_key_and_mix_swap_leaves_total():
    sd = world()
    start = minute_at(DAYS[6], 300)
    burst = Injection("burst", "medium", 20, "inj:b0", "M6", start, start + 15, "inj:b0", add=(20,) * 15)
    out = apply(sd, [burst])
    assert out.first_seen["inj:b0"] == start and out.minutes["inj:b0"][DAYS[6]][300] == 20
    assert out.minutes["inj:b0"][DAYS[6]][315] == 0 and out.minutes["*total"][DAYS[6]][300] == 35
    assert Incident("inj:b0", start, start + 15, "test-a") in out.incidents
    tmpl = [k for k in out.minutes if not k.startswith(("*", "doc:"))]
    assert out.minutes["*total"][DAYS[6]][300] == sum(out.minutes[t][DAYS[6]][300] for t in tmpl)
    swap = Injection("mix_swap", "small", 2, "*mix", "M7", start, start + 60, pair=("A", "B"))
    out = apply(sd, [swap])
    assert out.minutes["*total"][DAYS[6]][300] == 15 and out.minutes["A"][DAYS[6]][300] == 4
    assert out.minutes["B"][DAYS[6]][300] == 8 and out.minutes["*error"][DAYS[6]][300] == 4
    assert list(out.minutes["*total"][DAYS[6]]) == list(sd.minutes["*total"][DAYS[6]])      # *total unchanged
    assert {i.key for i in out.incidents} == {"*mix", "A", "B"}
    check_consistent(out)


def test_short_window_still_places_mix_swap_even_if_mix_hashes_to_tune():
    days = DAYS[:6]
    sd = world()
    sd = replace(sd, days=days)
    kinds = assign(sd, days[:3])
    injs = plan(sd, kinds, 3, Expect(sd))
    assert "mix_swap" in {i.type for i in injs}
    assert all(slot_of(i.start)[0] >= days[3] for i in injs)       # last half of the window, whatever part(key, day) says

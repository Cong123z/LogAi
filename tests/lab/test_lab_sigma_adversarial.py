"""Adversarial parity: lab sigma + Tracker vs the live path (real Owner -> Detector/judge, real commit() at 02:00,
Baseline.load via owner.reload_baseline) on the same minutes. No engine/ES: the Owner gets one batch per span with
known template ids, and commit runs exactly between closing 01:59 and 02:00 (the lab's switch point)."""
import random
from datetime import date, datetime, time, timedelta

import pytest

from logai2.db import put_setting
from logai2.jobs.commit import commit
from logai2.owner.owner import Owner
from logai2.timeutil import TZ
from scripts.lab.data import minute_at
from scripts.lab.episodes import Tracker
from scripts.lab.load import load_service
from scripts.lab.methods.sigma import Sigma, rebuild_stats

SVC = "s"
D = {n: date(2026, 10, n) for n in range(5, 19)}     # Oct 6 Tue .. Oct 17 Sat


def drive_live(conn, cfg, days, gen, templates, links=(), exclusions=()):
    """The live path over `days`; gen(day, slot) -> [(template_id, level, n)]. Returns {day: Baseline loaded at 02:00}."""
    cfg.baseline.historic_after_minutes = 10 ** 9       # one batch closes many minutes: none of them is "historic"
    cfg.lab.record = True
    put_setting(conn, "detection_enabled", True)
    t0 = datetime.combine(days[0], time(), TZ)
    for tid, lv in templates.items():                   # the miner is bypassed: template rows must exist
        conn.execute("INSERT INTO templates (service, template_id, level, text, first_seen, last_seen) "
                     "VALUES (%s, %s, %s, %s, %s, %s)", (SVC, tid, lv, tid, t0, t0))
    if links:
        conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES ('D', 't', 'h', 'r')")
        for tid in links:
            conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                         "VALUES (%s, %s, 'D', 'manual')", (SVC, tid))
    for d, hf, ht in exclusions:
        conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                     "VALUES (%s, %s, %s, %s, 'exclude')", (SVC, d, hf, ht))
    conn.commit()
    cfg.flush.max_closed_minutes = 10**6   # one handle per block must close the whole block (no bites)
    clock = [minute_at(days[0], 0) * 60.0]
    owner = Owner(conn, cfg, minute_at(days[0], 0), now=lambda: clock[0])
    loaded = {}
    for d in days:
        for lo, hi in ((0, 120), (120, 1440)):
            if lo == 120:                                # the 02:00 commit, then the owner reloads the baseline
                owner.flush({}, {})
                commit(conn, cfg, d)
                owner.reload_baseline()
                loaded[d] = owner.detector.baseline
            counts = [{"service": SVC, "template_id": tid, "text": None, "level": lv, "module": "", "n": n,
                       "minute": minute_at(d, slot), "sample": ""}
                      for slot in range(lo, hi) for tid, lv, n in gen(d, slot) if n]
            clock[0] = minute_at(d, hi) * 60.0 if hi < 1440 else minute_at(d + timedelta(1), 0) * 60.0
            owner.handle({"counts": counts, "watermark_ms": int(clock[0] * 1000), "events": 0, "positions": {},
                          "sources": []})
            owner.flush({}, {})
    return loaded


def live_changes(conn):
    rows = conn.execute("SELECT key, extract(epoch FROM at)::float8 AS at, to_state FROM alert_changes "
                        "WHERE service = %s", (SVC,)).fetchall()
    conn.commit()
    return {(r["key"], int(r["at"]) // 60 - 1, r["to_state"]) for r in rows}


def lab_changes(conn, cfg, days, prev=None, per_day=False):
    """Sigma k=3, one Tracker per key carried across days (live state survives midnight); per_day=True starts a
    fresh Tracker each day, as tests/lab/test_lab_parity.py does."""
    sd = load_service(conn, SVC, days[0], days[-1], 30)
    conn.commit()
    m, trackers, done = Sigma(), {}, set()
    if prev is None:
        prev = m.fit(sd, days[0] - timedelta(1), cfg)       # as run.py does
    for d in days:
        if per_day:
            done |= {(k, mn, st) for k, tr in trackers.items() for mn, st in tr.changes}
            trackers = {}
        model = m.fit(sd, d, cfg)
        if model is not None:
            for key in sd.keys():
                if key == "*minutes":
                    continue
                o = m.day(model, prev, sd, key, d, (3.0,), cfg)[3.0]
                tr = trackers.setdefault(key, Tracker(cfg.alert))
                for slot in range(1440):
                    if sd.observed[d][slot]:
                        tr.step(minute_at(d, slot), bool(o.high_flag[slot]), o.score[slot])
        prev = model
    return sd, done | {(k, mn, st) for k, tr in trackers.items() for mn, st in tr.changes}


def keyed(changes, key):
    return sorted((mn, st) for k, mn, st in changes if k == key)


def at(d, hh, mm):
    return minute_at(d, hh * 60 + mm)


# --- the big world: Oct 6 (Tue) .. Oct 17 (Sat) -------------------------------------------------------

TPL = {"A": "INFO", "E1": "ERROR", "E2": "ERROR", "E3": "ERROR", "E4": "ERROR", "E5": "ERROR", "E7": "ERROR",
       "N": "ERROR", "W1": "WARN", "DE": "ERROR", "DI": "INFO"}
W1_14 = [10, 10, 9, 10, 10, 10, 10, 9, 9, 9, 9]           # WARN floor 10: == floor is high, 9 is not


def gen_main(d, slot):
    h, mi = divmod(slot, 60)
    wk = d.weekday() < 5
    r = random.Random(f"{d}/{slot}")
    out = []
    busy = 8 <= h < 19
    a = ((30 if busy else 3) if wk else (15 if busy else 2)) + r.randrange(7)
    if d == D[14] and h == 15 and 10 <= mi < 18:
        return []                                         # full outage: no line at all, *total = 0 (drop)
    if d == D[14] and h == 12 and 10 <= mi < 18:
        a = 2                                             # *total drop on a full weekday
    if d == D[17] and h == 12 and 10 <= mi < 18:
        a = 1                                             # weekend with 2 weekend days: young, never a drop
    out.append(("A", "INFO", a))
    if wk and h in (10, 11):            # E2: exactly 6/min -> mean 6, std 0, band_high 6
        n = 6
        if d == D[7] and h == 10:
            n = 30                                        # spike excluded by Review: the band stays 6
        if d == D[15] and h == 10 and 20 <= mi < 28:
            n = 7                                         # 7 > band 6 and >= floor 5: alerts; 6 == band never
        out.append(("E2", "ERROR", n))
    if wk and h == 11:
        out.append(("E1", "ERROR", r.randrange(2)))
    if not wk and h == 11 and mi < 6:
        out.append(("E1", "ERROR", 8))                    # Sat 10/Sun 11 borrow weekday; Sat 17 has its own profile
    if d == D[13] and h == 9 and mi < 6:
        out.append(("E3", "ERROR", 8))                    # then absent while ALERTING/COOLING
    if d == D[13] and h == 13 and mi < 2:
        out.append(("E4", "ERROR", 8))                    # then absent while WARMING
    if (d == D[14] and slot >= 23 * 60 + 56) or (d == D[15] and slot < 4):
        out.append(("E5", "ERROR", 8))                    # across midnight
    if d == D[12] and h == 2:
        out.append(("E7", "ERROR", 12))                   # the Oct 13 commit gives E7 a wide hour-2 band
    if d == D[13] and 115 <= slot < 125:
        out.append(("E7", "ERROR", 8))                    # 01:55-02:04: high on the old baseline, low on the new
    if d == D[14] and h == 17 and mi < 6:
        out.append(("N", "ERROR", 6))                     # a template born on a scored day: floor-only
    if d >= D[8] and h == 3 and mi < 5:
        out.append(("W1", "WARN", 1))                     # *warn born Oct 8
    if d == D[9] and h == 14 and mi < len(W1_14):
        out.append(("W1", "WARN", W1_14[mi]))
    if d == D[6] and slot == 180:
        out.append(("DE", "ERROR", 1))                    # gives doc:D its ERROR level
    if wk and busy:
        out.append(("DI", "INFO", 9 if d == D[15] and h == 16 and mi < 6 else 2))   # doc burst from its INFO template
    return out


def test_big_world_parity(conn, cfg):
    days = [D[n] for n in range(6, 18)]
    loaded = drive_live(conn, cfg, days, gen_main, TPL, links=("DE", "DI"), exclusions=[(D[7], 10, 11)])
    live = live_changes(conn)
    sd, lab = lab_changes(conn, cfg, days)

    # rebuild_stats is bit-identical to the SQL rebuild at every commit (boundaries like count == band_high need it)
    for d, b in loaded.items():
        got = rebuild_stats(sd, d)
        assert got.days == b.days, d
        assert got.stats.keys() == b.stats.keys(), d
        diff = [k for k in b.stats if tuple(got.stats[k]) != tuple(b.stats[k])]
        assert not diff, (d, diff[:5], [(got.stats[k], b.stats[k]) for k in diff[:5]])

    # the world really reached the edges
    assert loaded[D[14]].days[(SVC, "weekday")] == 5 and loaded[D[13]].days[(SVC, "weekday")] == 5
    assert loaded[D[12]].days[(SVC, "weekday")] == 4                    # young until the Oct 13 commit
    assert loaded[D[17]].days[(SVC, "weekend")] == 2                    # own weekend profile, no borrow
    assert loaded[D[17]].stats[(SVC, "*warn", "weekday", 3)]            # *warn born mid-history has stats
    assert loaded[D[14]].stats[(SVC, "E2", "weekday", 10)] == (6.0, 0.0)  # exclusion kept the spike out
    cut = minute_at(days[1], 120)                     # day 1 before 02:00 runs on an empty live baseline
    live = {x for x in live if x[1] >= cut}
    lab = {x for x in lab if x[1] >= cut}
    assert (at(D[14], 12, 11), "WARMING") in keyed(live, "*total")       # drop on a full weekday
    assert (at(D[14], 15, 11), "WARMING") in keyed(live, "*total")       # outage: *total = 0 is judged
    assert not [x for x in keyed(live, "*total") if D[17] == datetime.fromtimestamp(x[0] * 60, TZ).date()]
    assert (at(D[15], 10, 21), "WARMING") in keyed(live, "E2")
    assert len(keyed(live, "E2")) == 4 + 4                              # Oct 7 spike + Oct 15 boundary only
    assert any(st == "ALERTING" for _, st in keyed(live, "doc:D"))
    assert (at(D[13], 1, 56), "WARMING") in keyed(live, "E7") and (at(D[13], 2, 0), "COOLING") in keyed(live, "E7")
    assert [st for _, st in keyed(live, "E3")] == ["WARMING", "ALERTING", "COOLING", "NORMAL"]
    assert [st for _, st in keyed(live, "E4")] == ["WARMING", "NORMAL"]
    assert any(mn >= minute_at(D[15], 0) for mn, _ in keyed(live, "E5"))
    e1_days = {datetime.fromtimestamp(mn * 60, TZ).date() for mn, _ in keyed(live, "E1")}
    assert D[10] in e1_days and D[11] in e1_days and D[17] not in e1_days   # borrowed band vs own weekend band
    assert keyed(live, "W1") and keyed(live, "N")

    print(f"\nPARITY transitions live={len(live)} lab={len(lab)} keys={sorted({k for k, _, _ in live})}")
    assert sorted(lab - live) == [] and sorted(live - lab) == []
    # sensitivity: a fresh Tracker per day (test_lab_parity's loop) loses the alert that crosses midnight
    _, daily = lab_changes(conn, cfg, days, per_day=True)
    daily = {x for x in daily if x[1] >= cut}
    assert {k for k, _, _ in daily ^ live} == {"E5", "*error"}


# --- a small world for two known gaps ------------------------------------------------------------------

def gen_small(escalate, wide_hour1):
    def gen(d, slot):
        h, mi = divmod(slot, 60)
        out = [("A", "INFO", 5)]
        if escalate and d == D[7] and h == 11 and mi < 6:
            out.append(("X", "INFO", 8))                  # X is INFO here: no floor, live never alerts
        if escalate and d == D[8] and slot == 16 * 60:
            out.append(("X", "ERROR", 1))                 # later the same template logs one ERROR line
        if wide_hour1 and d == D[7] and h == 1:
            out.append(("E1", "ERROR", 40))
        if wide_hour1 and d == D[8] and 70 <= slot < 76:
            out.append(("E1", "ERROR", 8))                # 01:10: before the Oct 8 commit
        return out
    return gen


@pytest.mark.xfail(strict=True, reason="BUG: lab judges every minute with the template's FINAL level "
                   "(templates.level = max severity ever seen); live uses the max severity seen SO FAR, so a "
                   "template that was INFO when it burst and logs ERROR later alerts in the lab only")
def test_level_escalation_parity(conn, cfg):
    days = [D[6], D[7], D[8]]
    drive_live(conn, cfg, days, gen_small(True, False), {"A": "INFO", "X": "INFO"})
    live = live_changes(conn)
    _, lab = lab_changes(conn, cfg, days)
    assert keyed(live, "X") == []
    assert keyed(lab, "X") == keyed(live, "X")       # lab: WARMING 11:01, ALERTING 11:04, COOLING 11:06, NORMAL 11:08


def test_first_window_day_prev_none(conn, cfg):
    days = [D[6], D[7], D[8]]
    drive_live(conn, cfg, days, gen_small(False, True), {"A": "INFO", "E1": "ERROR"})
    live = {x for x in live_changes(conn) if x[1] >= minute_at(D[8], 0)}
    _, lab = lab_changes(conn, cfg, [D[8]])          # the window starts on Oct 8, as run.py does: prev = None
    assert keyed(live, "E1")                         # floor-only on the Oct 7 baseline (E1 unknown): alerts
    assert keyed(lab, "E1") == keyed(live, "E1")     # lab: Oct 8 baseline, hour-1 band ~100: nothing

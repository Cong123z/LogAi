"""Reader: per-index positions, _id dedup, known-line cache, watermark."""
from __future__ import annotations

from logai2.reader.cache import KnownLines
from logai2.reader.reader import Reader
from tests.fakes import FakeSource, hit

T0 = 1_792_000_000_000          # a whole-minute epoch ms
NOW = lambda: T0 / 1000 + 600   # noqa: E731  upper_ms = T0 + 570 s
UPPER = T0 + 570_000


def make(cfg, hits, floors, cache=None):
    src = FakeSource(hits)
    r = Reader(src, cfg, cache or KnownLines(10), now=NOW)
    r.set_indices(floors, {})
    return src, r


def test_aggregates_and_dedups(cfg):
    src = FakeSource({"a": [hit("1", T0, service="s", message="boom 1"),
                            hit("2", T0, service="s", message="boom 2")]})
    r = Reader(src, cfg, KnownLines(10), now=NOW)
    r.set_indices({"a": T0}, {})
    b = r.poll()
    assert sum(c["n"] for c in b["counts"]) == 2 and b["events"] == 2
    assert len(b["counts"]) == 1 and b["counts"][0]["template_id"] is None   # same normalized text
    c = b["counts"][0]
    assert (c["service"], c["minute"], c["sample"]) == ("s", T0 // 60_000, "boom 1")
    assert c["text"] and c["level"] == "INFO" and c["module"] == ""
    assert b["positions"]["a"] == {"pos_ms": T0, "caught_up": False, "failing": False}
    assert b["watermark_ms"] == T0
    b2 = r.poll()                       # re-reads T0 ms; both ids already seen
    assert b2["counts"] == [] and b2["positions"]["a"]["caught_up"]
    assert b2["watermark_ms"] == UPPER


def test_position_moves_and_new_doc_on_same_ms_is_read(cfg):
    src, r = make(cfg, {"a": [hit("1", T0, service="s", message="x")]}, {"a": T0})
    r.poll()
    src.hits["a"] += [hit("2", T0, service="s", message="x"), hit("3", T0 + 5, service="s", message="x")]
    b = r.poll()
    assert b["events"] == 2 and b["positions"]["a"]["pos_ms"] == T0 + 5


def test_cache_hit_sends_template_id(cfg):
    src, r = make(cfg, {"a": [hit("1", T0, service="s", message="boom 1")]}, {"a": T0})
    miss = r.poll()["counts"][0]
    r.learn({("s", miss["text"]): "s:T00001"})
    src.hits["a"].append(hit("2", T0 + 1, service="s", message="boom 7"))
    (c,) = r.poll()["counts"]
    assert (c["template_id"], c["text"], c["n"], c["sample"]) == ("s:T00001", None, 1, "boom 7")


def test_malformed_hits_are_counted_not_aggregated(cfg):
    _, r = make(cfg, {"a": [{"_id": "bad", "sort": [T0, 1], "_source": "x"},
                            hit("1", T0, service="s", message="m")]}, {"a": T0})
    b = r.poll()
    assert b["events"] == 1 and r.malformed == 1


def test_watermark_is_slowest_index(cfg):
    _, r = make(cfg, {"a": [hit("1", T0 + 120_000, service="s", message="m")],
                      "b": [hit("2", T0 + 30_000, service="s", message="m")]}, {"a": T0, "b": T0})
    assert r.poll()["watermark_ms"] == T0 + 30_000


def test_failing_index_stops_holding_watermark(cfg):
    src, r = make(cfg, {"a": [hit("1", T0 + 120_000, service="s", message="m")],
                        "b": [hit("2", T0 + 30_000, service="s", message="m")]}, {"a": T0, "b": T0})
    for i in range(3):
        src.fail("b")
        b = r.poll()
        assert b["positions"]["b"]["failing"] == (i == 2)
    assert b["watermark_ms"] == UPPER            # a caught up; b ignored
    b = r.poll()                                 # one success clears it
    assert not b["positions"]["b"]["failing"] and b["watermark_ms"] == T0 + 30_000


def test_no_healthy_index_watermark_is_upper(cfg):
    src, r = make(cfg, {"a": []}, {"a": T0})
    for _ in range(3):
        src.fail("a")
        b = r.poll()
    assert b["watermark_ms"] == UPPER


def test_removed_index_dropped(cfg):
    _, r = make(cfg, {"a": [hit("1", T0 + 120_000, service="s", message="m")],
                      "b": [hit("2", T0 + 30_000, service="s", message="m")]}, {"a": T0, "b": T0})
    r.poll()
    r.set_indices({"a": T0}, {})        # b removed → not in positions, not in watermark
    b = r.poll()
    assert set(b["positions"]) == {"a"} and b["watermark_ms"] == UPPER
    assert dict(r.floors) == {"a": T0}


def test_saved_cursor_wins_over_floor_and_dedup_survives(cfg):
    src, r = make(cfg, {"a": [hit("1", T0, service="s", message="m"),
                              hit("2", T0 + 60_000, service="s", message="m")]}, {"a": T0})
    assert r.poll()["events"] == 2
    r.set_indices({"a": T0}, {"a": T0 + 60_000})
    b = r.poll()                        # re-reads from the saved cursor; "2" already seen
    assert b["events"] == 0 and b["positions"]["a"]["pos_ms"] == T0 + 60_000


def test_forget_before_drops_old_ids_only(cfg):
    src, r = make(cfg, {"a": [hit("1", T0, service="s", message="m"),
                              hit("2", T0 + 60_000, service="s", message="m")]}, {"a": T0})
    r.poll()
    r.forget_before(T0 + 60_000)
    r.set_indices({"a": T0}, {})        # rewind to the floor: "1" is read again, "2" is not
    assert r.poll()["events"] == 1


def test_cursors_use_boundary_but_not_before_floor(cfg):
    _, r = make(cfg, {}, {"a": T0})
    r.set_indices({"a": T0, "b": T0 + 600_000}, {})
    assert r.cursors(T0 + 60_000) == {"a": (T0 + 60_000, T0), "b": (T0 + 600_000, T0 + 600_000)}


def test_known_lines_lru():
    k = KnownLines(2)
    k.put("s", "a", "A"); k.put("s", "b", "B")
    assert k.get("s", "a") == "A"       # a now most recent
    k.put("s", "c", "C")                # evicts b
    assert k.get("s", "b") is None and k.get("s", "c") == "C"
    assert (k.hits, k.misses) == (2, 1)

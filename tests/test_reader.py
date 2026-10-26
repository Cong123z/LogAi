"""Reader: per-pattern positions, _id dedup, known-line cache, watermark."""
from __future__ import annotations

from logai2.reader.cache import KnownLines
from logai2.reader.reader import Reader
from tests.fakes import FakeSource, hit

T0 = 1_792_000_000_000          # a whole-minute epoch ms
NOW = lambda: T0 / 1000 + 600   # noqa: E731  upper_ms = T0 + 570 s
UPPER = T0 + 570_000


def make(cfg, hits, floors, cache=None):
    src = FakeSource(hits)
    src.size = cfg.elasticsearch.batch_size
    r = Reader(src, cfg, cache or KnownLines(10), now=NOW)
    r.set_sources(floors, {})
    return src, r


def test_aggregates_and_dedups(cfg):
    src = FakeSource({"a": [hit("1", T0, service="s", message="boom 1"),
                            hit("2", T0, service="s", message="boom 2")]})
    r = Reader(src, cfg, KnownLines(10), now=NOW)
    r.set_sources({"a": T0}, {})
    b = r.poll()
    assert sum(c["n"] for c in b["counts"]) == 2 and b["events"] == 2
    assert len(b["counts"]) == 1 and b["counts"][0]["template_id"] is None   # same normalized text
    c = b["counts"][0]
    assert (c["service"], c["minute"], c["sample"]) == ("s", T0 // 60_000, "boom 1")
    assert c["text"] and c["level"] == "INFO" and c["module"] == ""
    # a short page reached upper_ms: caught up, watermark upper_ms
    assert b["positions"]["a"] == {"pos_ms": T0, "caught_up": True, "failing": False}
    assert b["watermark_ms"] == UPPER
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


def test_watermark_is_slowest_pattern(cfg):
    cfg.elasticsearch.batch_size = 1    # full pages: neither pattern reached upper_ms
    _, r = make(cfg, {"a": [hit("1", T0 + 120_000, service="s", message="m")],
                      "b": [hit("2", T0 + 30_000, service="s", message="m")]}, {"a": T0, "b": T0})
    assert r.poll()["watermark_ms"] == T0 + 30_000


def test_failing_pattern_stops_holding_watermark(cfg):
    cfg.elasticsearch.batch_size = 1
    src, r = make(cfg, {"a": [hit("1", T0 + 120_000, service="s", message="m")],
                        "b": [hit("2", T0 + 30_000, service="s", message="m")]}, {"a": T0, "b": T0})
    for i in range(3):
        src.fail("b")
        b = r.poll()
        assert b["positions"]["b"]["failing"] == (i == 2)
    assert b["watermark_ms"] == UPPER            # a caught up; b ignored
    b = r.poll()                                 # one success clears it
    assert not b["positions"]["b"]["failing"] and b["watermark_ms"] == T0 + 30_000


def test_no_healthy_pattern_holds_watermark(cfg):
    # ES down: closing minutes up to upper_ms would write them empty and drop the outage docs as late
    src, r = make(cfg, {"a": [hit("1", T0 + 30_000, service="s", message="m")]}, {"a": T0})
    assert r.poll()["watermark_ms"] == UPPER
    for _ in range(3):
        src.fail("a")
        b = r.poll()
    assert b["positions"]["a"]["failing"] and b["watermark_ms"] == T0 + 30_000


def test_no_patterns_watermark_is_upper(cfg):
    _, r = make(cfg, {}, {})
    assert r.poll()["watermark_ms"] == UPPER


def test_continuous_stream_still_catches_up(cfg):
    # new docs every poll: a page shorter than the page size reached upper_ms
    cfg.elasticsearch.batch_size = 5
    src, r = make(cfg, {"a": []}, {"a": T0})
    for k in range(4):
        src.hits["a"] += [hit(f"{k}-{i}", T0 + k * 1000 + i, service="s", message="m") for i in range(3)]
        b = r.poll()
        assert b["events"] == 3 and b["positions"]["a"]["caught_up"] and b["watermark_ms"] == UPPER


def test_full_page_is_not_caught_up(cfg):
    cfg.elasticsearch.batch_size = 2
    _, r = make(cfg, {"a": [hit(str(i), T0 + i, service="s", message="m") for i in range(3)]}, {"a": T0})
    b = r.poll()
    assert not b["positions"]["a"]["caught_up"] and b["watermark_ms"] == T0 + 1
    b = r.poll()                        # re-reads T0+1 plus the last doc: still a full page
    assert b["events"] == 1 and not b["positions"]["a"]["caught_up"] and b["watermark_ms"] == T0 + 2
    b = r.poll()                        # short page
    assert b["events"] == 0 and b["positions"]["a"]["caught_up"] and b["watermark_ms"] == UPPER


def test_removed_pattern_dropped(cfg):
    _, r = make(cfg, {"a": [hit("1", T0 + 120_000, service="s", message="m")],
                      "b": [hit("2", T0 + 30_000, service="s", message="m")]}, {"a": T0, "b": T0})
    r.poll()
    r.set_sources({"a": T0}, {})        # b removed → not in positions, not in watermark
    b = r.poll()
    assert set(b["positions"]) == {"a"} and b["watermark_ms"] == UPPER
    assert dict(r.floors) == {"a": T0}


def test_saved_cursor_wins_over_floor_for_new_pattern(cfg):
    src, r = make(cfg, {"a": [hit("1", T0, service="s", message="m"),
                              hit("2", T0 + 60_000, service="s", message="m")]}, {})
    r.set_sources({"a": T0}, {"a": T0 + 60_000})
    b = r.poll()
    assert b["events"] == 1 and b["positions"]["a"]["pos_ms"] == T0 + 60_000


def test_refresh_does_not_rewind_tracked_pattern(cfg):
    src, r = make(cfg, {"a": [hit("1", T0 + 60_000, service="s", message="m")]}, {"a": T0})
    assert r.poll()["events"] == 1
    r.poll()                            # quiet poll: caught up
    r.set_sources({"a": T0}, {"a": T0})
    b = r.poll()
    assert b["events"] == 0 and b["positions"]["a"] == {
        "pos_ms": T0 + 60_000, "caught_up": True, "failing": False}


def test_caught_up_flips_back_when_new_doc_arrives(cfg):
    cfg.elasticsearch.batch_size = 2
    src, r = make(cfg, {"a": [hit("1", T0, service="s", message="m")]}, {"a": T0})
    r.poll()
    assert r.poll()["positions"]["a"]["caught_up"]
    src.hits["a"].append(hit("2", T0 + 10, service="s", message="m"))
    b = r.poll()
    assert not b["positions"]["a"]["caught_up"] and b["watermark_ms"] == T0 + 10


def test_forget_before_keeps_ids_at_or_after_position(cfg):
    # Idle index: its last doc sits before the boundary and is re-read every poll.
    _, r = make(cfg, {"a": [hit("1", T0, service="s", message="m"),
                            hit("2", T0 + 5, service="s", message="m")]}, {"a": T0})
    assert r.poll()["events"] == 2
    r.forget_before(T0 + 120_000)
    b = r.poll()
    assert b["events"] == 0 and b["positions"]["a"]["caught_up"] and b["watermark_ms"] == UPPER
    assert set(r._seen["a"]) == {("a", "2")}   # "1" is before the position: forgotten


def test_non_numeric_sort_is_malformed(cfg):
    _, r = make(cfg, {"a": [hit("1", T0, service="s", message="m")]}, {"a": T0})
    r.source.hits["a"].append({"_id": "x", "sort": ["junk", 1], "_source": {}})
    r.source.page = lambda *_: r.source.hits["a"]
    b = r.poll()
    assert b["events"] == 1 and r.malformed == 1


def test_cursors_use_boundary_but_not_before_floor(cfg):
    _, r = make(cfg, {}, {"a": T0})
    r.set_sources({"a": T0, "b": T0 + 600_000}, {})
    assert r.cursors(T0 + 60_000) == {"a": (T0 + 60_000, T0), "b": (T0 + 600_000, T0 + 600_000)}


def test_known_lines_lru():
    k = KnownLines(2)
    k.put("s", "a", "A"); k.put("s", "b", "B")
    assert k.get("s", "a") == "A"       # a now most recent
    k.put("s", "c", "C")                # evicts b
    assert k.get("s", "b") is None and k.get("s", "c") == "C"
    assert (k.hits, k.misses) == (2, 1)


def test_normalize_failure_counts_line_unparsed(cfg, monkeypatch):
    from logai2.reader import reader as reader_mod
    real = reader_mod.normalize
    monkeypatch.setattr(reader_mod, "normalize",
                        lambda m: (_ for _ in ()).throw(ValueError("bad")) if "poison" in m else real(m))
    _, r = make(cfg, {"a": [hit("1", T0, service="s", message="poison pill"),
                            hit("2", T0 + 1, service="s", message="ok")]}, {"a": T0})
    b = r.poll()
    assert b["events"] == 2
    bad = [c for c in b["counts"] if c["text"] is None and c["template_id"] is None]
    assert len(bad) == 1 and bad[0]["n"] == 1 and bad[0]["sample"] == "poison pill"


def test_new_daily_index_first_minute_counted(cfg):
    src, r = make(cfg, {"app-1": [hit("1", T0, service="s", message="m")]}, {"app-*": T0})
    assert r.poll()["events"] == 1
    src.hits["app-2"] = [hit("2", T0 + 60_000, service="s", message="m")]   # no set_sources call
    b = r.poll()
    assert b["events"] == 1 and b["positions"]["app-*"]["pos_ms"] == T0 + 60_000


def test_one_search_per_pattern_per_poll(cfg):
    src, r = make(cfg, {f"app-{i}": [hit(str(i), T0 + i, service="s", message="m")] for i in range(3)}, {"app-*": T0})
    assert r.poll()["events"] == 3
    assert src.calls == ["app-*"]
    r.poll()
    assert src.calls == ["app-*"] * 2


def test_dedup_same_id_two_indices(cfg):
    _, r = make(cfg, {"app-1": [hit("1", T0, service="s", message="m")],
                      "app-2": [hit("1", T0, service="s", message="m")]}, {"app-*": T0})
    assert r.poll()["events"] == 2
    assert r.poll()["events"] == 0      # re-read, both (_index, _id) already seen


def test_overlapping_patterns_read_once(cfg):
    src = FakeSource({"app-pay-1": [hit(str(i), T0 + i, service="s", message="m") for i in range(3)]})
    r = Reader(src, cfg, KnownLines(10), now=NOW)
    r.set_sources({"app-*": T0, "app-pay-*": T0}, {}, {"app-*": "app-*", "app-pay-*": "app-pay-*,-app-*"})
    assert r.poll()["events"] == 3
    assert src.calls == ["app-*", "app-pay-*,-app-*"]

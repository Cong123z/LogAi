from logai2 import commands


def test_enqueue_take_finish(conn):
    a = commands.enqueue(conn, "reload_links")
    b = commands.enqueue(conn, "recompute_baseline", {"why": "test"})
    taken = commands.take(conn)
    conn.commit()
    assert [(c["id"], c["kind"], c["payload"]) for c in taken] == [(a, "reload_links", {}),
                                                                 (b, "recompute_baseline", {"why": "test"})]
    commands.finish(conn, a, {"ok": True})
    assert [c["id"] for c in commands.take(conn)] == [b]
    conn.commit()
    row = conn.execute("SELECT result, done_at FROM commands WHERE id = %s", (a,)).fetchone()
    assert row["result"] == {"ok": True} and row["done_at"] is not None

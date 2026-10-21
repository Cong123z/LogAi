from scripts.link_eval import corpus_top2, layer2_table, nearest_manual, score, truth
from tests.test_links import at, doc, link, tmpl, vec


def test_leave_one_out_and_corpus_tables(conn):
    doc(conn, "D1", vec((0, 1.0))), doc(conn, "D2", vec((9, 1.0)))
    tmpl(conn, "A", v=vec((0, 1.0))), link(conn, "A", "D1")
    tmpl(conn, "B", v=at(0.9, 5)), link(conn, "B", "D1")
    tmpl(conn, "C", v=at(0.86, 6)), link(conn, "C", "D2")       # near A/B but a person chose D2
    templates = truth(conn)
    near = nearest_manual(conn)
    assert score(templates, lambda t: near[(t["service"], t["template_id"])][0]) == (2, 1, 0)
    top2 = corpus_top2(conn)
    assert [d for _, d in top2[("s", "C")]] == ["D1", "D2"]
    table = layer2_table("x", templates, top2)
    assert "| 0.85 | 0.00 | 2 | 1 | 0 |" in table and "| 0.92 | 0.00 | 1 | 0 | 2 |" in table

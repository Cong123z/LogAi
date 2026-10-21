"""Layered links (logai2/links.py): vectors are built so cosine similarities are exact."""
import math

import pytest

from logai2.embed import literal
from logai2.links import apply_links, candidates, cluster_edges, desired_corpus, error_lines, level_class, similar


def vec(*parts: tuple[int, float]) -> list[float]:
    v = [0.0] * 1024
    for i, x in parts:
        v[i] = x
    return v


def at(sim: float, axis: int, anchor: int = 0) -> list[float]:
    """Cosine `sim` with the anchor axis, the rest on its own axis."""
    return vec((anchor, sim), (axis, math.sqrt(1 - sim * sim)))


def tmpl(conn, tid, level="ERROR", v=None, service="s"):
    conn.execute("INSERT INTO templates VALUES (%s, %s, %s, %s, now(), now(), %s::vector)",
                 (service, tid, level, "text " + tid, literal(v) if v else None))


def link(conn, tid, doc, source="manual", service="s"):
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES (%s, %s, %s, %s)",
                 (service, tid, doc, source))


def doc(conn, doc_id, *vectors, status="active"):
    conn.execute("INSERT INTO documents (doc_id, title, handling, status, revision) VALUES (%s, %s, 'h', %s, 'r')",
                 (doc_id, doc_id, status))
    for n, v in enumerate(vectors):
        conn.execute("INSERT INTO document_errors VALUES (%s, %s, 'line', %s::vector)", (doc_id, n, literal(v)))


def links(conn):
    return {r["template_id"]: (r["source"], r["doc_id"]) for r in conn.execute(
        "SELECT template_id, source, doc_id FROM template_doc_links WHERE source <> 'manual' ORDER BY 1")}


@pytest.fixture
def docs(cfg):
    return cfg.documentation   # ERROR 0.85, WARN 0.88, INFO 0.92, margin 0.05


def test_inherit_per_class_threshold_same_service_only(conn, docs):
    doc(conn, "D1")
    for anchor, (level, m, near, far) in enumerate([("ERROR", "e:M", 0.86, 0.84), ("WARN", "w:M", 0.89, 0.87),
                                                    ("INFO", "i:M", 0.93, 0.91)]):
        tmpl(conn, m, level, vec((anchor, 1.0)))
        link(conn, m, "D1")
        tmpl(conn, m[:2] + "near", level, at(near, 10 + anchor, anchor))
        tmpl(conn, m[:2] + "far", level, at(far, 20 + anchor, anchor))
    tmpl(conn, "x:info-near-error", "INFO", at(0.99, 30))      # close to e:M (axis 0) but another class
    tmpl(conn, "o:other-svc", "ERROR", at(0.99, 31), service="other")
    assert apply_links(conn, docs) is True
    assert links(conn) == {t: ("inherited", "D1") for t in ("e:near", "w:near", "i:near")}
    row = conn.execute("SELECT similarity, inherited_from FROM template_doc_links WHERE template_id = 'e:near'").fetchone()
    assert row["inherited_from"] == "e:M" and math.isclose(row["similarity"], 0.86, abs_tol=1e-4)
    assert apply_links(conn, docs) is False   # idempotent


def test_never_inherit_from_an_automatic_link_and_manual_or_rejected_rows_stay(conn, docs):
    doc(conn, "D1"), doc(conn, "D2")
    tmpl(conn, "M", v=vec((0, 1.0))), link(conn, "M", "D1")
    tmpl(conn, "X", v=vec((0, 0.9), (5, math.sqrt(0.19))))       # inherits from M (0.9)
    tmpl(conn, "Y", v=vec((0, 0.6), (5, 0.8)))                   # 0.89 to X, 0.6 to M: no chain
    tmpl(conn, "N", v=at(0.95, 6)), link(conn, "N", None)        # sticky "no document"
    tmpl(conn, "R", v=at(0.95, 7)), link(conn, "R", "D2", "rejected")
    tmpl(conn, "H", v=at(0.95, 8)), link(conn, "H", "D2")        # a manual link elsewhere wins
    apply_links(conn, docs)
    assert links(conn) == {"R": ("rejected", "D2"), "X": ("inherited", "D1")}
    assert conn.execute("SELECT doc_id FROM template_doc_links WHERE template_id = 'H'").fetchone()["doc_id"] == "D2"


def test_removing_the_manual_source_or_changing_level_drops_the_inherited_link(conn, docs):
    doc(conn, "D1")
    tmpl(conn, "M", v=vec((0, 1.0))), link(conn, "M", "D1")
    tmpl(conn, "X", v=at(0.9, 5)), tmpl(conn, "Z", v=at(0.9, 6))
    apply_links(conn, docs)
    conn.execute("UPDATE templates SET level = 'INFO' WHERE template_id = 'Z'")
    assert apply_links(conn, docs) is True and links(conn) == {"X": ("inherited", "D1")}
    conn.execute("DELETE FROM template_doc_links WHERE template_id = 'M'")
    assert apply_links(conn, docs) is True and links(conn) == {}


def test_corpus_link_needs_threshold_and_margin_and_an_active_document(conn, docs):
    doc(conn, "D1", vec((0, 1.0)), vec((40, 1.0)))       # two ERROR lines: they never compete
    doc(conn, "D2", vec((41, 1.0)))
    doc(conn, "DRAFT", vec((42, 1.0)), status="draft")
    tmpl(conn, "A", v=at(0.90, 5))                                        # D1 0.90, nothing else near
    tmpl(conn, "B", v=vec((0, 0.9), (40, 0.3), (5, math.sqrt(0.1))))     # D1 best line 0.9 (two D1 lines)
    tmpl(conn, "C", v=vec((40, 0.66), (41, 0.65), (7, math.sqrt(1 - 0.66**2 - 0.65**2))))  # 0.01 apart: margin
    tmpl(conn, "W", "WARN", v=at(0.87, 8))                                # below WARN 0.88
    tmpl(conn, "F", v=vec((42, 1.0)))                                     # only a draft's line is close
    assert set(desired_corpus(conn, docs.corpus_similarity, docs.corpus_margin)) == {("s", "A"), ("s", "B")}
    apply_links(conn, docs)
    assert links(conn) == {"A": ("corpus", "D1"), "B": ("corpus", "D1")}
    tmpl(conn, "C2", v=vec((40, 0.9), (41, 0.3), (9, math.sqrt(0.1))))   # D1 0.9, D2 0.3
    apply_links(conn, docs)
    assert links(conn)["C2"] == ("corpus", "D1")


def test_inherited_beats_corpus_and_both_replace_llm_and_draft(conn, docs):
    doc(conn, "D1"), doc(conn, "D2", vec((5, 1.0)))
    doc(conn, "DRAFT", status="draft")
    tmpl(conn, "M", v=vec((0, 1.0))), link(conn, "M", "D1")
    tmpl(conn, "X", v=vec((0, 0.86), (5, math.sqrt(1 - 0.86**2))))   # inherits D1 at 0.86, corpus D2 at 0.51
    tmpl(conn, "Y", v=vec((5, 0.99), (6, math.sqrt(1 - 0.99**2))))   # corpus D2 0.99
    link(conn, "Y", "DRAFT", "draft")
    tmpl(conn, "Z", v=vec((0, 0.95), (7, math.sqrt(1 - 0.95**2))))
    link(conn, "Z", "D2", "llm")
    apply_links(conn, docs)
    assert links(conn) == {"X": ("inherited", "D1"), "Y": ("corpus", "D2"), "Z": ("inherited", "D1")}
    assert conn.execute("SELECT status FROM documents WHERE doc_id = 'DRAFT'").fetchone()["status"] == "rejected"


def test_similar_candidates_and_cluster_edges(conn):
    doc(conn, "D1", vec((0, 1.0))), doc(conn, "D2", vec((5, 1.0))), doc(conn, "D3", vec((6, 1.0)))
    tmpl(conn, "A", v=vec((0, 0.8), (5, 0.6)))
    tmpl(conn, "B", v=vec((0, 0.8), (5, 0.6))), link(conn, "B", "D1")
    tmpl(conn, "C", v=vec((0, 1.0)))
    tmpl(conn, "I", "INFO", v=vec((0, 0.8), (5, 0.6)))
    assert [(r["template_id"], round(r["similarity"], 2), r["current_doc_id"]) for r in similar(conn, "s", "A", 0.7)] == [
        ("B", 1.0, "D1"), ("C", 0.8, None)]
    assert [(c["doc_id"], round(c["similarity"], 2)) for c in candidates(conn, "s", "A", 3, 0.5)] == [
        ("D1", 0.8), ("D2", 0.6)]
    assert cluster_edges(conn, [("s", "A"), ("s", "B"), ("s", "C")], 0.85) == [("s", "A", "B")]


def test_level_class_and_error_lines():
    assert [level_class(x) for x in ("fatal", "Warning", "DEBUG", None)] == ["ERROR", "WARN", "INFO", "INFO"]
    assert error_lines("Timeout for 10.0.0.1\n\n  \n42") == ["Timeout for <*>"]

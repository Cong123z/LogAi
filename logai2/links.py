"""Template -> document links in layers (design section 3, plan 2026-10-21-embeddings-doc-links).

0 manual, 1 inherited (nearest manual template), 2 corpus (a document's ERROR lines),
3 llm (the LLM picked a candidate), 4 draft (the LLM wrote a document). A lower layer wins.
Only manual links are inheritance sources. Nothing automatic overwrites manual or rejected rows.
"""
from __future__ import annotations

from logai2.reader.normalize import normalize

# Links the detector, alert rows and metrics use. draft / rejected / NULL never drive alerts.
ALERTING_SOURCES = ("manual", "inherited", "corpus", "llm")
AUTO_SOURCES = ("inherited", "corpus")       # recomputed by apply_links every cycle
LLM_SOURCES = ("llm", "draft")               # written by the resolver; replaced by layers 1-2

_ERROR = ("ERROR", "FATAL", "CRITICAL")
_WARN = ("WARN", "WARNING")


def level_class(level: str | None) -> str:
    level = (level or "").upper()
    return "ERROR" if level in _ERROR else "WARN" if level in _WARN else "INFO"


def class_sql(column: str) -> str:
    return (f"CASE WHEN upper({column}) IN {_ERROR!r} THEN 'ERROR' "
            f"WHEN upper({column}) IN {_WARN!r} THEN 'WARN' ELSE 'INFO' END")


def error_lines(error: str) -> list[str]:
    """A document's ERROR field as the linker embeds it: one normalized line per non-empty text line."""
    return [n for n in (normalize(line) for line in error.splitlines() if line.strip()) if n.strip("<*> ")]


def _by_class(cls: str, name: str) -> str:
    return f"CASE {cls} WHEN 'ERROR' THEN %({name}_error)s WHEN 'WARN' THEN %({name}_warn)s ELSE %({name}_info)s END"


def _params(name: str, thresholds: dict) -> dict:
    return {f"{name}_{c.lower()}": float(thresholds[c]) for c in ("ERROR", "WARN", "INFO")}


# Templates a person decided (manual, also "no document") or rejected: nothing automatic touches them.
_DECIDED = ("NOT EXISTS (SELECT 1 FROM template_doc_links x WHERE x.service = t.service "
            "AND x.template_id = t.template_id AND x.source IN ('manual', 'rejected'))")


# A document applies to its own service's templates, or to all when shared (service NULL).
_SAME_SERVICE = "(d.service IS NULL OR d.service = t.service)"


def desired_inherited(conn, thresholds: dict) -> dict[tuple[str, str], tuple]:
    """Layer 1: (service, template) -> (doc_id, similarity, inherited_from), nearest manual template."""
    t_cls, s_cls = class_sql("t.level"), class_sql("s.level")
    rows = conn.execute(f"""
        SELECT t.service, t.template_id, m.doc_id, m.sim, m.template_id AS inherited_from
        FROM templates t CROSS JOIN LATERAL (
          SELECT l.doc_id, s.template_id, 1 - (s.embedding <=> t.embedding) AS sim
          FROM template_doc_links l JOIN templates s USING (service, template_id)
          WHERE l.service = t.service AND l.source = 'manual' AND l.doc_id IS NOT NULL
            AND s.embedding IS NOT NULL AND {s_cls} = {t_cls}
          ORDER BY s.embedding <=> t.embedding, s.template_id LIMIT 1) m
        WHERE t.embedding IS NOT NULL AND {_DECIDED} AND m.sim >= {_by_class(t_cls, 'inherit')}
        """, _params("inherit", thresholds)).fetchall()
    return {(r["service"], r["template_id"]): (r["doc_id"], r["sim"], r["inherited_from"]) for r in rows}


def desired_corpus(conn, thresholds: dict, margin: float) -> dict[tuple[str, str], tuple]:
    """Layer 2: (service, template) -> (doc_id, similarity) when one active document's ERROR line is
    close enough and at least `margin` closer than any other document's."""
    # ponytail: every template against every ERROR line each cycle; restrict to changed rows past ~50k templates.
    t_cls = class_sql("t.level")
    rows = conn.execute(f"""
        WITH per_doc AS (
          SELECT t.service, t.template_id, {t_cls} AS cls, e.doc_id, max(1 - (e.embedding <=> t.embedding)) AS sim
          FROM templates t CROSS JOIN document_errors e JOIN documents d ON d.doc_id = e.doc_id AND d.status = 'active'
          WHERE t.embedding IS NOT NULL AND {_DECIDED} AND {_SAME_SERVICE}
          GROUP BY 1, 2, 3, 4),
        ranked AS (
          SELECT *, row_number() OVER w AS rn, lead(sim) OVER w AS second FROM per_doc
          WINDOW w AS (PARTITION BY service, template_id ORDER BY sim DESC, doc_id))
        SELECT service, template_id, doc_id, sim FROM ranked
        WHERE rn = 1 AND sim >= {_by_class('cls', 'corpus')} AND sim - coalesce(second, -1) >= %(margin)s
        """, {**_params("corpus", thresholds), "margin": margin}).fetchall()
    return {(r["service"], r["template_id"]): (r["doc_id"], r["sim"]) for r in rows}


def apply_links(conn, docs_cfg) -> bool:
    """Bring inherited and corpus links in line with the current manual links and vectors.

    Layer 1 wins over layer 2, and both replace an llm or draft link (an unused draft document
    becomes rejected). Returns whether any template's document changed (the engine must reload).
    Call inside a transaction.
    """
    desired = {k: ("corpus", doc, sim, None) for k, (doc, sim) in
               desired_corpus(conn, docs_cfg.corpus_similarity, docs_cfg.corpus_margin).items()}
    desired.update({k: ("inherited", doc, sim, frm) for k, (doc, sim, frm) in
                    desired_inherited(conn, docs_cfg.inherit_similarity).items()})
    current = {(r["service"], r["template_id"]): r for r in conn.execute(
        "SELECT service, template_id, doc_id, source, similarity, inherited_from FROM template_doc_links "
        "WHERE source = ANY(%s)", (list(AUTO_SOURCES + LLM_SOURCES),))}
    # A template, or the manual source it inherits from, whose text just changed has no vector until the
    # linker embeds it again: keep its link meanwhile instead of flapping the alert to "undocumented".
    unembedded = {(r["service"], r["template_id"]) for r in conn.execute(
        "SELECT service, template_id FROM templates WHERE embedding IS NULL")}
    manual = {(r["service"], r["template_id"]): r["doc_id"] for r in conn.execute(
        "SELECT service, template_id, doc_id FROM template_doc_links WHERE source = 'manual'")}
    changed = False
    # sorted: the same lock order as the web's multi-row writes, so the two never deadlock
    for (service, tid), (source, doc, sim, frm) in sorted(desired.items()):
        cur = current.get((service, tid))
        same = cur and (cur["source"], cur["doc_id"], cur["inherited_from"]) == (source, doc, frm)
        if same and abs((cur["similarity"] or 0) - sim) < 1e-4:
            continue
        # the WHERE keeps a manual or rejected row a person wrote since we read
        written = conn.execute(
            "INSERT INTO template_doc_links (service, template_id, doc_id, source, similarity, inherited_from) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (service, template_id) DO UPDATE SET "
            "doc_id = EXCLUDED.doc_id, source = EXCLUDED.source, similarity = EXCLUDED.similarity, "
            "inherited_from = EXCLUDED.inherited_from, note = NULL, linked_at = now() "
            "WHERE template_doc_links.source NOT IN ('manual', 'rejected')",
            (service, tid, doc, source, sim, frm)).rowcount
        if not written:   # a person wrote a manual or rejected row since we read
            continue
        if cur and cur["source"] == "draft":
            conn.execute("UPDATE documents SET status = 'rejected' WHERE doc_id = %s AND status = 'draft'",
                         (cur["doc_id"],))
        changed = changed or not same
    for (service, tid), cur in sorted(current.items()):
        waiting = (service, tid) in unembedded or (
            cur["source"] == "inherited" and (service, cur["inherited_from"]) in unembedded
            and manual.get((service, cur["inherited_from"])) == cur["doc_id"])
        if cur["source"] in AUTO_SOURCES and (service, tid) not in desired and not waiting:
            conn.execute("DELETE FROM template_doc_links WHERE service = %s AND template_id = %s AND source = %s",
                         (service, tid, cur["source"]))
            changed = True
    return changed


def similar(conn, service: str, template_id: str, min_sim: float, limit: int = 200) -> list[dict]:
    """Same service and level class, most similar first (the "apply to similar" list)."""
    return conn.execute(f"""
        SELECT o.template_id, o.text, o.level, 1 - (o.embedding <=> t.embedding) AS similarity,
               l.doc_id AS current_doc_id, l.source AS current_source
        FROM templates t JOIN templates o ON o.service = t.service AND o.template_id <> t.template_id
          AND o.embedding IS NOT NULL AND {class_sql('o.level')} = {class_sql('t.level')}
        LEFT JOIN template_doc_links l ON l.service = o.service AND l.template_id = o.template_id
        WHERE t.service = %s AND t.template_id = %s AND 1 - (o.embedding <=> t.embedding) >= %s
        ORDER BY similarity DESC, o.template_id LIMIT %s""",
        (service, template_id, min_sim, limit)).fetchall()


def candidates(conn, service: str, template_id: str, k: int, min_sim: float, other_services: bool = False) -> list[dict]:
    """Active documents ranked by their best ERROR line against the template (for the LLM): of its own
    service and shared ones; with other_services, only other services' documents (references to adapt)."""
    scope = "d.service IS NOT NULL AND d.service <> t.service" if other_services else _SAME_SERVICE
    return conn.execute(f"""
        SELECT d.doc_id, d.service, d.title, d.error, d.handling, max(1 - (e.embedding <=> t.embedding)) AS similarity
        FROM templates t CROSS JOIN document_errors e JOIN documents d ON d.doc_id = e.doc_id AND d.status = 'active'
        WHERE t.service = %s AND t.template_id = %s AND t.embedding IS NOT NULL AND {scope}
        GROUP BY d.doc_id HAVING max(1 - (e.embedding <=> t.embedding)) >= %s
        ORDER BY similarity DESC, d.doc_id LIMIT %s""", (service, template_id, min_sim, k)).fetchall()


def cluster_edges(conn, keys: list[tuple[str, str]], min_sim: float) -> list[tuple[str, str, str]]:
    """(service, template_a, template_b) for pairs of the given templates at least min_sim apart."""
    if len(keys) < 2:
        return []
    rows = conn.execute("""
        WITH k AS (SELECT * FROM unnest(%s::text[], %s::text[]) AS k(service, template_id))
        SELECT a.service, a.template_id AS a, b.template_id AS b
        FROM templates a JOIN k ka USING (service, template_id)
        JOIN templates b ON b.service = a.service AND b.template_id > a.template_id
        JOIN k kb ON kb.service = b.service AND kb.template_id = b.template_id
        WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL AND 1 - (a.embedding <=> b.embedding) >= %s""",
        ([s for s, _ in keys], [t for _, t in keys], min_sim)).fetchall()
    return [(r["service"], r["a"], r["b"]) for r in rows]


def next_doc_id(conn) -> str:
    """DOC- + the next number after the highest DOC-<digits> id. Locks documents until the transaction ends,
    so two creators (web, resolver) never pick the same id."""
    conn.execute("LOCK TABLE documents IN EXCLUSIVE MODE")
    top = conn.execute("SELECT max(substring(doc_id FROM 5)::int) AS n FROM documents "
                       "WHERE doc_id ~ '^DOC-[0-9]{1,9}$'").fetchone()["n"] or 0
    return f"DOC-{top + 1:03d}"

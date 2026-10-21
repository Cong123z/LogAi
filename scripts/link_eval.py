"""Measure the automatic link layers against the manual links:
python scripts/link_eval.py [config.yaml] [--whole-docs] [--llm]

Read-only. Ground truth = every manual link with a document. Prints a Markdown report:
- layer 1 (inherit), leave-one-out: the nearest *other* manual template of the same service and level class;
- layer 2 (corpus), per threshold and margin: the best active document by ERROR lines;
- --whole-docs: layer 2 again with each document embedded whole (title + ERROR + Handling), for comparison;
- --llm: layer 3, the active LLM profile asked to confirm among each template's candidates (writes nothing;
  one LLM call per template that has candidates).
right = the document a person chose; wrong = another one; none = nothing passed.
Thresholds are the lowest values with 0 wrong; change config only on this evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai2.config import load_config  # noqa: E402
from logai2.db import connect  # noqa: E402
from logai2.embed import embed  # noqa: E402
from logai2.links import candidates, class_sql, level_class  # noqa: E402
from logai2.llm import LlmError, active_profiles  # noqa: E402
from logai2.resolver import Resolver  # noqa: E402

THRESHOLDS = (0.70, 0.75, 0.80, 0.82, 0.85, 0.88, 0.90, 0.92)
MARGINS = (0.0, 0.05, 0.10)


def truth(conn) -> list[dict]:
    return conn.execute(
        "SELECT t.service, t.template_id, t.level, t.text, t.embedding::text AS vec, l.doc_id FROM templates t "
        "JOIN template_doc_links l USING (service, template_id) "
        "WHERE l.source = 'manual' AND l.doc_id IS NOT NULL AND t.embedding IS NOT NULL").fetchall()


def nearest_manual(conn) -> dict:
    rows = conn.execute(f"""
        SELECT t.service, t.template_id, m.doc_id, m.sim FROM templates t
        JOIN template_doc_links l ON l.service = t.service AND l.template_id = t.template_id
        CROSS JOIN LATERAL (
          SELECT l2.doc_id, 1 - (s.embedding <=> t.embedding) AS sim
          FROM template_doc_links l2 JOIN templates s USING (service, template_id)
          WHERE l2.service = t.service AND l2.source = 'manual' AND l2.doc_id IS NOT NULL
            AND s.template_id <> t.template_id AND s.embedding IS NOT NULL
            AND {class_sql('s.level')} = {class_sql('t.level')}
          ORDER BY s.embedding <=> t.embedding LIMIT 1) m
        WHERE l.source = 'manual' AND l.doc_id IS NOT NULL AND t.embedding IS NOT NULL""").fetchall()
    return {(r["service"], r["template_id"]): (r["doc_id"], r["sim"]) for r in rows}


def corpus_top2(conn) -> dict:
    rows = conn.execute("""
        SELECT t.service, t.template_id, e.doc_id, max(1 - (e.embedding <=> t.embedding)) AS sim
        FROM templates t JOIN template_doc_links l USING (service, template_id)
        CROSS JOIN document_errors e JOIN documents d ON d.doc_id = e.doc_id AND d.status = 'active'
        WHERE l.source = 'manual' AND l.doc_id IS NOT NULL AND t.embedding IS NOT NULL
        GROUP BY 1, 2, 3""").fetchall()
    ranked: dict = {}
    for r in rows:
        ranked.setdefault((r["service"], r["template_id"]), []).append((r["sim"], r["doc_id"]))
    return {k: sorted(v, reverse=True)[:2] for k, v in ranked.items()}


def whole_doc_top2(conn, cfg, templates: list[dict]) -> dict:
    docs = conn.execute("SELECT doc_id, title, error, handling FROM documents WHERE status = 'active' "
                        "ORDER BY doc_id").fetchall()
    vectors = embed(cfg.embedding.url, cfg.embedding.model,
                    ["\n".join(filter(None, (d["title"], d["error"], d["handling"]))) for d in docs])
    out = {}
    for t in templates:
        tv = [float(x) for x in t["vec"].strip("[]").split(",")]
        sims = sorted(((sum(a * b for a, b in zip(tv, dv)), d["doc_id"]) for d, dv in zip(docs, vectors)), reverse=True)
        out[(t["service"], t["template_id"])] = sims[:2]
    return out


def score(templates: list[dict], pick) -> tuple[int, int, int]:
    right = wrong = none = 0
    for t in templates:
        doc = pick(t)
        if doc is None:
            none += 1
        elif doc == t["doc_id"]:
            right += 1
        else:
            wrong += 1
    return right, wrong, none


def layer2_table(title: str, templates: list[dict], top2: dict) -> list[str]:
    lines = [f"### {title}", "", "| threshold | margin | right | wrong | none |", "|---|---|---|---|---|"]
    for th in THRESHOLDS:
        for margin in MARGINS:
            def pick(t, th=th, margin=margin):
                top = top2.get((t["service"], t["template_id"])) or []
                if not top or top[0][0] < th or top[0][0] - (top[1][0] if len(top) > 1 else -1) < margin:
                    return None
                return top[0][1]
            lines.append(f"| {th:.2f} | {margin:.2f} | " + " | ".join(map(str, score(templates, pick))) + " |")
    return lines + [""]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cfg = load_config(args[0] if args else None)
    conn = connect(cfg.database.dsn)
    templates = truth(conn)
    by_class: dict[str, int] = {}
    for t in templates:
        by_class[level_class(t["level"])] = by_class.get(level_class(t["level"]), 0) + 1
    out = ["# Document link evaluation", "",
           f"Ground truth: {len(templates)} manually linked, embedded templates "
           f"({', '.join(f'{c} {n}' for c, n in sorted(by_class.items()))}), "
           f"{len({t['doc_id'] for t in templates})} documents.", "",
           "## Layer 1: inherit (leave-one-out)", "", "| threshold | right | wrong | none |", "|---|---|---|---|"]
    near = nearest_manual(conn)
    for th in THRESHOLDS:
        def pick(t, th=th):
            hit = near.get((t["service"], t["template_id"]))
            return hit[0] if hit and hit[1] >= th else None
        out.append(f"| {th:.2f} | " + " | ".join(map(str, score(templates, pick))) + " |")
    out += ["", "## Layer 2: corpus", ""]
    out += layer2_table("ERROR lines only (as the linker matches)", templates, corpus_top2(conn))
    if "--whole-docs" in sys.argv:
        out += layer2_table("Whole documents (title + ERROR + Handling), for comparison", templates,
                            whole_doc_top2(conn, cfg, templates))
    if "--llm" in sys.argv:
        out += llm_table(conn, cfg, templates)
    print("\n".join(out))


def llm_table(conn, cfg, templates: list[dict]) -> list[str]:
    profiles = active_profiles(conn)
    if not profiles:
        return ["## Layer 3: LLM", "", "No active LLM profile.", ""]
    resolver = Resolver(cfg, conn)
    resolver.profiles = profiles
    right = wrong = none = failed = no_candidates = 0
    for t in templates:
        docs = candidates(conn, t["service"], t["template_id"], cfg.llm.candidates, cfg.llm.candidate_similarity)
        if not docs:
            no_candidates += 1
            continue
        try:
            pick, _ = resolver.confirm(t, docs)
        except LlmError:
            failed += 1
            continue
        if pick is None:
            none += 1
        elif pick["doc_id"] == t["doc_id"]:
            right += 1
        else:
            wrong += 1
    names = ", ".join(f"`{p['name']}` ({p['model']})" for p in profiles)
    return ["## Layer 3: LLM confirm", "", f"Profiles in order: {names}; candidates ≥ "
            f"{cfg.llm.candidate_similarity}, top {cfg.llm.candidates}.", "",
            "| right | wrong | none | failed | no candidates |", "|---|---|---|---|---|",
            f"| {right} | {wrong} | {none} | {failed} | {no_candidates} |", ""]


if __name__ == "__main__":
    main()

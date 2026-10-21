from pathlib import Path

import yaml

from scripts.split_doc_errors import apply_split

SPLIT = Path(__file__).resolve().parent.parent / "docs/plans/2026-10-21-corpus-error-split.yaml"


def test_split_only_documents_still_as_seeded(conn):
    conn.execute("INSERT INTO documents (doc_id, title, handling, revision) VALUES "
                 "('DOC-001', 't', 'LixiHandler entered Receiving subscriber incorrect. The subscriber typed an "
                 "invalid receiving number in the LIXI gift-money USSD menu (*136#).', 'r'), "
                 "('DOC-011', 't', 'con-<*>-Receiver type group', 'r'), "
                 "('DOC-004', 't', 'edited by hand', 'r'), ('DOC-016', 't', 'Check whether...', 'r')")
    results = dict(apply_split(conn, yaml.safe_load(SPLIT.read_text())))
    assert results["DOC-001"] == results["DOC-011"] == "split"
    assert results["DOC-004"].startswith("unchanged") and results["DOC-016"] == "skipped: no ERROR line"
    rows = {r["doc_id"]: r for r in conn.execute("SELECT doc_id, error, handling, revision FROM documents")}
    assert rows["DOC-001"]["error"] == "LixiHandler entered Receiving subscriber incorrect"
    assert rows["DOC-001"]["handling"].startswith("The subscriber typed") and rows["DOC-001"]["revision"] != "r"
    assert (rows["DOC-011"]["error"], rows["DOC-011"]["handling"]) == ("con-<*>-Receiver type group", "")
    assert rows["DOC-016"]["handling"] == "Check whether..." and rows["DOC-004"]["revision"] == "r"
    assert dict(apply_split(conn, yaml.safe_load(SPLIT.read_text())))["DOC-001"].startswith("unchanged")

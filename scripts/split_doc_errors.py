"""Split seeded documents into ERROR + Handling: python scripts/split_doc_errors.py SPLIT.yaml [config.yaml]

SPLIT.yaml maps doc_id -> {error, handling} (docs/plans/2026-10-21-corpus-error-split.yaml). A document
is changed only while it is still exactly as seeded: ERROR empty and Handling equal to the original
text ("<error>. <handling>"). Anything edited since is left alone. Prints one line per document.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai2.config import load_config  # noqa: E402
from logai2.db import connect  # noqa: E402


def apply_split(conn, entries: dict) -> list[tuple[str, str]]:
    results = []
    for doc_id, e in entries.items():
        error, handling = (e.get("error") or "").strip(), (e.get("handling") or "").strip()
        if not error:
            results.append((doc_id, "skipped: no ERROR line"))
            continue
        original = f"{error}. {handling}" if handling else error
        changed = conn.execute(
            "UPDATE documents SET error = %s, handling = %s, revision = %s, updated_at = now() "
            "WHERE doc_id = %s AND error = '' AND handling = %s",
            (error, handling, uuid.uuid4().hex, doc_id, original)).rowcount
        results.append((doc_id, "split" if changed else "unchanged: missing or edited since seeding"))
    return results


def main() -> None:
    entries = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
    cfg = load_config(sys.argv[2] if len(sys.argv) > 2 else None)
    conn = connect(cfg.database.dsn)
    with conn.transaction():
        for doc_id, outcome in apply_split(conn, entries):
            print(f"{doc_id}: {outcome}")


if __name__ == "__main__":
    main()

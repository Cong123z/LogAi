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

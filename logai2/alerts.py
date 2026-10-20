"""Group non-NORMAL alert_state rows into what the operator sees. Pure; used by owner and web."""
from __future__ import annotations

RANK = {"ALERTING": 3, "COOLING": 2, "WARMING": 1, "NORMAL": 0}


def alert_rows(states: list[dict], links: dict[tuple[str, str], str], titles: dict[str, str],
               texts: dict[tuple[str, str], str]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for s in states:
        if s["state"] == "NORMAL":
            continue
        svc, key = s["service"], s["key"]
        if key.startswith("*"):
            g = (svc, "service", None)
        elif key.startswith("doc:"):
            g = (svc, "document", key[4:])
        elif (svc, key) in links:
            g = (svc, "document", links[(svc, key)])
        else:
            g = (svc, "template", key)
        groups.setdefault(g, []).append(s)

    rows = []
    for (svc, kind, ident), members in groups.items():
        doc_id = ident if kind == "document" else None
        rows.append({
            "service": svc, "kind": kind, "doc_id": doc_id,
            "title": titles.get(doc_id) if doc_id else None,
            "state": max((m["state"] for m in members), key=RANK.__getitem__),
            "since": min(m["since"] for m in members),
            "templates": [{"template_id": m["key"], "text": texts.get((svc, m["key"])), "state": m["state"],
                           "count": m["count"], "band_low": m["band_low"], "band_high": m["band_high"]}
                          for m in members if not m["key"].startswith(("*", "doc:"))],
            "keys": [m["key"] for m in members],
        })
    rows.sort(key=lambda r: (-RANK[r["state"]], r["since"]))
    return rows

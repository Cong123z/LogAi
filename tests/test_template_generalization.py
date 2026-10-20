"""The exposed template must be Drain3's generalised (`<*>`) text, not the raw
first line of the cluster.

Copied from logai-engine's test_template_generalization.py. There the bug was
the registry keeping the raw first-event text; here the owner stores whatever
``Miners.match`` returns, so the guard is that a later match of the same
cluster returns the converged template under the same id.
"""
from __future__ import annotations

from logai2.owner.miners import Miners


def test_match_returns_generalized_template(cfg):
    m = Miners(cfg.drain3, {})
    first = m.match("auth", "User alice logged in from <*>")
    # The first event of a cluster has nothing to generalise against yet.
    assert first == ("auth:T00001", "User alice logged in from <*>", True)
    second = m.match("auth", "User bob logged in from <*>")
    assert second == ("auth:T00001", "User <*> logged in from <*>", False)
    assert "alice" not in second[1] and "bob" not in second[1]


def test_generalized_template_survives_restore(cfg):
    m = Miners(cfg.drain3, {})
    m.match("auth", "User alice logged in from <*>")
    m.match("auth", "User bob logged in from <*>")
    m2 = Miners(cfg.drain3, m.dirty())
    assert m2.match("auth", "User carol logged in from <*>") == (
        "auth:T00001", "User <*> logged in from <*>", False)

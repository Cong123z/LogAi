"""A large HDBSCAN fit runs in a child process: the engine's other threads
(retrain heartbeat, metrics) keep running, and the caller gets wait reports."""
from __future__ import annotations

import threading
import time

from logai.config import ClusteringConfig


def test_large_fit_runs_in_child_and_reports_while_waiting(real_numpy, monkeypatch):
    import logai.clustering.hdbscan_cluster as clustering
    monkeypatch.setattr(clustering, "np", real_numpy)
    monkeypatch.setattr(clustering, "SUBPROCESS_MIN_TEMPLATES", 2)
    monkeypatch.setattr(clustering, "WAIT_REPORT_SECONDS", 0.05)

    rng = real_numpy.random.default_rng(0)
    axes = real_numpy.eye(1024)[:3] * 10
    X = real_numpy.vstack([axes[i] + 0.01 * rng.normal(size=(20, 1024)) for i in range(3)])
    ids = [f"T{i}" for i in range(len(X))]

    ticks, done = [], threading.Event()

    def heartbeat():
        while not done.wait(0.05):
            ticks.append(time.time())

    threading.Thread(target=heartbeat, daemon=True).start()
    waits = []
    labels = clustering.GroupClusterer(ClusteringConfig()).cluster(ids, X, on_wait=waits.append)
    done.set()

    blobs = [{labels[t] for t in ids[i * 20:(i + 1) * 20]} for i in range(3)]
    assert all(len(b) == 1 and -1 not in b for b in blobs)  # each blob is one cluster
    assert len({next(iter(b)) for b in blobs}) == 3          # and the three differ
    assert waits and waits == sorted(waits)                  # reported while waiting
    assert len(ticks) >= 2                                   # other threads kept running

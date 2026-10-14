"""Training at scale: the compact feature pass and the sampled global model.

A. Logs flow as expected: same vectors as the old per-record path, each event
   once, sampling keeps quiet windows, newest-first range, empty input.
B. Memory of the feature pass does not grow per event beyond the timestamps.
C. Model quality: a model fitted on the sample scores held-out traffic like the
   model fitted on every vector. Runs in a clean interpreter, because other
   test modules replace numpy/sklearn in sys.modules with stubs.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from array import array
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logai.collector.es_collector import ElasticsearchCollector  # noqa: E402
from logai.config import AlertConfig, AnomalyConfig, FeatureConfig, TrainingConfig  # noqa: E402
from logai.features.feature_engine import FeatureEngine  # noqa: E402
from logai.models import ParsedEvent, RawLog  # noqa: E402
from logai.storage.training_event_index import TrainingEventIndex  # noqa: E402


def _tp():
    """train_pipeline, imported only when a test runs: other test modules stub
    numpy in sys.modules at collection time and must load it first."""
    from logai.training import train_pipeline
    return train_pipeline

T0 = 1_700_000_000.0


def _event(event_id, template_id, service, ts):
    raw = RawLog(timestamp=ts, service=service, level="INFO", message="m", event_id=event_id)
    return ParsedEvent(raw=raw, template_id=template_id, template=f"tpl {template_id}",
                       parameters=[], is_new_template=False)


def _features(windows, sample_size=10**9, seed=42):
    return _tp().sample_window_features(windows, FeatureConfig(), sample_size, seed)


def _old_vectors(records, template_to_group, config):
    """Phases 6-7 as they were: the whole index unique by event id (the old
    Phase 1 full-set dedup), sorted in memory, one engine per window."""
    seen, unique = set(), []
    for record in records:
        if record["event_id"] not in seen:
            seen.add(record["event_id"])
            unique.append(record)
    grouped = defaultdict(list)
    for record in sorted(unique, key=lambda r: float(r["timestamp"])):
        gid = template_to_group.get(record["template_id"])
        if gid:
            grouped[(record["service"] or "unknown", gid)].append(float(record["timestamp"]))
    origin = min((ts[0] for ts in grouped.values() if ts), default=None)
    out = {}
    for key, timestamps in grouped.items():
        engine = FeatureEngine(config, origin=origin)
        out[key] = [(t, engine.update(key, t).as_vector()) for t in timestamps]
    return out


class TestLogsFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.index = TrainingEventIndex(f"{self.tmp}/events.jsonl")
        # `_valid_records` only needs the index, so it runs without a full pipeline.
        self.fake_pipeline = SimpleNamespace(event_index=self.index)

    def valid_records(self):
        return _tp().TrainingPipeline._valid_records(self.fake_pipeline)

    def test_same_vectors_as_old_path(self):
        """Out-of-order times, duplicate ids, two services in one group, an
        ungrouped template and a timestamp the collector read as "now"."""
        rng = random.Random(7)
        events, n = [], 0
        for i in range(3000):
            ts = T0 + i * 0.7 + rng.uniform(-3, 3)  # jitter => out of order
            service = ("auth", "payment", "order")[i % 3]
            template = ("T1", "T2", "T3", "T_UNGROUPED")[i % 4]
            events.append(_event(f"e{n}", template, service, ts))
            n += 1
            if i % 50 == 0:  # replayed duplicate a few events later
                events.append(_event(f"e{n - 1}", template, service, ts))
        events.insert(1500, _event("now", "T1", "auth", T0 + 10**7))  # fallback "now"
        self.index.append_batch(events)
        template_to_group = {"T1": "G1", "T2": "G1", "T3": "G2"}  # T1+T2 share G1

        windows = _tp().bucket_window_timestamps(self.valid_records(), template_to_group)
        kept, scored = _features(windows)
        new = defaultdict(list)
        for fv in kept:
            new[fv.group_id].append((fv.timestamp, fv.as_vector()))

        old = _old_vectors(list(self.index.records()), template_to_group, FeatureConfig())
        self.assertEqual(dict(new), old)
        self.assertEqual(scored, sum(len(v) for v in old.values()))
        self.assertEqual(scored, len(kept))
        self.assertTrue(all(list(ts) == sorted(ts) for ts in windows.values()))

    def test_each_event_counted_once(self):
        self.index.append_batch([_event("a", "T1", "s", T0), _event("b", "T1", "s", T0 + 1)])
        self.index.append_batch([_event("b", "T1", "s", T0 + 1), _event("c", "T1", "s", T0 + 2)])
        ids = [r["event_id"] for r in self.valid_records()]
        self.assertEqual(ids, ["a", "b", "c"])

    def test_recent_event_ids_is_the_index_tail(self):
        self.index.append_batch([_event(f"e{i}", "T1", "s", T0 + i) for i in range(10)])
        self.assertEqual(self.index.recent_event_ids(3), ["e7", "e8", "e9"])

    def test_sampling_is_uniform_capped_and_reproducible(self):
        busy, quiet = ("busy", "G1"), ("quiet", "G2")
        windows = {
            busy: array("d", (T0 + i * 0.05 for i in range(50_000))),
            quiet: array("d", (T0 + i * 6 for i in range(500))),
        }
        kept, scored = _features(windows, sample_size=2000)
        per_window = defaultdict(int)
        for fv in kept:
            per_window[fv.group_id] += 1
        self.assertEqual(scored, 50_500)
        self.assertEqual(len(kept), 2000)
        # Volume-weighted, like the traffic realtime scores (~1% quiet).
        self.assertAlmostEqual(per_window[quiet] / len(kept), 500 / 50_500, delta=0.006)

        again, _ = _features(windows, sample_size=2000)
        self.assertEqual([fv.timestamp for fv in again], [fv.timestamp for fv in kept])

    def test_small_budget_is_exact_so_the_model_is_always_fitted(self):
        """Bernoulli sampling kept 20-45 of 30 (under min_training_samples
        in 16 of 40 seeds); selection sampling keeps exactly the budget."""
        windows = {("s", "G"): array("d", (T0 + i * 0.1 for i in range(20_000)))}
        for seed in range(40):
            self.assertEqual(len(_features(windows, sample_size=30, seed=seed)[0]), 30)

    def test_sample_is_uniform_over_the_stream(self):
        windows = {("s", "G"): array("d", (T0 + i for i in range(10_000)))}
        positions = []
        for seed in range(20):
            positions += [fv.timestamp - T0 for fv in _features(windows, sample_size=500, seed=seed)[0]]
        quarters = [sum(q * 2500 <= p < (q + 1) * 2500 for p in positions) for q in range(4)]
        for count in quarters:  # 2500 expected per quarter
            self.assertAlmostEqual(count, 2500, delta=250)

    def test_everything_kept_when_events_fit(self):
        windows = {("a", "G"): array("d", [T0, T0 + 1]), ("b", "G"): array("d", [T0 + 2])}
        kept, scored = _features(windows, sample_size=3)
        self.assertEqual((len(kept), scored), (3, 3))

    def test_train_logs_counts_and_handles_no_grouped_events(self):
        self.index.append_batch([_event(f"e{i}", "T1", "s", T0 + i) for i in range(40)])
        config = SimpleNamespace(
            training=TrainingConfig(feature_sample_size=10),
            anomaly=AnomalyConfig(min_training_samples=5),
            features=FeatureConfig(),
        )
        model = MagicMock()
        model.train.return_value = True
        fake = SimpleNamespace(
            config=config, anomaly_model=model, _valid_records=self.valid_records,
            progress=_tp().TrainingProgress(),
        )
        with self.assertLogs("logai.training", "INFO") as logs:
            _tp().TrainingPipeline._train_anomaly_model(fake, {"T1": "G1"})
        self.assertIn("scored 40 events across 1 (service, group) windows", "\n".join(logs.output))
        self.assertAlmostEqual(len(model.train.call_args[0][0]), 10, delta=8)

        model.train.return_value = False
        with self.assertLogs("logai.training", "WARNING"):
            _tp().TrainingPipeline._train_anomaly_model(fake, {})  # nothing grouped
        self.assertEqual(model.train.call_args[0][0], [])


class TestNewestStart(unittest.TestCase):
    """`newest_start` against a fake ES that counts real document times."""

    def start(self, doc_times, max_docs, start_ts=T0, end_ts=T0 + 86400):
        from datetime import datetime

        queries = []

        def search(body):
            queries.append(body)
            assert body["size"] == 0 and body["track_total_hits"] is True
            rng = body["query"]["range"]["@timestamp"]
            gte = datetime.fromisoformat(rng["gte"]).timestamp()
            lte = datetime.fromisoformat(rng["lte"]).timestamp()
            return {"hits": {"total": {"value": sum(gte <= t <= lte for t in doc_times)}}}

        result = ElasticsearchCollector.newest_start(
            SimpleNamespace(_search=search), start_ts, end_ts, max_docs
        )
        self.assertLess(len(queries), 40)
        return result

    def kept(self, doc_times, start):
        return sum(t >= start for t in doc_times)

    def test_whole_range_fits(self):
        docs = [T0 + i * 60 for i in range(100)]
        self.assertEqual(self.start(docs, 1000), T0)

    def test_keeps_the_newest_documents_up_to_the_cap(self):
        docs = [T0 + i * 7.3 for i in range(10_000)]
        start = self.start(docs, 2500)
        self.assertLessEqual(self.kept(docs, start), 2500)
        self.assertGreaterEqual(self.kept(docs, start), 2500 - 1)  # 1 s precision, 1 doc / 7.3 s
        self.assertGreater(start, docs[-2600])

    def test_burst_older_than_the_newest_hours(self):
        """Found on real hdfs-logs: one hour held 6.3M docs, more than the
        whole 5M budget. Hourly buckets then started after the burst and read
        only ~1M; the start must instead fall inside the burst."""
        burst = [T0 + 3600 + i * 0.01 for i in range(60_000)]   # 10 min, very dense
        quiet = [T0 + 7200 + i * 5 for i in range(1_000)]       # newer, sparse
        docs = burst + quiet
        start = self.start(docs, 20_000)
        kept = self.kept(docs, start)
        self.assertLessEqual(kept, 20_000)
        self.assertGreaterEqual(kept, 20_000 - 100)  # one second of the burst
        self.assertTrue(burst[0] < start < burst[-1])

    def test_error_keeps_the_range_start(self):
        fake = SimpleNamespace(_search=MagicMock(side_effect=RuntimeError("boom")))
        self.assertEqual(ElasticsearchCollector.newest_start(fake, T0, T0 + 99, 10), T0)


class TestMemory(unittest.TestCase):
    def peak_bytes(self, n_events):
        """Peak traced memory of Phases 6-7 for `n_events` across 20 windows,
        records generated lazily as the event index streams them."""
        def records():
            for i in range(n_events):
                yield {"event_id": f"e{i}", "template_id": f"T{i % 20}",
                       "service": "svc", "timestamp": T0 + i * 0.01}
        template_to_group = {f"T{i}": f"G{i}" for i in range(20)}
        tracemalloc.start()
        try:
            windows = _tp().bucket_window_timestamps(records(), template_to_group)
            kept, _ = _features(windows, sample_size=2000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(len(kept), 2300)
        return peak

    def test_feature_pass_memory_is_about_the_timestamps(self):
        small, large = self.peak_bytes(40_000), self.peak_bytes(160_000)
        per_extra_event = (large - small) / 120_000
        # 8 B/event of timestamps (+ array growth slack). The old path kept a
        # dict per record and a FeatureVector per event: ~1.5 KB/event.
        self.assertLess(per_extra_event, 40, f"{per_extra_event:.1f} B/event")


# --- C. model quality ---------------------------------------------------------

def _poisson_base(rng, start, end, rate):
    t, out = start, []
    while True:
        t += rng.expovariate(rate)
        if t >= end:
            return out
        out.append(t)


def _traffic(seed, start, minutes, inject=None, scale=1.0):
    """Event times for four kinds of normal windows, plus an optional
    injected anomaly {"window", "at", "kind"} (minutes from start)."""
    import math

    rng = random.Random(seed)

    def _poisson(rng, start, end, rate):  # every Poisson rate x scale
        return _poisson_base(rng, start, end, rate * scale)

    end = start + minutes * 60
    busy = _poisson(rng, start, end, 10.0)
    periodic = [t for t in _poisson(rng, start, end, 3.5)
                if rng.random() < (2 + 1.5 * math.sin(2 * math.pi * (t - start) / 1800)) / 3.5]
    quiet = _poisson(rng, start, end, 1 / 60)
    bursty = _poisson(rng, start, end, 0.2)
    for burst in range(int(start) + 150, int(end) - 10, 300):
        bursty += [burst + rng.uniform(0, 10) for _ in range(100)]
    windows = {("busy", "G1"): busy, ("periodic", "G2"): periodic,
               ("quiet", "G3"): quiet, ("bursty", "G4"): bursty}
    span = None
    if inject:
        at = start + inject["at"] * 60
        if inject["kind"] == "busy_burst":      # 10x for one minute
            span = (at, at + 60)
            busy += _poisson(rng, at, at + 60, 90.0)
        elif inject["kind"] == "stop_flood":    # silent 2 min, then 5x for one minute
            span = (at + 120, at + 180)
            windows[("busy", "G1")] = busy = (
                [t for t in busy if not at <= t < at + 180] + _poisson(rng, at + 120, at + 180, 50.0)
            )
        elif inject["kind"] == "quiet_burst":   # 30 events in 10 s
            span = (at, at + 10)
            quiet += [at + rng.uniform(0, 10) for _ in range(30)]
    return {k: array("d", sorted(v)) for k, v in windows.items()}, span


# Quality gate. Isolation Forest is itself noisy: refitting the FULL vector
# set with another random_state moved the threshold by up to 0.02 and, on
# marginal traffic, turned 85% detection of a burst into 0%. So a sampled model
# is compared with the full model fitted with the SAME random_state (paired),
# on traffic where the full model detects reliably (rates x5, ~440k events).
SCALE = 5
SAMPLED_SIZE = 20_000   # 4.5% of the events, below the 100k production default
HUNDREDS = 500          # the "train on a few hundred vectors" idea, for contrast
SEEDS = (0, 1, 2)
SCENARIOS = (("busy_burst", ("busy", "G1")), ("stop_flood", ("busy", "G1")),
             ("quiet_burst", ("quiet", "G3")))


def _quality_report():
    """Runs in a clean interpreter (see TestModelQuality)."""
    from logai.alert.alert_state_machine import AlertStateMachine
    from logai.anomaly.isolation_forest_model import GlobalAnomalyModel
    from logai.storage.base import ModelStore

    alert_config = AlertConfig(warm_consecutive=10, alert_consecutive=30, cool_consecutive=10,
                               score_high=0.65, score_low=0.52, min_events_1m=150)  # config.yaml
    high = alert_config.score_high
    tmp = tempfile.mkdtemp()

    def fit(vectors, random_state):
        model = GlobalAnomalyModel(AnomalyConfig(random_state=random_state),
                                   ModelStore(f"{tmp}/{random.random()}"))
        assert model.train(vectors)
        return model

    class Store(dict):
        def all(self):
            return dict(self)

        def bulk_set(self, values):
            self.update(values)

    train_windows, _ = _traffic(seed=1, start=T0, minutes=120, scale=SCALE)
    full_vectors, n_events = _features(train_windows)
    models = {}
    for seed in SEEDS:
        models[f"full_{seed}"] = fit(full_vectors, seed)
        models[f"sampled_{seed}"] = fit(_features(train_windows, SAMPLED_SIZE, seed)[0], seed)
        models[f"hundreds_{seed}"] = fit(random.Random(seed).sample(full_vectors, HUNDREDS), seed)

    def evaluate(windows, span=None, window=None):
        vectors, _ = _features(windows)  # every event, as realtime scores it
        # Skip the first 30 min: the 30-min baseline is still filling.
        warm_from = min(ts[0] for ts in windows.values()) + 1800
        vectors = [v for v in vectors if v.timestamp >= warm_from]
        out = {}
        for name, model in models.items():
            results = sorted(model.predict_batch(vectors), key=lambda r: r.timestamp)
            machine = AlertStateMachine(alert_config, Store())
            alerted = {"/".join(s.group_id) for s in machine.transition_batch(results)
                       if s.alert_state == "ALERTING"}
            row = {"alerted": sorted(alerted)}
            if span is None:
                row["fp_rate"] = sum(r.anomaly_score >= high for r in results) / len(results)
            else:
                hit = [r for r in results if r.group_id == window and span[0] <= r.timestamp < span[1]]
                row["detection"] = sum(r.anomaly_score >= high for r in hit) / len(hit)
            out[name] = row
        return out

    report = {
        "events": n_events,
        "offset": {name: float(model._model.offset_) for name, model in models.items()},
        "normal": evaluate(_traffic(seed=2, start=T0 + 86400, minutes=60, scale=SCALE)[0]),
    }
    for kind, window in SCENARIOS:
        windows, span = _traffic(seed=3, start=T0 + 2 * 86400, minutes=75,
                                 inject={"kind": kind, "at": 60}, scale=SCALE)
        report[kind] = evaluate(windows, span, window)
    return report


class TestModelQuality(unittest.TestCase):
    """A model fitted on a sample must behave like the model fitted on every
    vector with the same random_state."""

    @classmethod
    def setUpClass(cls):
        child = subprocess.run(
            [sys.executable, __file__, "--quality-report"],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        if child.returncode != 0:
            raise AssertionError(child.stderr[-3000:])
        cls.report = json.loads(child.stdout.strip().splitlines()[-1])

    def mean(self, section, kind, key):
        return sum(self.report[section][f"{kind}_{seed}"][key] for seed in SEEDS) / len(SEEDS)

    def test_full_model_is_a_sound_reference(self):
        # The busy window alerts on its injected anomalies only. (The bursty
        # window's regular bursts reach min_events_1m at this scale and alert
        # under every model alike: a global-model trait, not a sampling one.)
        for seed in SEEDS:
            self.assertNotIn("busy/G1", self.report["normal"][f"full_{seed}"]["alerted"])
            for scenario in ("busy_burst", "stop_flood"):
                self.assertIn("busy/G1", self.report[scenario][f"full_{seed}"]["alerted"])

    def test_sampled_alerts_exactly_like_full(self):
        for section in ("normal", "busy_burst", "stop_flood", "quiet_burst"):
            for seed in SEEDS:
                self.assertEqual(self.report[section][f"sampled_{seed}"]["alerted"],
                                 self.report[section][f"full_{seed}"]["alerted"], f"{section} {seed}")

    def test_no_more_false_positives_than_full(self):
        self.assertLessEqual(self.mean("normal", "sampled", "fp_rate"),
                             self.mean("normal", "full", "fp_rate") + 0.005)

    def test_detects_bursts_like_full(self):
        for scenario in ("busy_burst", "stop_flood"):
            self.assertGreaterEqual(self.mean(scenario, "sampled", "detection"),
                                    self.mean(scenario, "full", "detection") - 0.10, scenario)

    def test_threshold_stays_close_to_full(self):
        offsets = self.report["offset"]

        def drift(kind):
            return sum(abs(offsets[f"{kind}_{s}"] - offsets[f"full_{s}"]) for s in SEEDS) / len(SEEDS)

        self.assertLess(drift("sampled"), 0.01)
        # Why a few hundred vectors is not enough: the threshold wanders more.
        self.assertGreater(drift("hundreds"), 2 * drift("sampled"))


if __name__ == "__main__" and "--quality-report" in sys.argv:
    print(json.dumps(_quality_report()))

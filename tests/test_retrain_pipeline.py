"""Two training runs on one data dir: existing groups are frozen (same
members, same IDs); only templates without a group are placed, into the
nearest existing group or into new groups."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from logai.config import AppConfig
from logai.models import ParsedEvent, RawLog, TemplateState
from logai.training.train_pipeline import TrainingPipeline

DAY = 86400.0


@pytest.fixture(autouse=True)
def _use_real_numpy(real_numpy, monkeypatch):
    # Other test modules stub numpy at import; nearest-group placement needs
    # real dot products.
    import logai.clustering.hdbscan_cluster as clustering
    import logai.training.train_pipeline as training
    monkeypatch.setattr(clustering, "np", real_numpy)
    monkeypatch.setattr(training, "np", real_numpy)
# Unit vectors: templates on the same axis are "similar" (dot 1.0 >= 0.88).
AXIS = {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0]}


class TestRetrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.tmp
        self.cfg.storage.model_dir = f"{self.tmp}/models"
        self.cfg.drain3.persistence_path = f"{self.tmp}/drain3_state.bin"
        self.cfg.doc_matcher.corpus_path = f"{self.tmp}/documentation_corpus.json"
        self.cfg.doc_matcher.overrides_path = f"{self.tmp}/documentation_overrides.json"
        self.cfg.doc_matcher.status_path = f"{self.tmp}/documentation_status.json"
        self.embedded: list[str] = []
        self.axis: dict[str, str] = {}  # template id -> axis of its embedding

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _train(self, events: dict[str, list[float]], labels: dict[str, int]) -> TrainingPipeline:
        """events: template_id -> timestamps; labels: HDBSCAN label per template
        (only consulted for templates HDBSCAN is actually asked about)."""
        p = TrainingPipeline(self.cfg)

        def embed(texts):
            self.embedded.extend(texts)
            return [AXIS[self.axis[t.removeprefix("text of ")]] for t in texts]

        p.embedder.embed = MagicMock(side_effect=embed)
        p.clusterer.cluster = MagicMock(side_effect=lambda ids, _m: {t: labels[t] for t in ids})
        # Matcher unavailable: the case where documentation carried over by ID
        # used to leak to an unrelated group.
        p.doc_matcher = MagicMock(ready=False, last_error="off", reload=MagicMock(return_value=False))
        p.anomaly_model.train = MagicMock(return_value=True)
        p._generalized_template_text = lambda tid: None
        p.parser.parse = MagicMock(side_effect=lambda raw: ParsedEvent(
            raw=raw, template_id=raw.message, template=f"text of {raw.message}",
            parameters=[], is_new_template=False,
        ))
        logs = [
            RawLog(event_id=f"{tid}-{ts}", timestamp=ts, service="svc", level="INFO", message=tid)
            for tid, stamps in events.items() for ts in stamps
        ]
        p.run(logs)
        self.clustered = [call.args[0] for call in p.clusterer.cluster.call_args_list]
        return p

    def _group_of(self, template_id: str) -> str:
        return TrainingPipeline(self.cfg).template_registry.get(template_id).group_id

    def _lineage(self) -> dict:
        return json.loads(Path(self.tmp, "group_lineage.json").read_text())

    def test_existing_groups_frozen_new_templates_added_or_new_group(self):
        self.axis.update(A="x", B="x", C="y", D="y", E="x", F="z", G="z")
        self._train({t: [1.0] for t in "ABCD"}, {"A": 0, "B": 0, "C": 1, "D": 1})
        first = {t: self._group_of(t) for t in "ABCD"}
        self.assertEqual(len(set(first.values())), 2)
        self.embedded.clear()

        # D absent from the window; E is like A/B; F, G are like nothing known.
        # HDBSCAN would now lump everything together - it must not be asked.
        self._train({"A": [2.0], "B": [2.0], "C": [2.0], "E": [2.0], "F": [2.0], "G": [2.0]},
                    {"F": 0, "G": 0})
        for t in "ABCD":
            self.assertEqual(self._group_of(t), first[t])  # same group, same ID
        self.assertEqual(self._group_of("E"), first["A"])  # added to an old group
        self.assertEqual(self._group_of("F"), self._group_of("G"))
        self.assertNotIn(self._group_of("F"), first.values())
        self.assertEqual(self.clustered, [["F", "G"]])  # only unplaced templates
        self.assertEqual(sorted(self.embedded), ["text of E", "text of F", "text of G"])
        lineage = self._lineage()
        self.assertEqual(lineage["added"], {first["A"]: ["E"]})
        self.assertEqual(list(lineage["new"].values()), [["F", "G"]])

    def test_pending_template_from_realtime_is_placed_without_new_embedding(self):
        self.axis.update(A="x", B="x", P="x")
        self._train({"A": [1.0], "B": [1.0]}, {"A": 0, "B": 0})
        registry = TrainingPipeline(self.cfg).template_registry
        registry.upsert(TemplateState("P", "text of P", "svc", last_seen=1.5,
                                      group_id="UNASSIGNED_PENDING"))
        registry.set_embedding("P", AXIS["x"])
        self.embedded.clear()
        self._train({"A": [2.0]}, {})
        self.assertEqual(self._group_of("P"), self._group_of("A"))
        self.assertEqual(self.embedded, [])

    def test_documentation_stays_with_group(self):
        self.axis.update(A="x", B="x", C="y", D="y")
        p = self._train({t: [1.0] for t in "ABCD"}, {"A": 0, "B": 0, "C": 1, "D": 1})
        gid_ab = self._group_of("A")
        group = p.group_registry.get(gid_ab)
        group.documented, group.documentation_id = True, "DOC-AB"
        p.group_registry.upsert(group)

        p = self._train({t: [2.0] for t in "ABCD"}, {})
        self.assertEqual(p.group_registry.get(self._group_of("A")).documentation_id, "DOC-AB")
        self.assertIsNone(p.group_registry.get(self._group_of("C")).documentation_id)

    def test_ttl_never_prunes_grouped_templates_only_ungrouped(self):
        self.axis.update(A="x", B="x", P="y")
        self._train({"A": [0.0], "B": [0.0]}, {"A": 0, "B": 0})
        registry = TrainingPipeline(self.cfg).template_registry
        registry.upsert(TemplateState("P", "text of P", "svc", last_seen=0.0,
                                      group_id="UNASSIGNED_PENDING"))
        self._train({"A": [40 * DAY]}, {})
        ids = {t.template_id for t in TrainingPipeline(self.cfg).template_registry.all_templates()}
        self.assertEqual(ids, {"A", "B"})  # B unseen 40 days but grouped: kept

    def test_new_ids_never_reuse_and_noise_gets_single_group(self):
        self.axis.update(A="x", B="x", N="z")
        self._train({"A": [1.0], "B": [1.0]}, {"A": 0, "B": 0})
        first = self._group_of("A")
        self._train({"A": [2.0], "N": [2.0]}, {"N": -1})
        self.assertRegex(self._group_of("N"), r"^G\d{4}$")
        self.assertGreater(self._group_of("N"), first)
        self.assertEqual(self._lineage()["new"], {self._group_of("N"): ["N"]})


class TestRealtimeDropsVanishedGroups(unittest.TestCase):
    def test_state_of_groups_gone_after_retrain_is_dropped_on_startup(self):
        from logai.models import GroupState
        from logai.realtime.realtime_pipeline import RealtimePipeline
        from logai.storage.base import JSONStore
        from logai.storage.registries import GroupRegistry

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = AppConfig()
        cfg.storage.base_dir = tmp
        cfg.storage.model_dir = f"{tmp}/models"
        cfg.drain3.persistence_path = f"{tmp}/drain3_state.bin"
        cfg.doc_matcher.corpus_path = f"{tmp}/missing.yaml"
        GroupRegistry(cfg.storage).upsert(GroupState(group_id="G0001"))
        alerts = JSONStore(f"{tmp}/{cfg.storage.anomaly_state_file}")
        alerts.set('["svc", "G0001"]', {"group_id": ["svc", "G0001"], "state": "ALERTING"})
        alerts.set('["svc", "G0007"]', {"group_id": ["svc", "G0007"], "state": "ALERTING"})
        JSONStore(f"{tmp}/{cfg.storage.incident_analysis_file}").set(
            '["svc", "G0007"]', {"service": "svc", "group_id": "G0007"}
        )

        RealtimePipeline(cfg)

        self.assertEqual(
            set(JSONStore(f"{tmp}/{cfg.storage.anomaly_state_file}").all()), {'["svc", "G0001"]'}
        )
        self.assertEqual(JSONStore(f"{tmp}/{cfg.storage.incident_analysis_file}").all(), {})

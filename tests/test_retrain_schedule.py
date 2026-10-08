"""Retrain schedule: next-run math, store, engine trigger/run, web API, health."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from logai.config import AppConfig
from logai.storage.retrain_schedule import (
    RetrainScheduleConflict,
    RetrainScheduleError,
    RetrainScheduleStore,
    next_run_at,
    validate_schedule,
)

UTC = ZoneInfo("UTC")


def ts(year, month, day, hour=0, minute=0, zone=UTC):
    return datetime(year, month, day, hour, minute, tzinfo=zone).timestamp()


def schedule(**overrides):
    value = {
        "enabled": True, "time": "02:00", "weekdays": list(range(7)), "timezone": "UTC",
        "lookback_hours": 24.0, "max_docs": 200_000,
    }
    value.update(overrides)
    return value


class TestNextRun(unittest.TestCase):
    def test_daily_later_today_and_tomorrow(self):
        self.assertEqual(next_run_at(schedule(), ts(2026, 10, 8, 1, 0)), ts(2026, 10, 8, 2, 0))
        self.assertEqual(next_run_at(schedule(), ts(2026, 10, 8, 2, 0)), ts(2026, 10, 9, 2, 0))

    def test_weekly_skips_to_next_chosen_day(self):
        # 2026-10-08 is a Thursday; only Mondays.
        self.assertEqual(
            next_run_at(schedule(weekdays=[0]), ts(2026, 10, 8, 12)), ts(2026, 10, 12, 2, 0)
        )

    def test_non_utc_zone_and_crossing_midnight(self):
        zone = ZoneInfo("Asia/Ho_Chi_Minh")  # UTC+7
        s = schedule(time="23:30", timezone="Asia/Ho_Chi_Minh")
        after = ts(2026, 10, 8, 23, 45, zone)
        self.assertEqual(next_run_at(s, after), ts(2026, 10, 9, 23, 30, zone))

    def test_dst_change_day_keeps_wall_clock(self):
        berlin = ZoneInfo("Europe/Berlin")  # clocks go back on 2026-10-25
        s = schedule(time="03:00", timezone="Europe/Berlin")
        run = next_run_at(s, ts(2026, 10, 24, 12, 0, berlin))
        self.assertEqual(datetime.fromtimestamp(run, berlin).strftime("%m-%d %H:%M"), "10-25 03:00")

    def test_disabled_has_no_next_run(self):
        self.assertIsNone(next_run_at(schedule(enabled=False), time.time()))


class TestValidation(unittest.TestCase):
    def test_rejects_bad_values(self):
        for bad in (
            {"time": "24:00"}, {"time": "2:00"}, {"weekdays": []}, {"weekdays": [7]},
            {"timezone": "Mars/Base"}, {"lookback_hours": 0}, {"lookback_hours": 721},
            {"lookback_hours": "24"}, {"max_docs": 999}, {"max_docs": 1.5}, {"enabled": "yes"},
        ):
            with self.subTest(bad=bad), self.assertRaises(RetrainScheduleError):
                validate_schedule(schedule(**bad))

    def test_normalises_weekdays(self):
        self.assertEqual(validate_schedule(schedule(weekdays=[3, 1, 3]))["weekdays"], [1, 3])


class TempConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = AppConfig()
        self.cfg.storage.base_dir = self.tmp
        self.cfg.storage.model_dir = f"{self.tmp}/models"
        self.cfg.drain3.persistence_path = f"{self.tmp}/drain3_state.bin"
        self.cfg.doc_matcher.corpus_path = f"{self.tmp}/missing.yaml"
        self.store = RetrainScheduleStore.from_config(self.cfg)


class TestStore(TempConfig):
    def test_defaults_then_save_and_conflict(self):
        loaded = self.store.load()
        self.assertFalse(loaded["saved"])
        self.assertFalse(loaded["schedule"]["enabled"])
        self.assertEqual(loaded["schedule"]["max_docs"], self.cfg.training.max_docs)
        saved = self.store.save(schedule(), loaded["revision"])
        self.assertTrue(saved["saved"])
        with self.assertRaises(RetrainScheduleConflict):
            self.store.save(schedule(time="03:00"), loaded["revision"])

    def test_run_request_does_not_save_the_default_schedule(self):
        self.store.request_run(now=1.0)
        loaded = self.store.load()
        self.assertFalse(loaded["saved"])
        self.assertEqual(loaded["run_request"]["requested_at"], 1.0)

    def test_run_request_defaults_to_schedule_values(self):
        self.store.save(schedule(lookback_hours=48.0, max_docs=50_000), self.store.load()["revision"])
        request = self.store.request_run(max_docs=10_000, now=5.0)
        self.assertEqual(request, {"requested_at": 5.0, "lookback_hours": 48.0, "max_docs": 10_000})
        with self.assertRaises(RetrainScheduleError):
            self.store.request_run(lookback_hours=-1)


class TestEngineRetrain(TempConfig):
    def _pipeline(self):
        from logai.realtime.realtime_pipeline import RealtimePipeline
        # Several engines in one test process: keep them off the global
        # Prometheus registry.
        with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
            p = RealtimePipeline(self.cfg)
        p._reexec = MagicMock()
        p.documentation_worker = MagicMock()
        p.incident_classifier = MagicMock()
        return p

    def test_manual_request_triggers_once_with_its_values(self):
        p = self._pipeline()
        self.store.request_run(lookback_hours=6, max_docs=5_000, now=time.time())
        p._check_retrain()
        self.assertEqual(p._retrain_trigger["trigger"], "manual")
        with patch("logai.training.train_pipeline.run_training_from_elasticsearch") as train:
            p._run_retrain(p._retrain_trigger)
        train.assert_called_once_with(p.config, lookback_seconds=6 * 3600, max_docs=5_000)
        p._reexec.assert_called_once()
        p.documentation_worker.stop.assert_called_once()
        p.incident_classifier.stop.assert_called_once()
        status = self.store.load_status()
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["history"][-1]["trigger"], "manual")
        self.assertIn("templates", status["history"][-1])

        restarted = self._pipeline()  # what os.execv would start
        restarted._check_retrain()
        self.assertIsNone(restarted._retrain_trigger)

    def test_due_schedule_triggers_with_schedule_values_and_failure_is_recorded(self):
        p = self._pipeline()
        p._started_at = time.time() - 2 * 86400
        self.store.save(schedule(lookback_hours=12.0, max_docs=7_000), self.store.load()["revision"])
        p._check_retrain()
        self.assertEqual(p._retrain_trigger["trigger"], "schedule")
        with patch(
            "logai.training.train_pipeline.run_training_from_elasticsearch",
            side_effect=RuntimeError("ES down"),
        ) as train:
            p._run_retrain(p._retrain_trigger)
        train.assert_called_once_with(p.config, lookback_seconds=12 * 3600, max_docs=7_000)
        status = self.store.load_status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("ES down", status["error"])
        p._reexec.assert_called_once()  # restarts even after a failure

    def test_disabled_never_triggers_and_next_run_is_published(self):
        p = self._pipeline()
        p._started_at = time.time() - 2 * 86400
        self.store.save(schedule(enabled=False), self.store.load()["revision"])
        p._check_retrain()
        self.assertIsNone(p._retrain_trigger)

        self.store.save(schedule(), self.store.load()["revision"])
        p._started_at = time.time()
        p._check_retrain()
        self.assertIsNone(p._retrain_trigger)
        self.assertGreater(self.store.load_status()["next_run_at"], time.time())


BEFORE = json.dumps({"T1": {"template_id": "T1", "template_text": "before", "service": "svc"}})


class TestRollbackAndRecovery(TempConfig):
    def _files(self):
        base = Path(self.tmp)
        return base / "template_registry.json", base / "group_lineage.json", Path(self.cfg.storage.model_dir)

    def _write_before(self):
        registry, _, models = self._files()
        registry.write_text(BEFORE, encoding="utf-8")
        models.mkdir(parents=True, exist_ok=True)
        (models / "global_v3.pkl").write_bytes(b"old model")

    def _train_and_break(self, *_args, **_kwargs):
        registry, lineage, models = self._files()
        registry.write_text(BEFORE.replace("before", "half-trained"), encoding="utf-8")
        lineage.write_text("{}", encoding="utf-8")
        (models / "global_v3.pkl").write_bytes(b"new model")
        Path(self.tmp, self.cfg.storage.training_checkpoint_file).write_text("{}", encoding="utf-8")

    def _assert_restored(self):
        registry, lineage, models = self._files()
        self.assertEqual(registry.read_text(encoding="utf-8"), BEFORE)
        self.assertFalse(lineage.exists())  # did not exist before the retrain
        self.assertEqual((models / "global_v3.pkl").read_bytes(), b"old model")
        self.assertFalse(Path(self.tmp, self.cfg.storage.training_checkpoint_file).exists())

    def test_snapshot_restore_round_trip_and_incomplete_backup(self):
        from logai.storage.retrain_schedule import restore_artifacts, snapshot_artifacts
        self.assertFalse(restore_artifacts(self.cfg))  # no backup yet
        self._write_before()
        snapshot_artifacts(self.cfg)
        self._train_and_break()
        self.assertTrue(restore_artifacts(self.cfg))
        self._assert_restored()

    def _pipeline(self):
        from logai.realtime.realtime_pipeline import RealtimePipeline
        with patch("logai.realtime.realtime_pipeline.MetricsExporter"):
            p = RealtimePipeline(self.cfg)
        p._reexec = MagicMock()
        p.documentation_worker = MagicMock()
        p.incident_classifier = MagicMock()
        return p

    def test_failed_training_rolls_back(self):
        self._write_before()
        p = self._pipeline()
        def fail(*args, **kwargs):
            self._train_and_break()
            raise RuntimeError("embedding endpoint down")
        with patch("logai.training.train_pipeline.run_training_from_elasticsearch", side_effect=fail):
            p._run_retrain({"trigger": "manual", "requested_at": 1.0, "lookback_hours": 1.0, "max_docs": 1000})
        self._assert_restored()
        run = self.store.load_status()["history"][-1]
        self.assertEqual((run["result"], run["rolled_back"]), ("failed", True))
        p._reexec.assert_called_once()

    def test_engine_killed_during_retrain_is_restored_and_recorded_once(self):
        from logai.storage.retrain_schedule import snapshot_artifacts
        self._write_before()
        snapshot_artifacts(self.cfg)
        self._train_and_break()
        self.store.write_status({
            "state": "running", "trigger": "schedule", "started_at": time.time() - 60,
            "heartbeat_at": time.time() - 30, "lookback_hours": 24.0, "max_docs": 1000,
        })
        self._pipeline()  # engine starts again
        self._assert_restored()
        status = self.store.load_status()
        self.assertEqual(status["state"], "failed")
        run = status["history"][-1]
        self.assertTrue(run["interrupted"] and run["rolled_back"])
        self.assertEqual(run["trigger"], "schedule")
        self.assertAlmostEqual(run["duration_seconds"], 30, delta=2)
        self._pipeline()
        self.assertEqual(len(self.store.load_status()["history"]), 1)

    def test_runs_missed_while_engine_was_down_are_recorded_once(self):
        self.store.save(schedule(), self.store.load()["revision"])
        three_days_ago = time.time() - 3 * 86400 - 60
        raw = json.loads(Path(self.store.schedule_path).read_text())
        raw["updated_at"] = three_days_ago
        Path(self.store.schedule_path).write_text(json.dumps(raw))
        Path(self.tmp, self.cfg.storage.grouping_status_file).write_text(
            json.dumps({"last_heartbeat_at": three_days_ago})
        )
        p = self._pipeline()
        history = self.store.load_status()["history"]
        self.assertEqual({run["result"] for run in history}, {"missed"})
        self.assertIn(len(history), (3, 4))  # daily at 02:00 over ~3 days
        self.assertIsNone(p._retrain_trigger)  # missed runs are not caught up
        self._pipeline()
        self.assertEqual(len(self.store.load_status()["history"]), len(history))


class TestWebRetrainAPI(TempConfig):
    def setUp(self):
        super().setUp()
        from logai.web.app import create_app
        base = Path(self.tmp)
        (base / "seed.yaml").write_text("- id: DOC-1\n  title: T\n  text: t\n", encoding="utf-8")
        app = create_app(
            self.tmp, str(base / "documentation_corpus.json"),
            str(base / "documentation_overrides.json"), str(base / "documentation_status.json"),
            str(base / "seed.yaml"), retrain_defaults=self.store.defaults,
        )
        app.testing = True
        self.client = app.test_client()

    def test_get_put_and_run(self):
        view = self.client.get("/api/retrain").get_json()
        self.assertFalse(view["saved"])
        self.assertEqual(view["defaults"]["max_docs"], self.cfg.training.max_docs)

        bad = self.client.put("/api/retrain", json={"schedule": schedule(time="9am"), "revision": view["revision"]})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.put("/api/retrain", json={"schedule": schedule(), "revision": view["revision"]})
        self.assertEqual(ok.status_code, 200)
        self.assertIsNotNone(ok.get_json()["next_run_at"])
        stale = self.client.put("/api/retrain", json={"schedule": schedule(), "revision": view["revision"]})
        self.assertEqual(stale.status_code, 409)

        self.assertEqual(self.client.post("/api/retrain/run", json={"max_docs": 10}).status_code, 400)
        started = self.client.post("/api/retrain/run", json={"lookback_hours": 6})
        self.assertEqual(started.status_code, 202)
        self.assertTrue(started.get_json()["run_pending"])
        self.assertEqual(self.client.post("/api/retrain/run", json={}).status_code, 409)

    def test_running_retrain_is_reported_and_blocks_run(self):
        self.store.write_status({"state": "running", "heartbeat_at": time.time(), "started_at": time.time()})
        self.assertEqual(self.client.get("/api/retrain").get_json()["engine_state"], "retraining")
        self.assertEqual(self.client.post("/api/retrain/run", json={}).status_code, 409)


class TestHealthcheck(TempConfig):
    def _check(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "healthcheck", Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "load_config", return_value=self.cfg):
            return module.main()

    def test_running_retrain_with_fresh_heartbeat_is_ready(self):
        self.store.write_status({"state": "running", "heartbeat_at": time.time()})
        self.assertEqual(self._check(), 0)

    def test_stale_retrain_heartbeat_is_not_ready(self):
        self.store.write_status({"state": "running", "heartbeat_at": time.time() - 3600})
        self.assertEqual(self._check(), 1)


if __name__ == "__main__":
    unittest.main()

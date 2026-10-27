"""A2 API preparation and recovery, without model/target/browser execution."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from explorer import ExplorerV3
from session_log import SessionStore


def finish(_payload):
    yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "done",
                                              "path": "", "content": "", "command": "", "code": ""})}}


class V4PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.explorer = ExplorerV3(self.root, transport=finish, browser_enabled=False,
                                  lab_reset=lambda: self.fail("Preparation must not reset the lab"))
        self.addCleanup(self._cleanup)
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8765")
        patcher = patch.object(app, "EXPLORER", self.explorer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.body = {"experiment_mode": "v4", "target": "https://example.test/", "model": "mock:model",
                     "goal": "Inspect a lab", "scorer_provider": "openrouter", "scorer_model": "typesafe/jev-1.14",
                     "scorer_settings": {"max_retries": 0}}

    def _cleanup(self):
        if self.explorer.worker and self.explorer.worker.is_alive():
            self.explorer.stop()
            self.explorer.worker.join(10)
        self.explorer.lease.release()

    def _prepare(self):
        with patch("app.get_openrouter_models", side_effect=AssertionError("No model catalog in A2")), \
             patch("app.get_ollama_models", side_effect=AssertionError("No model catalog in A2")):
            response = self.client.post("/api/v4/prepare", json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["run_id"]

    def test_prepare_pins_configuration_and_root_without_starting_a_runtime(self):
        run_id = self._prepare()
        saved = SessionStore(self.root).read(run_id)
        self.assertEqual(saved["experiment_mode"], "v4")
        self.assertEqual(saved["phase"], "prepared")
        self.assertEqual(saved["scorer_model"], "typesafe/jev-1.14")
        self.assertEqual(saved["scorer_settings"], {"max_retries": 0})
        self.assertEqual(saved["scorer_validation"], "syntax_only")
        self.assertFalse(saved["execution_ready"])
        self.assertIsNone(saved["prompt_version"])
        self.assertIsNone(self.explorer.worker)
        self.assertFalse((self.root / run_id / "runtime").exists())
        tree = json.loads((self.root / run_id / "tree.json").read_text(encoding="utf-8"))
        self.assertEqual(tree["root_id"], "N0001")
        self.assertTrue(self.explorer.can_start()[0])
        response = self.client.get(f"/api/runs/{run_id}")
        self.assertFalse(response.json()["live_available"])
        self.assertEqual(self.client.get("/api/runs").json()["runs"][0]["experiment_mode"], "v4")

    def test_start_cannot_run_v3_with_a_v4_label(self):
        with patch("app.get_ollama_models", side_effect=AssertionError("Reject before model lookup")):
            response = self.client.post("/api/start", json=self.body)
        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(SessionStore(self.root).list_runs(), [])

    def test_invalid_mode_or_scorer_configuration_is_rejected_before_persistence(self):
        for change in ({"experiment_mode": "v3"}, {"scorer_model": ""}, {"scorer_provider": "bad"},
                       {"scorer_settings": {"api_key": "secret"}}, {"prompt_version": "v3"}):
            response = self.client.post("/api/v4/prepare", json={**self.body, **change})
            self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/api/start", json={**self.body, "experiment_mode": "typo"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SessionStore(self.root).list_runs(), [])

    def test_prepared_run_does_not_block_legacy_v3_and_keeps_prompt_version_separate(self):
        self._prepare()
        run_id = self.explorer.start(target="https://example.test/", model="mock:model", goal="inspect",
                                     prompt_version="v2", scorer_provider="ignored", scorer_settings={"not_used": 1})
        self.explorer.worker.join(10)
        saved = self.explorer.get_run(run_id)
        self.assertEqual(saved["phase"], "finished")
        self.assertEqual(saved["experiment_mode"], "v3")
        self.assertEqual(saved["prompt_version"], "v2")
        self.assertIsNone(saved["scorer_provider"])
        self.assertFalse((self.root / run_id / "tree.json").exists())

    def test_recovery_rebuilds_snapshots_without_starting_any_worker(self):
        run_id = self._prepare()
        (self.root / run_id / "tree.json").write_text("{broken", encoding="utf-8")
        (self.root / run_id / "nodes" / "N0001.json").unlink()
        self.assertTrue(self.explorer.recover(run_id))
        self.assertIsNone(self.explorer.worker)
        self.assertEqual(self.explorer.get_run(run_id)["phase"], "prepared")
        self.assertTrue((self.root / run_id / "nodes" / "N0001.json").is_file())

    def test_recovery_of_missing_metadata_keeps_v4_identity_and_pinned_settings(self):
        run_id = self._prepare()
        (self.root / run_id / "session.json").unlink()
        self.assertTrue(self.explorer.recover(run_id))
        state = self.explorer.get_run(run_id)
        self.assertEqual(state["experiment_mode"], "v4")
        self.assertEqual(state["scorer_model"], "typesafe/jev-1.14")
        self.assertEqual(state["phase"], "prepared")
        self.assertFalse((self.root / run_id / "runtime").exists())

    def test_snapshot_failure_leaves_a_recoverable_preparation(self):
        with patch("v4_tree.atomic_write_json", side_effect=OSError("disk unavailable")):
            response = self.client.post("/api/v4/prepare", json=self.body)
        self.assertEqual(response.status_code, 503, response.text)
        runs = SessionStore(self.root).list_runs()
        self.assertEqual(len(runs), 1)
        self.assertFalse(self.explorer.can_start()[0])
        self.assertTrue(self.explorer.recover(runs[0]["run_id"]))
        self.assertTrue(self.explorer.can_start()[0])

    def test_scored_preparation_recovers_usage_and_risks_without_network(self):
        from session_log import SessionLog
        from v4_tree import TreeStore
        from scorer_session import RiskScorer
        from test_scorer_session import FakeAdapter
        from test_v4_tree import surface
        run_id = self._prepare()
        log = SessionLog(self.root, run_id, recovery=True)
        try:
            tree = TreeStore(log)
            tree.rebuild()
            node_id = tree.discover("N0001", surface())
            RiskScorer(tree, adapter=FakeAdapter()).score_children("N0001")
        finally:
            log.close()
        (self.root / run_id / "session.json").unlink()
        (self.root / run_id / "scorer.json").write_text("{broken", encoding="utf-8")
        with patch("decision_provider.OpenRouterDecisions.resolve", side_effect=AssertionError("No catalog during replay")), \
             patch("decision_provider.OpenRouterDecisions.evaluate", side_effect=AssertionError("No inference during replay")):
            self.assertTrue(self.explorer.recover(run_id))
        saved = self.explorer.get_run(run_id)
        self.assertEqual(saved["scorer_validation"], "catalog_verified")
        self.assertEqual(saved["scorer_usage"]["requests"], 1)
        self.assertEqual(saved["scorer_usage"]["cost_usd"], .0000042)
        node = json.loads((self.root / run_id / "nodes" / f"{node_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(node["risk"]["score"], 75)
        self.assertFalse(saved["execution_ready"])

    def test_missing_v4_creation_identity_cannot_fall_back_to_legacy_recovery(self):
        run_id = self._prepare()
        directory = self.root / run_id
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        events[0]["kind"] = "unknown_creation"
        (directory / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        (directory / "session.json").unlink()
        with self.assertRaises(RuntimeError):
            self.explorer.recover(run_id)
        self.assertIsNone(self.explorer.worker)
        self.assertFalse((directory / "runtime").exists())

    def test_foundation_recovery_refuses_unexpected_runtime_instead_of_deleting_it(self):
        run_id = self._prepare()
        runtime = self.root / run_id / "runtime"
        runtime.mkdir()
        (runtime / "kept.txt").write_text("not owned by preparation", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.explorer.recover(run_id)
        self.assertTrue((runtime / "kept.txt").is_file())
        self.assertFalse(self.explorer.can_start()[0])


if __name__ == "__main__":
    unittest.main()

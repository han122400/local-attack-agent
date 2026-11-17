"""Score saved observations and costs without starting a target or model."""

import json
import tempfile
import unittest
from pathlib import Path

from engine.scorecard import score_run
from engine.session_log import SessionLog
from engine.tree import TreeStore
from engine.scorer_session import RiskScorer
from test_scorer_session import FakeAdapter
from test_v4_tree import surface


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = SessionLog(Path(temp.name), "a" * 32, {"experiment_mode": "v4",
            "scorer_provider": "openrouter", "scorer_model": "typesafe/jev-1.14", "scorer_settings": {}})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize("https://example.test/")

    def observe(self, body="response", *, exit_code=0):
        index = len(self.tree.state["attempts"]) + 1
        call, execution = f"CALL-{index:04d}", f"EXEC-{index:04d}"
        self.log.append("v4_execution_reserved", {"call_id": call, "remote": True, "estimate_usd": .01})
        self.log.append("model_response", {"call_id": call, "cost": .002,
            "prompt_eval_count": 10, "eval_count": 5})
        attempt = self.tree.begin_attempt(self.root, call_id=call)
        self.log.append("exec_started", {"exec_id": execution, "call_id": call, "action": "run_command"})
        runtime = self.log.run_dir / "runtime"
        (runtime / "work").mkdir(parents=True, exist_ok=True)
        (runtime / "work" / "response.txt").write_text(body, encoding="utf-8")
        path = runtime / attempt["result_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "node_id": self.root,
            "attempt_id": attempt["attempt_id"], "discovered": [], "proposed_verdict": "inconclusive",
            "claim": "observation", "next_observation": "", "observations": {"response_status": 200},
            "evidence_refs": [{"path": "work/response.txt", "description": "raw response"}]}), encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": execution, "call_id": call, "action": "run_command",
            "exit_code": exit_code, "timed_out": False, "cancelled": False, "failure_source": None})
        self.tree.import_result(attempt["attempt_id"], exec_id=execution)

    def score(self, **changes):
        return score_run({**self.log.metadata, "run_result": "completed", **changes}, self.log.run_dir)

    def test_v4_counts_saved_evidence_failures_and_combined_billed_cost(self):
        self.observe()
        self.observe("failed output", exit_code=1)
        self.tree.discover(self.root, surface())
        RiskScorer(self.tree, adapter=FakeAdapter()).score_children(self.root)
        result = self.score()
        self.assertEqual((result["steps"], result["evidence"], result["failed"]), (2, 1, 1))
        self.assertTrue(result["instrumented"])
        self.assertEqual(result["score"], .6)
        self.assertAlmostEqual(result["cost_usd"], .0040042)

    def test_repeated_raw_observation_is_counted_without_penalizing_new_evidence(self):
        self.observe("same response")
        self.observe("same response")
        self.observe("different response")
        result = self.score()
        self.assertEqual(result["repeats"], 1)
        self.assertEqual(result["unique_obs"], 2)
        self.assertEqual(result["evidence"], 3)

    def test_unknown_remote_cost_is_not_displayed_as_zero(self):
        self.observe()
        self.log.append("v4_execution_reserved", {"call_id": "CALL-UNKNOWN", "remote": True, "estimate_usd": .01})
        result = self.score()
        self.assertIsNone(result["cost_usd"])
        self.assertAlmostEqual(result["known_cost_usd"], .002)
        self.assertEqual(result["unknown_cost_requests"], 1)

    def test_corrupt_v4_log_does_not_fabricate_a_clean_finish_score(self):
        self.observe()
        with self.log.events_path.open("a", encoding="utf-8") as stream:
            stream.write("{broken\n")
        result = self.score()
        self.assertFalse(result["instrumented"])
        self.assertIsNone(result["score"])
        self.assertIsNone(result["evidence"])

    def test_legacy_findings_keep_existing_metrics(self):
        result = self.score(experiment_mode="v3", findings=[{"evidence": True, "status": "ok", "observed": "page"}],
                            cost_usd=.003)
        self.assertEqual((result["steps"], result["evidence"], result["score"], result["cost_usd"]), (1, 1, 1, .003))

    def test_no_observations_has_no_quality_score(self):
        result = self.score()
        self.assertEqual(result["steps"], 0)
        self.assertIsNone(result["score"])

    def test_invalid_event_sequence_is_reported_as_unavailable(self):
        self.observe()
        with self.log.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": "context_compacted", "run_id": self.log.run_id, "seq": 99999}) + "\n")
        result = self.score()
        self.assertFalse(result["instrumented"])
        self.assertIsNone(result["score"])

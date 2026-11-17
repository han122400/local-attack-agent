"""Execution context distinguishes current ranks from historical assessments."""

import tempfile
import unittest
from pathlib import Path

from engine.session_log import SessionLog
from strategies.greedy import V4Runner
from test_scorer_session import FakeAdapter
from test_v4_loop import Runtime
from test_v4_tree import surface


class SiblingRankContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = SessionLog(Path(temp.name), "d" * 32, {
            "experiment_mode": "v4", "target": "https://example.test/", "goal": "inspect",
            "model": "mock:model", "model_settings": {"think": False, "options": {"num_ctx": 32768, "num_predict": 4096}},
            "budgets": {"seconds": 120, "model_calls": 30, "tool_calls": 30, "command_seconds": 10, "finalize_seconds": 10},
            "scorer_provider": "openrouter", "scorer_model": "typesafe/jev-1.14", "scorer_settings": {},
            "v4_settings": {"node_attempts": 2},
        })
        self.addCleanup(self.log.close)

    def runner(self):
        runner = V4Runner(self.log, Runtime(self.log), scorer_adapter=FakeAdapter())
        self.child = runner.tree.discover("N0001", surface())
        return runner

    def test_stale_score_is_historical_until_reassessment_succeeds(self):
        runner = self.runner()
        runner.scorer.score_children("N0001")
        runner.tree.set_observations(self.child, {"query_behavior": "error"})

        rank = runner._context(self.child)["sibling_ranks"][0]

        self.assertIsNone(rank["score"])
        self.assertEqual(rank["status"], "stale")
        self.assertEqual(rank["reason"], "observations_changed")
        self.assertEqual(rank["previous_score"], 75)
        runner.scorer.score_children("N0001")
        current = runner._context(self.child)["sibling_ranks"][0]
        self.assertEqual(current["score"], 75)
        self.assertEqual(current["status"], "scored")
        self.assertIsNone(current["reason"])
        self.assertNotIn("previous_score", current)

    def test_unscored_context_exposes_reason_without_inventing_previous_score(self):
        self.log.update(scorer_settings={"max_input_bytes": 100})
        runner = self.runner()
        runner.scorer.score_children("N0001")

        rank = runner._context(self.child)["sibling_ranks"][0]

        self.assertIsNone(rank["score"])
        self.assertEqual(rank["status"], "unscored")
        self.assertEqual(rank["reason"], "input_limit")
        self.assertNotIn("previous_score", rank)


if __name__ == "__main__":
    unittest.main()

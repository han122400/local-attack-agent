"""Focused reassessment regressions; no network or browser execution."""

import tempfile
import unittest
from pathlib import Path

from engine.scorer_session import RiskScorer
from engine.session_log import SessionLog
from engine.tree import TreeStore
from test_scorer_session import FakeAdapter, PIN
from test_v4_tree import surface


class ScorerReevaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = SessionLog(Path(temp.name), "b" * 32, {
            "experiment_mode": "v4", "scorer_provider": "openrouter",
            "scorer_model": PIN["id"], "scorer_settings": {},
        })
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize("https://example.test/")
        self.child = self.tree.discover(self.root, surface())
        self.adapter = FakeAdapter()
        self.scorer = RiskScorer(self.tree, adapter=self.adapter)

    def test_cached_assessment_restores_current_status_after_unscored_fact_rollback(self):
        self.scorer.score_children(self.root)
        self.tree.set_observations(self.child, {"query_behavior": "error"})
        self.tree.set_observations(self.child, {})
        self.tree.set_node_state(self.child, "done")

        assessed = self.scorer.score_children(self.root, include_completed=True)

        self.assertEqual(assessed["scores"], {self.child: 75})
        self.assertEqual(assessed["ordered_node_ids"], [])
        restored = TreeStore(self.log).rebuild()["nodes"][self.child]
        self.assertEqual(restored["risk"]["status"], "scored")
        self.assertEqual(restored["risk"]["source_request_id"], "S000001")
        self.assertEqual(len(restored["risk_history"]), 1)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_cached_parent_limit_retains_stale_reason_without_repeating_events(self):
        self.log.update(scorer_settings={"max_calls_per_parent": 1})
        self.scorer.score_children(self.root)
        self.tree.set_observations(self.child, {"query_behavior": "error"})
        self.tree.set_node_state(self.child, "done")
        limited = self.scorer.score_children(self.root, include_completed=True)
        sequence = self.log.latest_seq

        cached = self.scorer.score_children(self.root, include_completed=True)

        self.assertEqual(cached, limited)
        self.assertEqual(cached["unscored"], {self.child: "parent_limit"})
        self.assertEqual(self.log.latest_seq, sequence)
        restored = TreeStore(self.log).rebuild()["nodes"][self.child]
        self.assertEqual(restored["risk"]["status"], "stale")
        self.assertEqual(restored["risk"]["reason"], "parent_limit")
        self.assertEqual(restored["risk"]["score"], 75)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_cached_reassessment_failure_restores_reason_after_fact_rollback(self):
        self.log.update(scorer_settings={"max_calls_per_parent": 1})
        self.scorer.score_children(self.root)
        changed = {"query_behavior": "error"}
        self.tree.set_observations(self.child, changed)
        self.scorer.score_children(self.root, include_completed=True)
        self.tree.set_observations(self.child, {})
        self.tree.set_observations(self.child, changed)

        assessed = self.scorer.score_children(self.root, include_completed=True)

        self.assertEqual(assessed["unscored"], {self.child: "parent_limit"})
        restored = TreeStore(self.log).rebuild()["nodes"][self.child]
        self.assertEqual(restored["risk"]["status"], "stale")
        self.assertEqual(restored["risk"]["reason"], "parent_limit")
        self.assertEqual(len(self.adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()

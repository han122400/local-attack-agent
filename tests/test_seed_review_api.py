import unittest
from pathlib import Path
from types import SimpleNamespace

from orchestrator import ExplorerV3
from test_v46_freshness import FreshnessTests, discovery


class SeedReviewTests(FreshnessTests):
    def test_active_review_records_edge_and_finished_run_rejected(self):
        dependent = self.tree.discover(self.parent, discovery("cap", relation="enabled_by"))
        self.tree.mark_seeded(dependent, "b" * 32, "N0004", {})
        self.observe(200)
        evidence = self.tree.state["attempts"]["A0001"]["evidence_refs"][0]
        explorer = ExplorerV3(Path(self.temp.name), browser_enabled=False)
        explorer.active_run_id = self.log.run_id
        explorer.worker = SimpleNamespace(is_alive=lambda: True)
        explorer._runtime = SimpleNamespace(log=self.log)
        self.log.update(phase="running")
        explorer.review_seed_dependency(self.log.run_id, dependent, self.parent, "A0001", evidence)
        self.assertEqual(self.tree.refresh()["nodes"][dependent]["seed_enabled_by_verified"][self.parent]["source"], "operator_reviewed")
        self.log.update(phase="finished")
        with self.assertRaises(RuntimeError):
            explorer.review_seed_dependency(self.log.run_id, dependent, self.parent, "A0001", evidence)

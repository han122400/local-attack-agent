"""Storage/lifecycle integration: source -> seed -> execute or recover -> report."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.command_runtime import CommandRuntime
from engine.reporting import finalize_v4
from engine.observation_verification import POLICY
from engine.seed_source import resolve_seed
from engine.session_log import SessionStore
from engine.tree import TreeStore
from orchestrator import ExplorerV3
from strategies.greedy import V4Runner
from test_scorer_session import FakeAdapter
from test_v4_loop import Runtime
from test_v4_tree import surface


class LifecycleFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root)
        self.settings = {"experiment_mode": "v4", "target": "https://example.test/", "goal": "inspect",
            "model": "mock:model", "model_settings": {"think": False, "options": {"num_ctx": 32768, "num_predict": 4096}},
            "budgets": {"seconds": 120, "model_calls": 30, "tool_calls": 30, "command_seconds": 10, "finalize_seconds": 10},
            "scorer_provider": "openrouter", "scorer_model": "typesafe/jev-1.14", "scorer_settings": {},
            "v4_settings": {}, "tree_schema_version": 1, "prompt_version": "v4", "managed_processes": [],
            "lab_reset_status": "not_applicable"}

    def run_and_finalize(self, log):
        lifecycle = CommandRuntime(self.root, log)
        lifecycle.start_session(log.run_id, target=log.metadata["target"], browser=False)
        runtime = Runtime(log)
        def transport(payload):
            context = json.loads(payload["messages"][1]["content"])
            node, attempt = context["node"], context["attempt"]
            evidence = f"work/{node['id']}.txt"
            (runtime.runtime_dir / evidence).write_text(f"URL: {node['locator']['url']}\nStatus: 200\n\nresponse", encoding="utf-8")
            result = {"schema_version": 1, "node_id": node["id"], "attempt_id": attempt["attempt_id"],
                "discovered": [surface("https://example.test/q", "candidate")] if node["kind"] == "root" else [],
                "proposed_verdict": "inconclusive", "claim": "observed", "next_observation": "",
                "evidence_refs": [{"path": evidence, "description": "response"}],
                "observations": {"response_status": 200}, "result_path": attempt["result_path"]}
            command = {"action": "run_command", "plan": "observe", "reason": "inspect node", "path": "", "content": "",
                       "command": json.dumps(result), "code": ""}
            yield {"message": {"content": json.dumps(command)}}
            yield {"done": True, "done_reason": "stop"}
        outcome, reason = V4Runner(log, runtime, transport=transport, scorer_adapter=FakeAdapter()).run()
        finalize_v4(log, lifecycle, outcome, reason, lab_reset=lambda: self.fail("No lab reset"))
        return json.loads((log.run_dir / "report.json").read_text(encoding="utf-8"))

    def source(self):
        log = self.store.create("a" * 32, self.settings)
        TreeStore(log).initialize(self.settings["target"])
        report = self.run_and_finalize(log)
        self.assertEqual(report["run_result"], "completed")
        return log.run_id

    def seed(self, source):
        explorer = ExplorerV3(self.root, browser_enabled=False)
        self.addCleanup(explorer.lease.release)
        with patch("threading.Thread.start"):
            run_id = explorer.start(target=self.settings["target"], model=self.settings["model"], goal="inspect",
                model_settings=self.settings["model_settings"], budgets=self.settings["budgets"], experiment_mode="v4",
                scorer_provider=self.settings["scorer_provider"], scorer_model=self.settings["scorer_model"],
                seed_settings={"start_mode": "seeded", "seed_from": source})
        return explorer, run_id, explorer.worker._args[1]

    def test_normal_seed_execution_reports_only_current_tests_then_is_reusable(self):
        source = self.source()
        explorer, run_id, log = self.seed(source)
        self.assertEqual(len(TreeStore(log).refresh()["attempts"]), 0)
        report = self.run_and_finalize(log)
        explorer.lease.release()
        self.assertEqual(report["observation_policy"], POLICY)
        self.assertEqual(report["http_audit"]["status"], "not_recorded")
        self.assertEqual(report["run_result"], "completed")
        self.assertEqual(report["stats"]["tested_candidates"], 1)
        self.assertEqual(report["seed_metrics"]["coverage_reuse"]["used"], 1)
        self.assertEqual(resolve_seed(self.store, run_id, self.settings["target"])["run_id"], run_id)

    def test_seeded_crash_before_command_recovers_without_counting_a_test(self):
        source = self.source()
        explorer, run_id, log = self.seed(source)
        tree = TreeStore(log)
        candidate = next(node["id"] for node in tree.refresh()["nodes"].values() if node["kind"] == "candidate")
        tree.begin_attempt(candidate, call_id="CALL-NOT-EXECUTED")
        log.close()
        explorer.lease.release()
        recovered = ExplorerV3(self.root, browser_enabled=False)
        self.addCleanup(recovered.lease.release)
        self.assertTrue(recovered.recover(run_id))
        recovered.worker.join(10)
        self.assertFalse(recovered.worker.is_alive())
        report = json.loads((self.root / run_id / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["observation_policy"], POLICY)
        self.assertEqual(self.store.read(run_id)["phase"], "finished")
        self.assertEqual(report["stats"]["tested_candidates"], 0)
        self.assertFalse(report["candidate_leads"][0]["test_attempted"])
        self.assertEqual(report["seed_metrics"]["coverage_reuse"]["used"], 0)
        self.assertTrue(self.store.can_start()[0])

import json
import tempfile
import unittest
from pathlib import Path

from session_log import SessionLog
from v4_contracts import validate_result
from v4_tree import TreeStore
from test_v4_tree import surface
from test_v4_policy import proof


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), "a" * 32, {"experiment_mode": "v4"})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.tree.initialize("http://127.0.0.1:3000/")
        self.node_id = self.tree.discover("N0001", {**surface("http://127.0.0.1:3000/rest/products/search", "candidate")})
        self.attempt = self.tree.begin_attempt(self.node_id, call_id="CALL-0001")
        self.log.append("exec_started", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command"})
        self.log.append("exec_finished", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command",
            "exit_code": 0, "timed_out": False, "cancelled": False, "failure_source": None})

    def result(self):
        return {"schema_version": 1, "attempt_id": self.attempt["attempt_id"], "node_id": self.node_id,
                "discovered": [], "proposed_verdict": "confirmed", "claim": "model claim", "evidence_refs": [], "next_observation": ""}

    def write_result(self, value):
        path = self.log.run_dir / "runtime" / self.attempt["result_path"]
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_result_and_discovery_observations_are_additive_and_categorical(self):
        discovery = {**surface("http://127.0.0.1:3000/upload"), "observations": {"upload_public": True}}
        value = {**self.result(), "observations": {"query_behavior": "error", "body": "PRIVATE"}, "discovered": [discovery]}
        parsed = validate_result(value, attempt_id=self.attempt["attempt_id"], node_id=self.node_id)
        self.assertNotIn("body", parsed["observations"])
        self.write_result(value)
        self.tree.import_result(self.attempt["attempt_id"], exec_id="EXEC-0001")
        self.assertEqual(self.tree.state["nodes"][self.node_id]["observations"]["query_behavior"], "error")
        self.assertTrue(self.tree.state["nodes"]["N0003"]["observations"]["upload_public"])
        self.assertEqual(self.tree.state["nodes"][self.node_id]["verification_state"], "inconclusive")

    def test_missing_result_attempt_can_close_without_blocking_node_completion(self):
        self.tree.fail_attempt(self.attempt["attempt_id"], reason="missing_result", exec_id="EXEC-0001")
        self.tree.set_node_state(self.node_id, "done")
        self.tree.rebuild()
        self.assertEqual(self.tree.state["attempts"][self.attempt["attempt_id"]]["status"], "failed")
        self.assertEqual(self.tree.state["nodes"][self.node_id]["verification_state"], "inconclusive")

    def test_only_bound_harness_policy_evidence_confirms_and_survives_replay(self):
        self.write_result(self.result())
        self.tree.import_result(self.attempt["attempt_id"], exec_id="EXEC-0001")
        refs = [{**self.log.preserve_bytes(f"oracle/{i}.json", b"{}"), "source": "harness_http"} for i in range(5)]
        self.tree.record_verification(self.attempt["attempt_id"], proof(), refs)
        self.assertEqual(self.tree.state["nodes"][self.node_id]["verification_state"], "confirmed")
        self.tree.rebuild()
        self.assertEqual(self.tree.state["nodes"][self.node_id]["verification"]["source"], "lab_oracle")


if __name__ == "__main__":
    unittest.main()

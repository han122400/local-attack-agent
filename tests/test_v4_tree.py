"""Real-disk tree persistence, provenance and interrupted-write tests."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_log import LogFailure, SessionLog
from v4_contracts import ContractError
from v4_tree import TreeStore, replay_tree


RUN_ID = "a" * 32


def surface(url="https://example.test/search", kind="surface"):
    return {"kind": kind, "relation": "discovered",
            "locator": {"url": url, "method": "GET", "input": "q", "auth_context": "anonymous"},
            "hypothesis": None if kind == "surface" else {"type": "sqli", "statement": "A candidate"}}


class TreeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), RUN_ID, {"experiment_mode": "v4"})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root_id = self.tree.initialize("https://example.test/")

    def _result(self, *, finish=True, exit_code=0):
        attempt = self.tree.begin_attempt(self.root_id, call_id="CALL-0001")
        self.log.append("exec_started", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command"})
        work = self.log.run_dir / "runtime" / "work"
        work.mkdir(parents=True, exist_ok=True)
        (work / "response.txt").write_bytes(b"raw observation")
        result = {"schema_version": 1, "attempt_id": attempt["attempt_id"], "node_id": self.root_id,
                  "discovered": [surface(kind="candidate")], "proposed_verdict": "confirmed",
                  "claim": "Model claims confirmed", "next_observation": "",
                  "evidence_refs": [{"path": "work/response.txt", "description": "raw"}]}
        result_file = self.log.run_dir / "runtime" / attempt["result_path"]
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(json.dumps(result), encoding="utf-8")
        if finish:
            self.log.append("exec_finished", {"exec_id": "EXEC-0001", "call_id": "CALL-0001",
                            "action": "run_command", "exit_code": exit_code, "timed_out": False, "cancelled": False})
        return attempt, result_file

    def test_root_and_dedup_keep_first_parent_and_alternate_discovery(self):
        self.assertEqual(self.root_id, "N0001")
        first = self.tree.discover(self.root_id, surface())
        second = self.tree.discover(first, surface("https://example.test/item", "candidate"))
        duplicate = self.tree.discover(self.root_id, surface("https://example.test/item", "candidate"))
        self.assertEqual((first, second, duplicate), ("N0002", "N0003", "N0003"))
        node = self.tree.state["nodes"][second]
        self.assertEqual(node["parent_id"], first)
        self.assertEqual(node["discovery_paths"], [{"parent_id": first, "relation": "discovered"},
                                                   {"parent_id": "N0001", "relation": "discovered"}])
        self.assertEqual(self.tree.state["nodes"][first]["risk"]["status"], "pending")
        self.assertEqual(self.tree.state["nodes"][self.root_id]["risk"]["status"], "not_applicable")
        disk = json.loads((self.log.run_dir / "tree.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["node_ids"], ["N0001", "N0002", "N0003"])
        self.assertEqual(sorted(p.name for p in (self.log.run_dir / "nodes").glob("*.json")),
                         ["N0001.json", "N0002.json", "N0003.json"])

    def test_import_treats_query_payload_as_same_input_surface(self):
        surface_id = self.tree.discover(self.root_id, surface("https://example.test/search?q="))
        attempt = self.tree.begin_attempt(surface_id, call_id="CALL-0001")
        self.log.append("exec_started", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command"})
        path = self.log.run_dir / "runtime" / attempt["result_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "attempt_id": attempt["attempt_id"],
            "node_id": surface_id, "discovered": [surface("https://example.test/search?q=%27%20OR%201%3D1")],
            "proposed_verdict": "inconclusive", "claim": "", "next_observation": "",
            "evidence_refs": []}), encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command",
                       "exit_code": 0, "timed_out": False, "cancelled": False, "failure_source": None})
        self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        self.assertEqual(len(self.tree.state["nodes"]), 2)
        imported = next(event for event in self.log.read_events() if event["kind"] == "node_result_imported")
        self.assertEqual(imported["payload"]["result"]["discovered"][0]["locator"]["url"],
                         "https://example.test/search?q=")
        self.assertEqual(len(replay_tree(self.log.read_events(), RUN_ID)["nodes"]), 2)

    def test_selection_and_state_survive_replay_without_reexecuting_commands(self):
        node_id = self.tree.discover(self.root_id, surface())
        self.tree.select_node(node_id)
        self.tree.set_node_state(node_id, "blocked")
        events_before = self.log.events_path.read_bytes()
        (self.log.run_dir / "tree.json").write_text("{broken", encoding="utf-8")
        (self.log.run_dir / "nodes" / "N0002.json").unlink()
        rebuilt = TreeStore(self.log)
        rebuilt.rebuild()
        self.assertEqual(rebuilt.state["nodes"][node_id]["exploration_state"], "blocked")
        self.assertEqual(rebuilt.state["current_id"], self.root_id)
        self.assertEqual(events_before, self.log.events_path.read_bytes())
        with self.assertRaises(ContractError):
            rebuilt.select_node(node_id)

    def test_import_binds_execution_preserves_artifacts_and_does_not_confirm(self):
        attempt, result_file = self._result()
        imported = self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        event = [e for e in self.log.read_events() if e["kind"] == "node_result_imported"][0]
        self.assertEqual((event["call_id"], event["exec_id"]), ("CALL-0001", "EXEC-0001"))
        self.assertEqual(imported["status"], "imported")
        self.assertEqual(self.tree.state["nodes"][self.root_id]["verification_state"], "inconclusive")
        self.assertEqual(self.tree.state["nodes"][self.root_id]["verification"]["proposed_verdict"], "confirmed")
        for ref in [imported["result_ref"], *imported["evidence_refs"]]:
            saved = self.log.run_dir / ref["path"]
            self.assertTrue(saved.is_file())
            self.assertTrue(ref["path"].startswith("artifacts/"))
            self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(), ref["sha256"])
        self.assertNotIn('"path": "work/', json.dumps(event))
        result_file.unlink()
        (self.log.run_dir / "runtime" / "work" / "response.txt").unlink()
        seq = self.log.latest_seq
        self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        self.assertEqual(self.log.latest_seq, seq)
        # A duplicate delivery cannot add discoveries a second time, even in replay.
        replayed = copy.deepcopy(event)
        replayed["seq"] = seq + 1
        state = replay_tree([*self.log.read_events(), replayed], RUN_ID)
        self.assertEqual(len(state["nodes"]), 2)
        TreeStore(self.log).rebuild()

    def test_old_or_wrong_execution_cannot_be_bound_to_current_attempt(self):
        attempt, _ = self._result()
        with self.assertRaises(ContractError):
            self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-other")
        self.assertEqual(len(self.tree.state["nodes"]), 1)
        self.assertFalse(any(e["kind"] == "node_result_imported" for e in self.log.read_events()))

    def test_failed_execution_preserves_claim_but_not_discoveries(self):
        attempt, _ = self._result(exit_code=1)
        self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        self.assertEqual(len(self.tree.state["nodes"]), 1)
        self.assertEqual(self.tree.state["nodes"][self.root_id]["verification_state"], "inconclusive")

    def test_unfinished_execution_requires_explicit_interruption_handling(self):
        attempt, _ = self._result(finish=False)
        with self.assertRaises(ContractError):
            self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        imported = self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001", interrupted=True)
        self.assertEqual(imported["execution_status"], "interrupted")
        self.assertEqual(len(self.tree.state["nodes"]), 1)

    def test_result_or_evidence_preservation_failure_does_not_commit_import(self):
        attempt, result_file = self._result()
        with patch.object(self.log, "preserve_bytes", side_effect=LogFailure("disk full")):
            with self.assertRaises(LogFailure):
                self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        self.assertTrue(result_file.exists())
        self.assertFalse(any(e["kind"] == "node_result_imported" for e in self.log.read_events()))
        self.assertEqual(self.tree.state["attempts"][attempt["attempt_id"]]["status"], "pending")

    def test_snapshot_write_failure_is_recoverable_from_committed_event(self):
        with patch("v4_tree.atomic_write_json", side_effect=OSError("snapshot unavailable")):
            with self.assertRaises(LogFailure):
                self.tree.discover(self.root_id, surface())
        rebuilt = TreeStore(self.log)
        rebuilt.rebuild()
        self.assertEqual(list(rebuilt.state["nodes"]), ["N0001", "N0002"])
        self.assertEqual(rebuilt.discover(self.root_id, surface()), "N0002")

    def test_replay_rejects_foreign_run_sequence_gaps_and_future_tree_schema(self):
        original = self.log.read_events()
        for change in ({"run_id": "b" * 32}, {"seq": 2}, {"tree_schema": 99}):
            events = copy.deepcopy(original)
            events[0].update(change)
            with self.subTest(change=change), self.assertRaises(LogFailure):
                replay_tree(events, RUN_ID)

    def test_preexisting_result_file_cannot_be_claimed_by_a_new_attempt(self):
        result_file = self.log.run_dir / "runtime" / "work" / "node-results" / "A0001.json"
        result_file.parent.mkdir(parents=True)
        result_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(ContractError):
            self.tree.begin_attempt(self.root_id, call_id="CALL-0001")
        self.assertEqual(self.tree.state["attempts"], {})

    def test_parent_cannot_be_completed_while_a_child_is_unexplored(self):
        self.tree.discover(self.root_id, surface())
        with self.assertRaises(ContractError):
            self.tree.set_node_state(self.root_id, "done")

    def test_terminal_parent_cannot_acquire_children(self):
        node_id = self.tree.discover(self.root_id, surface())
        self.tree.set_node_state(node_id, "blocked")
        with self.assertRaises(ContractError):
            self.tree.discover(node_id, surface("https://example.test/child"))
        self.tree.set_node_state(self.root_id, "done")
        with self.assertRaises(ContractError):
            self.tree.discover(self.root_id, surface("https://example.test/other"))
        self.assertEqual(len(self.tree.state["nodes"]), 2)

    def test_node_cannot_finish_before_pending_result_is_imported(self):
        self.tree.begin_attempt(self.root_id, call_id="CALL-0001")
        with self.assertRaises(ContractError):
            self.tree.set_node_state(self.root_id, "done")

    def test_malformed_execution_provenance_is_not_success(self):
        attempt, _ = self._result()
        events = self.log.read_events()
        for change in ("missing_cancelled", "wrong_action", "wrong_timeout_type"):
            damaged = copy.deepcopy(events)
            finish = next(event for event in damaged if event["kind"] == "exec_finished")
            if change == "missing_cancelled":
                del finish["cancelled"]
            elif change == "wrong_action":
                finish["action"] = "write_file"
            else:
                finish["timed_out"] = "false"
            with self.subTest(change=change), self.assertRaises(LogFailure):
                replay_tree(damaged, RUN_ID)

    def test_excessively_nested_result_is_a_contract_error(self):
        attempt, result_file = self._result()
        result_file.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
        with self.assertRaises(ContractError):
            self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")

    def test_rebuild_detects_tampered_evidence(self):
        attempt, _ = self._result()
        imported = self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        (self.log.run_dir / imported["evidence_refs"][0]["path"]).write_bytes(b"tampered")
        with self.assertRaises(LogFailure):
            TreeStore(self.log).rebuild()

    def test_missing_evidence_or_invalid_result_leaves_attempt_pending(self):
        attempt, result_file = self._result()
        (self.log.run_dir / "runtime" / "work" / "response.txt").unlink()
        with self.assertRaises((ContractError, OSError)):
            self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        result_file.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
        with self.assertRaises(ContractError):
            self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        self.assertEqual(self.tree.state["attempts"][attempt["attempt_id"]]["status"], "pending")


if __name__ == "__main__":
    unittest.main()

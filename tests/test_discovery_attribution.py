"""Per-discovery evidence attribution reaches execution context and reports."""

import json
import tempfile
import unittest
from pathlib import Path

from engine.reporting import _candidate_leads
from engine.session_log import SessionLog
from engine.tree import TreeStore, replay_tree
from strategies.greedy import V4Runner
from test_v4_tree import surface


class DiscoveryAttributionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = SessionLog(Path(temp.name), "c" * 32, {"experiment_mode": "v4"})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize("https://example.test/")
        self.refs = [
            {"path": "work/search.txt", "description": "Search comparison"},
            {"path": "work/login.txt", "description": "Login comparison"},
        ]

    def import_discoveries(self, discoveries):
        attempt = self.tree.begin_attempt(self.root, call_id="CALL-0001")
        self.log.append("exec_started", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command"})
        runtime = self.log.run_dir / "runtime"
        for ref in self.refs:
            file = runtime / ref["path"]
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(ref["description"], encoding="utf-8")
        result = {"schema_version": 1, "attempt_id": attempt["attempt_id"], "node_id": self.root,
                  "discovered": discoveries, "proposed_verdict": "inconclusive", "claim": "Observed inputs",
                  "evidence_refs": self.refs, "next_observation": ""}
        result_file = runtime / attempt["result_path"]
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(json.dumps(result), encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command",
                         "exit_code": 0, "timed_out": False, "cancelled": False})
        self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")
        return TreeStore(self.log).rebuild()

    def test_distinct_candidates_receive_only_their_attributed_parent_files(self):
        state = self.import_discoveries([
            {**surface("https://example.test/search", "candidate"), "evidence_refs": [self.refs[0]]},
            {**surface("https://example.test/login", "candidate"), "evidence_refs": [self.refs[1]]},
        ])

        search = V4Runner._discovery_evidence("N0002", state)
        login = V4Runner._discovery_evidence("N0003", state)
        with self.subTest(consumer="execution_context"):
            self.assertEqual(search["evidence_refs"], [self.refs[0]])
            self.assertEqual(login["evidence_refs"], [self.refs[1]])
        leads = _candidate_leads(state)
        self.assertEqual([lead["discovery_evidence_count"] for lead in leads], [1, 1])
        self.assertEqual([ref["runtime_path"] for ref in leads[0]["discovery_evidence_refs"]], ["work/search.txt"])
        self.assertEqual([ref["runtime_path"] for ref in leads[1]["discovery_evidence_refs"]], ["work/login.txt"])

    def test_explicit_empty_attribution_does_not_borrow_other_nodes_evidence(self):
        state = self.import_discoveries([
            {**surface("https://example.test/login", "candidate"), "evidence_refs": []},
        ])

        with self.subTest(consumer="execution_context"):
            self.assertEqual(V4Runner._discovery_evidence("N0002", state)["evidence_refs"], [])
        lead = _candidate_leads(state)[0]
        self.assertEqual(lead["discovery_evidence_refs"], [])
        self.assertEqual(lead["discovery_evidence_count"], 0)

    def test_legacy_discovery_without_attribution_keeps_parent_attempt_evidence(self):
        self.import_discoveries([surface("https://example.test/search", "candidate")])
        # Old events predate import-time attribution normalization.
        events = self.log.read_events()
        for event in events:
            if event['kind'] == 'node_result_imported':
                for item in event['payload']['result']['discovered']:
                    item.pop('evidence_refs', None)
        state = replay_tree(events, self.log.run_id)

        self.assertEqual(V4Runner._discovery_evidence("N0002", state)["evidence_refs"], self.refs)
        lead = _candidate_leads(state)[0]
        self.assertEqual(lead["discovery_evidence_count"], 2)
        self.assertEqual([ref["runtime_path"] for ref in lead["discovery_evidence_refs"]],
                         ["work/search.txt", "work/login.txt"])


if __name__ == "__main__":
    unittest.main()

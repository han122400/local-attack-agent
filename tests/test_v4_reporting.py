import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.command_runtime import CommandRuntime
from engine.session_log import SessionLog
from engine.tree import TreeStore
from engine.reporting import save_report, finalize_v4
from test_v4_tree import surface


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), "a" * 32, {"experiment_mode": "v4", "target": "https://example.test/",
            "budgets": {"finalize_seconds": 10}, "managed_processes": [], "lab_reset_status": "not_applicable"})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.tree.initialize("https://example.test/")

    def test_report_counts_claims_separately_from_verified_candidates(self):
        self.tree.discover("N0001", surface("https://example.test/q", "candidate"))
        report = save_report(self.log, "budget_exhausted", "call_budget", state=self.tree.state)
        self.assertIsNone(report["stats"]["confirmed"])
        self.assertIn("자동 취약점 판정 없음", report["summary"])
        self.assertEqual(report["stats"]["candidates"], 1)
        self.assertEqual(report["stats"]["unexplored"], 2)
        self.assertEqual(report["stop_reason"], "call_budget")
        self.assertTrue((self.log.run_dir / "report.json").is_file())

    def test_report_exposes_unverified_candidate_and_its_discovery_evidence(self):
        attempt = self.tree.begin_attempt("N0001", call_id="CALL-0001")
        self.log.append("exec_started", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command"})
        work = self.log.run_dir / "runtime" / "work"
        work.mkdir(parents=True)
        (work / "search.txt").write_text("Status: 500\nSQLITE_ERROR", encoding="utf-8")
        result_path = self.log.run_dir / "runtime" / attempt["result_path"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"schema_version": 1, "attempt_id": attempt["attempt_id"],
            "node_id": "N0001", "discovered": [surface("https://example.test/search?q=", "candidate")],
            "proposed_verdict": "confirmed", "claim": "SQL error proves injection", "next_observation": "",
            "evidence_refs": [{"path": "work/search.txt", "description": "Original search response"}]}), encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command",
                         "exit_code": 0, "timed_out": False, "cancelled": False})
        self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-0001")

        report = save_report(self.log, "budget_exhausted", "call_budget", state=self.tree.state)

        self.assertEqual(len(report["candidate_leads"]), 1)
        lead = report["candidate_leads"][0]
        self.assertEqual(lead["node_id"], "N0002")
        self.assertEqual(lead["hypothesis"]["type"], "sqli")
        self.assertEqual(lead["locator"]["input"], "q")
        self.assertFalse(lead["test_attempted"])
        self.assertIsNone(lead["candidate_proposed_verdict"])
        self.assertEqual(lead["discovery_claim"], "SQL error proves injection")
        self.assertEqual(len(lead["discovery_evidence_refs"]), 1)
        self.assertEqual(lead["discovery_evidence_refs"][0]["description"], "Original search response")
        self.assertIn("후보 단서: N0002 sqli", report["summary"])
        self.assertIn("모델 기록(미확인, N0002): SQL error proves injection", report["summary"])
        self.assertIsNone(report["stats"]["confirmed"])

    def test_report_does_not_call_an_uninvestigated_tree_completed(self):
        report = save_report(self.log, "completed", "tree_exhausted", state=self.tree.state)
        self.assertEqual(report["run_result"], "incomplete")
        self.assertEqual(report["stop_reason"], "investigation_incomplete")
        self.assertEqual(report["stats"]["incomplete_nodes"], 1)
        self.assertEqual(report["incomplete_nodes"][0]["node_id"], "N0001")
        self.assertEqual(self.log.metadata["run_result"], "incomplete")

    def test_degraded_report_stops_at_corrupt_prefix_and_unknown_totals_stay_null(self):
        self.log.events_path.write_text(self.log.events_path.read_text(encoding="utf-8") + "{broken\n" +
            json.dumps({"kind": "node_verification_recorded", "seq": 3, "run_id": self.log.run_id}) + "\n", encoding="utf-8")
        report = save_report(self.log, "interrupted", "log_failure", error="corrupt event")
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["last_readable_seq"], 1)
        self.assertIsNone(report["stats"]["confirmed"])
        self.assertEqual(report["corruption"]["line"], 2)

    def test_normal_finalization_reports_before_export_and_destroy(self):
        runtime = CommandRuntime(Path(self.temp.name), self.log)
        runtime.start_session(self.log.run_id, browser=False)
        order = []
        original_export, original_destroy = runtime.export_evidence, runtime.destroy
        def export(run_id):
            self.assertTrue((self.log.run_dir / "report.json").is_file())
            order.append("export")
            return original_export(run_id)
        def destroy(run_id, timeout):
            order.append("destroy")
            return original_destroy(run_id, timeout)
        with patch.object(runtime, "export_evidence", side_effect=export), patch.object(runtime, "destroy", side_effect=destroy):
            finalize_v4(self.log, runtime, "completed", "tree_exhausted", lab_reset=lambda: self.fail("No remote-target reset"))
        self.assertEqual(order, ["export", "destroy"])
        self.assertEqual(self.log.metadata["phase"], "finished")
        self.assertEqual(self.log.metadata["run_result"], "incomplete")
        report = json.loads((self.log.run_dir / "report.json").read_text(encoding="utf-8"))
        final = next(event for event in self.log.snapshot_events() if event["kind"] == "run_finalized")
        self.assertEqual(final["run_result"], report["run_result"])
        self.assertEqual(final["reason"], report["stop_reason"])
        self.assertFalse(runtime.runtime_dir.exists())

    def test_report_failure_retains_runtime_and_requires_recovery(self):
        runtime = CommandRuntime(Path(self.temp.name), self.log)
        runtime.start_session(self.log.run_id, browser=False)
        with patch("engine.reporting.save_report", side_effect=OSError("disk full")):
            finalize_v4(self.log, runtime, "error", "test_failure", lab_reset=lambda: None)
        self.assertTrue(runtime.runtime_dir.exists())
        self.assertEqual(self.log.metadata["phase"], "recovery_required")

    def test_emergency_stop_does_not_write_to_failed_journal(self):
        import psutil
        runtime = CommandRuntime(Path(self.temp.name), self.log)
        runtime.restore_owned(self.log.run_id, [{"pid": 55555, "created_at": 1.0, "run_id": self.log.run_id, "exec_id": "EXEC-0001"}])
        with patch("engine.command_runtime.psutil.Process", side_effect=psutil.NoSuchProcess(55555)), \
             patch.object(self.log, "append", side_effect=AssertionError("No writes")), \
             patch.object(self.log, "update", side_effect=AssertionError("No writes")):
            runtime.stop(self.log.run_id, record=False)
        self.assertEqual(runtime.inspect(self.log.run_id)["managed_processes"], [])


if __name__ == "__main__":
    unittest.main()

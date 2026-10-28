import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from command_runtime import CommandRuntime
from session_log import SessionLog
from v4_tree import TreeStore
from v4_reporting import save_report, finalize_v4
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
        self.assertEqual(report["stats"]["confirmed"], 0)
        self.assertEqual(report["stats"]["candidates"], 1)
        self.assertEqual(report["stats"]["unexplored"], 2)
        self.assertEqual(report["stop_reason"], "call_budget")
        self.assertTrue((self.log.run_dir / "report.json").is_file())

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
        self.assertFalse(runtime.runtime_dir.exists())

    def test_report_failure_retains_runtime_and_requires_recovery(self):
        runtime = CommandRuntime(Path(self.temp.name), self.log)
        runtime.start_session(self.log.run_id, browser=False)
        with patch("v4_reporting.save_report", side_effect=OSError("disk full")):
            finalize_v4(self.log, runtime, "error", "test_failure", lab_reset=lambda: None)
        self.assertTrue(runtime.runtime_dir.exists())
        self.assertEqual(self.log.metadata["phase"], "recovery_required")

    def test_emergency_stop_does_not_write_to_failed_journal(self):
        import psutil
        runtime = CommandRuntime(Path(self.temp.name), self.log)
        runtime.restore_owned(self.log.run_id, [{"pid": 55555, "created_at": 1.0, "run_id": self.log.run_id, "exec_id": "EXEC-0001"}])
        with patch("command_runtime.psutil.Process", side_effect=psutil.NoSuchProcess(55555)), \
             patch.object(self.log, "append", side_effect=AssertionError("No writes")), \
             patch.object(self.log, "update", side_effect=AssertionError("No writes")):
            runtime.stop(self.log.run_id, record=False)
        self.assertEqual(runtime.inspect(self.log.run_id)["managed_processes"], [])


if __name__ == "__main__":
    unittest.main()

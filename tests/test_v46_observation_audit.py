import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.command_runtime import CommandRuntime
from engine.http_audit import import_exchange
from engine.reporting import SnapshotLog, finalize_v4, save_report
from engine.risk_rubric import AUDIT_PROJECTION_VERSION, build_ordinal_request
from engine.scorer_session import RiskScorer
from engine.scorer_state import context
from engine.session_log import SessionLog
from engine.tree import TreeStore
from test_scorer_session import FakeAdapter, PIN
from test_v4_tree import surface


class ObservationAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.store = self.directory / "runs"
        self.target = "https://example.test/"
        self.log = SessionLog(self.store, "a" * 32, {
            "experiment_mode": "v4", "target": self.target, "managed_processes": [],
            "scorer_provider": "openrouter", "scorer_model": PIN["id"],
            "scorer_settings": {"reevaluation_policy": AUDIT_PROJECTION_VERSION},
        })
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize(self.target)
        self.discovery = surface(self.target + "items?q=", "candidate")
        self.discovery["hypothesis"]["statement"] = "PRIVATE MODEL CONCLUSION"
        self.candidate = self.tree.discover(self.root, self.discovery)

    def test_model_observations_are_absent_from_the_score_and_its_cache_key(self):
        scorer = RiskScorer(self.tree, adapter=FakeAdapter())
        self.tree.set_observations(self.root, {"data_exposure": "sensitive"})
        self.tree.set_observations(self.candidate, {"reproduced": True, "security_signal": "query_injection"})
        first = scorer.score_children(self.root)
        saved = self.tree.refresh()["scorer"]
        self.assertEqual(saved["configuration"]["projection_version"], AUDIT_PROJECTION_VERSION)
        state = saved["requests"]["S000001"]["request"]["state"]
        self.assertEqual(state["projection_version"], AUDIT_PROJECTION_VERSION)
        self.assertEqual(state["parent"]["observations"], {})
        self.assertEqual(state["nodes"][self.candidate]["observations"], {})
        view = context(self.tree.refresh(), self.root, saved["configuration"])
        ordinal = build_ordinal_request("test", view["parent"], view["siblings"], [self.candidate])
        self.assertEqual(ordinal["state"], state)
        self.tree.set_observations(self.candidate, {"query_behavior": "controlled_difference"})
        self.assertEqual(first["context_key"], scorer.score_children(self.root)["context_key"])
        replayed = TreeStore(self.log).rebuild()
        self.assertEqual(replayed["scorer"]["requests"], saved["requests"])
        self.assertEqual(replayed["nodes"][self.candidate]["observations"]["query_behavior"], "controlled_difference")

    def record_result(self):
        runtime = CommandRuntime(self.store, self.log)
        runtime.start_session(self.log.run_id, target=self.target, browser=False)
        attempt = self.tree.begin_attempt(self.candidate, call_id="CALL-1")
        identity = {"exec_id": "EXEC-1", "call_id": "CALL-1", "action": "run_command"}
        self.log.append("exec_started", identity)
        output = runtime.runtime_dir / attempt["result_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"schema_version": 1, "node_id": self.candidate, "attempt_id": attempt["attempt_id"],
            "discovered": [], "proposed_verdict": "confirmed", "claim": "PRIVATE MODEL VERDICT",
            "observations": {"response_status": 200, "baseline_status": 404, "reproduced": True},
            "next_observation": "PRIVATE FOLLOWUP", "evidence_refs": []}), encoding="utf-8")
        self.log.append("exec_finished", {**identity, "exit_code": 0, "timed_out": False, "cancelled": False})
        self.tree.import_result(attempt["attempt_id"], exec_id="EXEC-1")
        return runtime, attempt["attempt_id"]

    def test_report_exposes_candidate_without_its_unverified_prose(self):
        self.record_result()
        report = save_report(self.log, "incomplete", "investigation_incomplete", state=self.tree.refresh())
        self.assertEqual(report["candidate_leads"][0]["node_id"], self.candidate)
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("candidate_claim", report["candidate_leads"][0])
        self.assertIsNone(report["stats"]["confirmed"])
        self.assertEqual(report["http_audit"]["status"], "not_recorded")

    def test_offline_originals_are_bound_to_attempt_and_reviewed_without_changing_scores(self):
        runtime, attempt = self.record_result()
        request, response = self.directory / "request.http", self.directory / "response.http"
        request.write_bytes(b"GET /items?q=book HTTP/1.1\r\nHost: example.test\r\n\r\n")
        response.write_bytes(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nPRIVATE\x00\xff")
        args = (self.store, self.log.run_id, attempt, "R-1", request, response)
        with self.assertRaisesRegex(ValueError, "finalized"):
            import_exchange(*args)
        state = self.tree.refresh()
        finalize_v4(self.log, runtime, "incomplete", "stopped", lab_reset=None)
        before = self.log.events_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "outside the run store"):
            import_exchange(self.store, self.log.run_id, attempt, "R-1", self.log.events_path, response)
        wrong = self.directory / "wrong.http"
        wrong.write_bytes(b"GET /items HTTP/1.1\r\nHost: other.example.test\r\n\r\n")
        with self.assertRaisesRegex(ValueError, "differs from the selected node"):
            import_exchange(self.store, self.log.run_id, attempt, "R-1", wrong, response)
        wrong.write_bytes(b"GET /items?other=book HTTP/1.1\r\nHost: example.test\r\n\r\n")
        with self.assertRaisesRegex(ValueError, "query differs"):
            import_exchange(self.store, self.log.run_id, attempt, "R-1", wrong, response)
        receipt = import_exchange(*args, field="response_status")
        self.assertEqual(receipt["comparison"]["state"], "supported")
        self.assertEqual(receipt, import_exchange(*args, field="response_status"))
        self.assertEqual((self.log.run_dir / receipt["request"]["path"]).read_bytes(), request.read_bytes())
        self.assertEqual((self.log.run_dir / receipt["response"]["path"]).read_bytes(), response.read_bytes())
        with self.assertRaisesRegex(ValueError, "already bound"):
            import_exchange(*args, field="baseline_status")
        second = import_exchange(self.store, self.log.run_id, attempt, "R-2", request, response, field="baseline_status")
        self.assertEqual(second["comparison"], {"field": "baseline_status", "state": "contradicted"})
        self.assertEqual(before, self.log.events_path.read_bytes())
        self.assertNotIn("PRIVATE", (self.log.run_dir / "report.json").read_text(encoding="utf-8"))
        frozen = SnapshotLog(self.store, self.log.metadata)
        report = save_report(frozen, "incomplete", "stopped", state=state, persist_metadata=False)
        self.assertEqual(report["http_audit"]["exchanges"], [receipt, second])
        (self.log.run_dir / receipt["request"]["path"]).write_bytes(b"changed")
        report = save_report(frozen, "incomplete", "stopped", state=state, persist_metadata=False)
        self.assertEqual(report["http_audit"]["status"], "unavailable")
        self.assertEqual(report["http_audit"]["exchanges"], [])
        (self.log.run_dir / receipt["request"]["path"]).write_bytes(request.read_bytes())
        restored = save_report(frozen, "incomplete", "stopped", state=state, persist_metadata=False)
        self.assertEqual(restored["http_audit"]["exchanges"], [receipt, second])

    def test_retry_after_report_write_failure_does_not_duplicate_the_receipt(self):
        from engine.session_log import atomic_write_json

        runtime, attempt = self.record_result()
        finalize_v4(self.log, runtime, "incomplete", "stopped", lab_reset=None)
        request, response = self.directory / "request", self.directory / "response"
        request.write_bytes(b"GET /items?q=book HTTP/1.1\r\nHost: example.test\r\n\r\n")
        response.write_bytes(b"HTTP/1.1 204 No Content\r\n\r\n")
        args = (self.store, self.log.run_id, attempt, "R-3", request, response)

        def fail_report(path, value):
            if path.name == "report.json":
                raise OSError("report write interrupted")
            return atomic_write_json(path, value)

        with patch("engine.http_audit.atomic_write_json", side_effect=fail_report):
            with self.assertRaisesRegex(OSError, "interrupted"):
                import_exchange(*args)
        receipt = import_exchange(*args)
        self.assertNotIn("comparison", receipt)
        report = json.loads((self.log.run_dir / "report.json").read_bytes())
        self.assertEqual(report["http_audit"]["exchanges"], [receipt])

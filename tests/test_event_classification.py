import json
import tempfile
import unittest
from pathlib import Path

from event_classification import classify_event
from session_log import SessionLog, SessionStore


class EventClassificationTests(unittest.TestCase):
    def test_failures_controls_and_observations_are_distinct(self):
        cases = [
            ("exec_finished", {"exit_code": 0, "stdout": "HTTP 404, 403, error"}, "execution", "succeeded"),
            ("exec_finished", {"exit_code": 1, "failure_source": "generated_code"}, "execution", "failed"),
            ("exec_finished", {"timed_out": True}, "execution", "timed_out"),
            ("exec_finished", {"cancelled": True}, "execution", "cancelled"),
            ("exec_finished", {"exit_code": 1, "failure_source": "system"}, "system", "system_error"),
            ("model_error", {"error_kind": "format"}, "model", "failed"),
            ("model_error", {"error_kind": "output_limit"}, "model", "failed"),
            ("model_error", {"error_kind": "transport"}, "system", "failed"),
            ("model_error", {"error_kind": "cancelled"}, "lifecycle", "cancelled"),
            ("model_error", {"error_kind": "budget"}, "lifecycle", "exhausted"),
            ("exec_output", {"channel": "stderr", "text": "HTTP 500"}, "observation", "observed"),
            ("http_response", {"status": 404}, "observation", "observed"),
            ("run_finalized", {"run_result": "error"}, "lifecycle", "succeeded"),
            ("finalize_failed", {"error": "cannot save"}, "system", "failed"),
        ]
        for kind, values, category, outcome in cases:
            with self.subTest(kind=kind, values=values):
                result = classify_event(kind, values)
                self.assertEqual((result["category"], result["outcome"]), (category, outcome))

    def test_persisted_and_streamed_classification_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "a" * 32, {})
            streamed = []
            log.listen(streamed.append)
            event = log.append("exec_finished", {"call_id": "CALL-1", "exec_id": "EXEC-1",
                "action": "run_python", "exit_code": 1, "failure_source": "generated_code",
                "cause_code": "python_exception", "exception": "SyntaxError: original", "retryable": True})
            self.assertEqual(event, streamed[0])
            self.assertEqual(event, log.read_events()[0])
            self.assertEqual(event, SessionStore(Path(temp)).read_events(log.run_id)[0])
            self.assertEqual(event["component"], "browser_worker")
            self.assertEqual(event["message"], "SyntaxError: original")
            self.assertEqual(event["cause_code"], "python_exception")
            self.assertTrue(event["retryable"])
            log.close()

    def test_legacy_reads_preserve_bytes_and_do_not_guess_failure_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "b" * 32, {})
            old = {"run_id": log.run_id, "seq": 1, "at": 1, "kind": "exec_finished",
                   "action": "run_python", "exit_code": 1, "exception": "Browser worker exited"}
            original = (json.dumps(old) + "\n").encode()
            log.events_path.write_bytes(original)
            for event in (log.read_events()[0], SessionStore(Path(temp)).read_events(log.run_id)[0]):
                self.assertEqual(event["outcome"], "failed")
                self.assertEqual(event["failure_source"], "unknown")
                self.assertEqual(event["cause_code"], "legacy_execution_failure")
            self.assertEqual(log.events_path.read_bytes(), original)
            log.close()

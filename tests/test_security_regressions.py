"""Security boundaries; model/runtime doubles never launch a browser or call a model."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from command_runtime import ExecutionResult
from explorer import ExplorerV3, context_char_budget, compose_report
from harness import Harness, ModelFailure
from scorecard import score_run
from session_log import SessionLog


RUN_ID = "f" * 32


def answer(action, **values):
    command = dict(action=action, plan="inspect", reason="model claim", path="", content="", command="", code="")
    return {"message": {"content": json.dumps({**command, **values})}, "done": True, "done_reason": "stop"}


class EvidenceAndContextTests(unittest.TestCase):
    def run_loop(self, replies, result=None):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), RUN_ID, {
                "goal": "inspect", "target": "http://127.0.0.1:3000/", "model": "mock",
                "model_settings": {"think": False, "options": {"num_ctx": 8192, "num_predict": 1024}},
                "budgets": {"seconds": 1200, "model_calls": 20, "tool_calls": 8, "command_seconds": 30}})
            self.addCleanup(log.close)
            replies = iter(replies)
            sent = []
            def transport(payload):
                sent.append(json.loads(json.dumps(payload)))
                yield next(replies)
            runtime = Mock(runtime_dir=Path(directory), python="python")
            runtime.run.return_value = result or ExecutionResult("EXEC-0001", "run_command", stdout="CONFIRMED SQLi", exit_code=0)
            explorer = ExplorerV3(Path(directory), transport=transport, browser_enabled=False)
            outcome, reason = explorer._loop(RUN_ID, log, runtime)
            return outcome, reason, dict(log.metadata), sent

    def test_printed_claim_is_not_evidence_or_a_completed_investigation(self):
        outcome, reason, state, _ = self.run_loop([answer("run_command", command="echo CONFIRMED SQLi"), answer("finish")])
        self.assertFalse(state["findings"][0]["evidence"])
        self.assertEqual(outcome, "inconclusive")
        report = compose_report(state["findings"], outcome, reason)
        self.assertNotIn("확인된 관찰:\n", report)

    def test_immediate_finish_is_inconclusive(self):
        outcome, _, _, _ = self.run_loop([answer("finish")])
        self.assertEqual(outcome, "inconclusive")

    def test_legacy_evidence_flag_and_empty_finish_cannot_earn_quality_points(self):
        for findings in ([], [{"step": 1, "evidence": True, "status": "ok", "observed": "made up"}]):
            with self.subTest(findings=findings):
                row = score_run({"findings": findings, "run_result": "completed"}, Path("missing-run"))
                self.assertEqual(row["evidence"], 0)
                self.assertEqual(row["score"], 0)

    def test_long_recent_command_never_exceeds_context_budget(self):
        _, _, _, sent = self.run_loop([answer("run_command", command="x" * 30000), answer("finish")])
        for payload in sent:
            context = payload["messages"][1]["content"]
            self.assertLessEqual(len(context), context_char_budget(8192))

    def test_retry_feedback_is_bounded_and_preserves_the_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), RUN_ID, {})
            self.addCleanup(log.close)
            sent = []
            def transport(payload):
                sent.append(json.loads(json.dumps(payload)))
                yield {"message": {"content": "{" + "x" * 20000}, "done": True, "done_reason": "stop"}
            harness = Harness(log, transport=transport, max_calls=6)
            with self.assertRaises(ModelFailure):
                harness.decide(RUN_ID, {"goal": "keep this", "recent": [{"stdout": "x" * 30000}]},
                               model="mock", settings={"think": False, "options": {"num_ctx": 8192}})
            for payload in sent:
                self.assertIn("keep this", payload["messages"][1]["content"])
                self.assertLessEqual(sum(len(m["content"]) for m in payload["messages"][1:]), context_char_budget(8192))

    def test_tiny_context_fails_before_the_model_is_called(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), RUN_ID, {})
            self.addCleanup(log.close)
            transport = Mock()
            with self.assertRaises(ModelFailure) as caught:
                Harness(log, transport=transport).decide(RUN_ID, {'goal': 'inspect'}, model='mock',
                                                       settings={'options': {'num_ctx': 512}})
            self.assertEqual(caught.exception.kind, 'context')
            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()

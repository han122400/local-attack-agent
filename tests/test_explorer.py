"""Run-scoped lifecycle and target regression tests."""

import json
import tempfile
import unittest
from pathlib import Path

from explorer import ExplorerV3, safe_target, validate_budgets
from session_log import SessionStore


def reply(action, **values):
    body = {"action": action, "plan": "inspect", "reason": "observe", "path": "", "content": "",
            "command": "", "code": ""}
    body.update(values)
    return {"message": {"content": json.dumps(body)}}


class V3ExplorerTests(unittest.TestCase):
    def test_target_and_budgets_are_scoped(self):
        self.assertEqual(safe_target("http://127.0.0.1:3000/#/"), "http://127.0.0.1:3000/#/")
        for target in ("http://example.com/", "http://127.0.0.1:3001/", "http://127.0.0.1:3000.evil.example/"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                safe_target(target)
        self.assertEqual(validate_budgets(None)["tool_calls"], 80)
        with self.assertRaises(ValueError):
            validate_budgets({"tool_calls": 0})

    def test_command_failure_is_returned_for_model_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([reply("run_command", command="exit /b 7"), reply("run_command", command="echo fixed"),
                            reply("finish")])
            prompts = []
            def transport(payload):
                prompts.append(json.loads(payload["messages"][1]["content"]))
                yield next(answers)
            explorer = ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True})
            run_id = explorer.start(goal="inspect")
            explorer.worker.join(10)
            self.assertFalse(explorer.worker.is_alive())
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["run_result"], "completed")
            self.assertEqual(prompts[1]["recent"][-1]["result"]["exit_code"], 7)
            self.assertIn("fixed", prompts[2]["recent"][-1]["result"]["stdout"])

    def test_model_failure_and_cleanup_failure_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            def bad(_payload):
                yield {"message": {"content": "{"}}
                yield {"done": True, "done_reason": "length"}
            explorer = ExplorerV3(Path(temp), transport=bad, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True})
            run_id = explorer.start(goal="inspect")
            explorer.worker.join(10)
            meta = SessionStore(Path(temp)).read(run_id)
            self.assertEqual(meta["run_result"], "error")
            self.assertEqual(meta["cleanup_status"], "succeeded")
            self.assertIn("Model output_limit", meta["finish_reason"])


if __name__ == "__main__":
    unittest.main()

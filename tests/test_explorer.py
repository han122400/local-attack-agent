"""Run-scoped lifecycle and target regression tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(safe_target("https://my-site.example:8443/app"), "https://my-site.example:8443/app")
        self.assertEqual(safe_target("http://localhost:5173/"), "http://localhost:5173/")
        for target in ("file:///etc/passwd", "javascript:alert(1)", "http://user:pass@example.com/",
                       "http://example.com/\nmalicious", "http://example.com:99999/", "http://example.com\\@evil.test/"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                safe_target(target)
        with self.assertRaises(ValueError):
            safe_target("https://other.example/", allowed_origin="https://my-site.example/")
        self.assertEqual(validate_budgets(None)["tool_calls"], 80)
        with self.assertRaises(ValueError):
            validate_budgets({"tool_calls": 0})

    def test_custom_site_skips_juice_reset_and_frees_next_run(self):
        with tempfile.TemporaryDirectory() as temp:
            reset_calls = []
            def reset():
                reset_calls.append(True)
                return {"verified": True}
            explorer = ExplorerV3(Path(temp), transport=lambda _payload: iter([reply("finish")]),
                                  browser_enabled=False, lab_reset=reset)
            run_id = explorer.start(target="https://my-site.example/app", goal="inspect")
            explorer.worker.join(10)
            self.assertFalse(explorer.worker.is_alive())
            state = explorer.get_run(run_id)
            self.assertEqual(state["target"], "https://my-site.example/app")
            self.assertEqual(state["run_result"], "inconclusive")
            self.assertEqual(state["lab_reset_status"], "not_applicable")
            self.assertEqual(state["initial_lab_status"], "not_applicable")
            self.assertEqual(reset_calls, [])
            self.assertTrue(explorer.store.can_start()[0])

    def test_command_failure_is_returned_for_model_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([reply("run_command", command="exit 7"), reply("run_command", command="echo fixed"),
                            reply("finish")])
            prompts = []
            def transport(payload):
                prompts.append(json.loads(payload["messages"][1]["content"]))
                yield next(answers)
            explorer = ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True})
            def command(runtime, text, exec_id, timeout):
                if text == 'exit 7':
                    return 7
                runtime._add_output(exec_id, 'stdout', 'fixed\n')
                return 0
            with patch('command_runtime.CommandRuntime._run_command', autospec=True, side_effect=command):
                run_id = explorer.start(goal="inspect")
                explorer.worker.join(10)
            self.assertFalse(explorer.worker.is_alive())
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["run_result"], "inconclusive")
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

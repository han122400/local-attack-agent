"""Model input packing and execution feedback; no browser, shell or live model."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from command_runtime import ExecutionResult
from explorer import ExplorerV3, _pack_model_context, context_char_budget
from harness import ModelFailure
from session_log import SessionLog


def answer(action, **fields):
    command = dict(action=action, plan="inspect", reason="model claim", path="", content="", command="", code="")
    return {"message": {"content": json.dumps({**command, **fields})}, "done": True, "done_reason": "stop"}


class ModelContextTests(unittest.TestCase):
    def run_loop(self, commands, results, *, num_ctx=8192):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = SessionLog(root, "a" * 32, {
                "goal": "Keep the original investigation goal", "target": "http://127.0.0.1:3000/",
                "model": "fixture", "model_settings": {"think": False, "options": {"num_ctx": num_ctx}},
                "budgets": {"seconds": 1200, "model_calls": 20, "tool_calls": 10, "command_seconds": 30}})
            sent, executed = [], []
            commands, results = iter(commands), iter(results)
            def transport(payload):
                sent.append(json.loads(payload["messages"][1]["content"]))
                yield next(commands)
            def run(run_id, payload, **kwargs):
                executed.append(payload)
                result = next(results)
                self.assertEqual(result.action, payload["action"])
                return result
            runtime = SimpleNamespace(runtime_dir=root / "runtime", python="python", run=run)
            try:
                result = ExplorerV3(root, transport=transport, browser_enabled=False)._loop(log.run_id, log, runtime)
                return result, sent, executed, dict(log.metadata), log.read_events(limit=1000)
            finally:
                log.close()

    def test_large_latest_command_is_previewed_but_executed_and_logged_in_full(self):
        code = "BEGIN\n" + "x" * 30000 + "\nEND"
        output = "IMPORTANT_PREFIX\n" + "z" * 30000 + "\nIMPORTANT_SUFFIX"
        _, sent, executed, _, events = self.run_loop(
            [answer("run_python", code=code), answer("finish")],
            [ExecutionResult("EXEC-1", "run_python", stdout=output, exit_code=0)])
        preview = sent[1]["recent"][-1]
        self.assertIn("BEGIN", preview["command"]["code"])
        self.assertIn("END", preview["command"]["code"])
        self.assertIn("IMPORTANT_PREFIX", preview["result"]["stdout"])
        self.assertIn("IMPORTANT_SUFFIX", preview["result"]["stdout"])
        self.assertLessEqual(len(json.dumps(sent[1], ensure_ascii=False)), context_char_budget(8192))
        self.assertEqual(executed[0]["code"], code)
        response = next(e for e in events if e["kind"] == "model_response")
        self.assertEqual(json.loads(response["output"])["code"], code)

    def test_command_and_error_detail_fit_small_context_without_losing_goal(self):
        _, sent, _, _, _ = self.run_loop(
            [answer("run_command", command="HEAD" + "x" * 30000 + "TAIL"), answer("finish")],
            [ExecutionResult("EXEC-1", "run_command", exit_code=1,
                             stderr="DETAIL_START" + "y" * 20000 + "DETAIL_END", exception="e" * 10000)], num_ctx=4096)
        self.assertLessEqual(len(json.dumps(sent[1], ensure_ascii=False)), context_char_budget(4096))
        self.assertEqual(sent[1]["goal"], "Keep the original investigation goal")
        self.assertIn("DETAIL_END", sent[1]["recent"][-1]["result"]["stderr"])

    def test_identical_failure_has_feedback_but_does_not_skip_or_finish(self):
        failure = lambda i: ExecutionResult(f"EXEC-{i}", "run_python", exit_code=1,
            exception=f'Traceback in EXEC-{i}.py\nNameError: name missing is not defined', exception_type="NameError")
        command = answer("run_python", code="print(missing)")
        result, sent, executed, state, _ = self.run_loop(
            [command] * 4 + [answer("finish")], [failure(i) for i in range(4)])
        self.assertEqual(len(executed), 4)
        self.assertEqual(result[0], "inconclusive")
        self.assertIn("feedback", sent[2])
        self.assertEqual(sent[2]["feedback"]["repeat_of"], 1)
        self.assertIn("NameError", sent[2]["feedback"]["error"])
        self.assertEqual(len(state["findings"]), 4)

    def test_different_command_with_same_output_is_not_a_repeat(self):
        _, sent, _, _, _ = self.run_loop(
            [answer("read_file", path="work/a.json"), answer("read_file", path="work/b.json"), answer("finish")],
            [ExecutionResult("EXEC-1", "read_file", stdout="[]", exit_code=0),
             ExecutionResult("EXEC-2", "read_file", stdout="[]", exit_code=0)])
        self.assertNotIn("note", sent[2])
        self.assertNotIn("repeat_of", sent[2]["history"][-1])

    def test_output_change_in_the_middle_is_not_hidden_by_digest(self):
        command = answer("run_command", command="read fixture")
        first = "same head " * 40 + "OLD" + " same tail" * 40
        second = first.replace("OLD", "NEW")
        _, sent, _, _, _ = self.run_loop([command, command, answer("finish")],
            [ExecutionResult("EXEC-1", "run_command", stdout=first, exit_code=0),
             ExecutionResult("EXEC-2", "run_command", stdout=second, exit_code=0)])
        self.assertNotIn("note", sent[2])

    def test_revised_code_with_same_error_is_not_an_unchanged_retry(self):
        results = [ExecutionResult(f"EXEC-{i}", "run_python", exit_code=1, exception="NameError: missing") for i in (1, 2)]
        _, sent, _, _, _ = self.run_loop(
            [answer("run_python", code="print(missing)"), answer("run_python", code="print(missing + 1)"), answer("finish")], results)
        self.assertIsNone(sent[2]["feedback"]["repeat_of"])
        self.assertIn("NameError", sent[2]["feedback"]["error"])
        self.assertNotIn("note", sent[2])

    def test_intervening_preparation_resets_repeat_detection(self):
        read = answer("read_file", path="work/a.json")
        _, sent, _, _, _ = self.run_loop(
            [read, answer("write_file", path="work/a.json", content="updated"), read, answer("finish")],
            [ExecutionResult("EXEC-1", "read_file", stdout="[]", exit_code=0),
             ExecutionResult("EXEC-2", "write_file", exit_code=0),
             ExecutionResult("EXEC-3", "read_file", stdout="[]", exit_code=0)])
        self.assertNotIn("note", sent[3])

    def test_required_goal_is_not_silently_truncated_to_fit(self):
        context = {"goal": "must retain this goal" * 100, "history": [], "recent": []}
        with self.assertRaises(ModelFailure) as failure:
            _pack_model_context(context, 200)
        self.assertEqual(failure.exception.kind, "context")
        self.assertEqual(context["goal"], "must retain this goal" * 100)


if __name__ == "__main__":
    unittest.main()

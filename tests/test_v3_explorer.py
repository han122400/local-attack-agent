import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from explorer import ExplorerV3
from session_log import SessionStore


def model_reply(action, **fields):
    data = {"action": action, "plan": "inspect", "reason": "next step",
            "path": "", "content": "", "command": "", "code": ""}
    data.update(fields)
    return {"message": {"content": json.dumps(data)}}


class ExplorerLoopTests(unittest.TestCase):
    def setUp(self):
        def command(runtime, text, exec_id, timeout):
            output = ((runtime.runtime_dir / 'work/note.txt').read_text() if text == 'cat work/note.txt'
                      else text.removeprefix('echo ') + '\n')
            runtime._add_output(exec_id, 'stdout', output)
            return 0
        runner = patch('command_runtime.CommandRuntime._run_command', autospec=True, side_effect=command)
        runner.start()
        self.addCleanup(runner.stop)

    def test_model_writes_executes_observes_and_finishes_then_cleans(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([model_reply("write_file", path="work/note.txt", content="hello"),
                            model_reply("run_command", command="cat work/note.txt"),
                            model_reply("finish")])
            prompts = []
            def transport(payload):
                prompts.append(json.loads(payload["messages"][1]["content"]))
                yield next(answers)
                yield {"done": True, "done_reason": "stop"}
            explorer = ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True, "probe": "mock"})
            run_id = explorer.start(goal="inspect local app", model="mock:model")
            self.assertIsInstance(run_id, str)
            explorer.worker.join(10)
            self.assertFalse(explorer.worker.is_alive())
            meta = SessionStore(Path(temp)).read(run_id)
            self.assertEqual(meta["run_result"], "inconclusive")
            self.assertEqual(meta["cleanup_status"], "succeeded")
            self.assertEqual(meta["lab_reset_status"], "succeeded")
            self.assertEqual(meta["scenario_source"], "manual")
            self.assertEqual(meta["chain_mode"], "none")
            self.assertFalse((Path(temp) / run_id / "runtime").exists())
            self.assertTrue((Path(temp) / run_id / "events.jsonl").is_file())
            self.assertIn("hello", json.dumps(prompts[2]))
            self.assertTrue(explorer.can_start()[0])

    def test_history_memory_accumulates_and_findings_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([model_reply("write_file", path="work/a.txt", content="alpha"),
                            model_reply("run_command", command="echo beta"),
                            model_reply("finish")])
            prompts = []
            def transport(payload):
                prompts.append(json.loads(payload["messages"][1]["content"]))
                yield next(answers)
                yield {"done": True, "done_reason": "stop"}
            explorer = ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True, "probe": "mock"})
            run_id = explorer.start(goal="inspect local app", model="mock:model")
            explorer.worker.join(10)
            # The finish decision must see both prior steps in cumulative history.
            finish_ctx = prompts[2]
            self.assertEqual([h["action"] for h in finish_ctx["history"]], ["write_file", "run_command"])
            self.assertEqual(finish_ctx["history"][0]["status"], "ok")
            self.assertIn("finish_soon", finish_ctx["remaining"])
            # Findings (the step memory) are preserved in session.json.
            meta = SessionStore(Path(temp)).read(run_id)
            self.assertEqual(meta["run_result"], "inconclusive")
            self.assertEqual([f["action"] for f in meta["findings"]], ["write_file", "run_command"])

    def test_unsupported_modes_rejected_without_creating_run(self):
        with tempfile.TemporaryDirectory() as temp:
            explorer = ExplorerV3(Path(temp), browser_enabled=False)
            with self.assertRaises(ValueError):
                explorer.start(goal="inspect", scenario_source="auto")
            with self.assertRaises(ValueError):
                explorer.start(goal="inspect", chain_mode="campaign")
            self.assertEqual(SessionStore(Path(temp)).list_runs(), [])

    def test_recovery_resumes_incomplete_run_and_preserves_original_result(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            run_id = "a" * 32
            log = store.create(run_id, {"goal": "inspect", "target": "http://127.0.0.1:3000/",
                                        "run_result": "error", "finish_reason": "model failed"})
            log.append("original", {"message": "keep"})
            log.close()
            explorer = ExplorerV3(Path(temp), browser_enabled=False, lab_reset=lambda: {"verified": True})
            self.assertFalse(explorer.can_start()[0])
            self.assertTrue(explorer.recover(run_id))
            explorer.worker.join(10)
            meta = store.read(run_id)
            self.assertEqual(meta["run_result"], "error")
            self.assertEqual(meta["phase"], "finished")
            self.assertTrue(explorer.can_start()[0])
            events = [json.loads(line) for line in (Path(temp) / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["kind"], "original")
            self.assertTrue(any(item["kind"] == "recovery_opened" for item in events))

    def test_failed_reset_blocks_start_until_same_run_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([model_reply("finish")])
            def transport(_payload):
                yield next(answers)
            attempts = 0
            def reset():
                nonlocal attempts
                attempts += 1
                if attempts == 2:  # initial reset succeeds, final reset fails
                    raise RuntimeError("probe mismatch")
                return {"verified": True}
            explorer = ExplorerV3(Path(temp), transport=transport, browser_enabled=False, lab_reset=reset)
            run_id = explorer.start(goal="inspect")
            explorer.worker.join(10)
            self.assertFalse(explorer.can_start()[0])
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["run_result"], "inconclusive")
            self.assertTrue(explorer.recover(run_id))
            explorer.worker.join(10)
            self.assertTrue(explorer.can_start()[0])
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["run_result"], "inconclusive")

    def test_recovery_rebuilds_interrupted_creation_without_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            run_id = "b" * 32
            directory = Path(temp) / run_id
            directory.mkdir()
            (directory / "session.json").write_text("{broken", encoding="utf-8")
            explorer = ExplorerV3(Path(temp), browser_enabled=False,
                                  lab_reset=lambda: {"verified": True})
            self.assertFalse(explorer.can_start()[0])
            self.assertTrue(explorer.recover(run_id))
            explorer.worker.join(10)
            state = explorer.get_run(run_id)
            self.assertEqual(state["run_result"], "interrupted")
            self.assertEqual(state["phase"], "finished")
            self.assertTrue(state["metadata_rebuilt"])
            self.assertEqual(len(list((directory / "artifacts" / "recovery").glob("session-metadata-*.bin"))), 1)
            self.assertTrue(explorer.can_start()[0])


if __name__ == "__main__":
    unittest.main()

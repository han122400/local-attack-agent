import json
import tempfile
import unittest
from pathlib import Path

from explorer import ExplorerV3
from session_log import SessionStore


def model_reply(action, **fields):
    data = {"action": action, "plan": "inspect", "reason": "next step",
            "path": "", "content": "", "command": "", "code": ""}
    data.update(fields)
    return {"message": {"content": json.dumps(data)}}


class ExplorerLoopTests(unittest.TestCase):
    def test_model_writes_executes_observes_and_finishes_then_cleans(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([model_reply("write_file", path="work/note.txt", content="hello"),
                            model_reply("run_command", command="type work\\note.txt"),
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
            self.assertEqual(meta["run_result"], "completed")
            self.assertEqual(meta["cleanup_status"], "succeeded")
            self.assertEqual(meta["lab_reset_status"], "succeeded")
            self.assertEqual(meta["scenario_source"], "manual")
            self.assertEqual(meta["chain_mode"], "none")
            self.assertFalse((Path(temp) / run_id / "runtime").exists())
            self.assertTrue((Path(temp) / run_id / "events.jsonl").is_file())
            self.assertIn("hello", json.dumps(prompts[2]))
            self.assertTrue(explorer.can_start()[0])

    def test_unsupported_modes_rejected_without_creating_run(self):
        with tempfile.TemporaryDirectory() as temp:
            explorer = ExplorerV3(Path(temp), browser_enabled=False)
            with self.assertRaises(ValueError):
                explorer.start(goal="inspect", scenario_source="auto")
            with self.assertRaises(ValueError):
                explorer.start(goal="inspect", chain_mode="campaign")
            self.assertEqual(SessionStore(Path(temp)).list_runs(), [])


if __name__ == "__main__":
    unittest.main()

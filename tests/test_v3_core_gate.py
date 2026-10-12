"""Deterministic model feedback and evidence checks for the v3 loop."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from explorer import ExplorerV3


def reply(action, **values):
    answer = {"action": action, "plan": "inspect", "reason": "use evidence",
              "path": "", "content": "", "command": "", "code": ""}
    answer.update(values)
    return {"message": {"content": json.dumps(answer)}}


class CoreGateTests(unittest.TestCase):
    def test_stop_during_model_call_finalizes_without_running_command(self):
        with tempfile.TemporaryDirectory() as temp:
            entered = threading.Event()
            def transport(_payload):
                entered.set()
                while not explorer.stop_event.is_set():
                    time.sleep(0.01)
                yield reply("run_command", command="echo should-not-run")
            explorer = ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True})
            run_id = explorer.start(goal="inspect", model="mock:model")
            self.assertTrue(entered.wait(10))
            self.assertTrue(explorer.stop(run_id))
            explorer.worker.join(10)
            self.assertFalse(explorer.worker.is_alive())
            state = explorer.get_run(run_id)
            self.assertEqual(state["run_result"], "stopped")
            self.assertEqual(state["cleanup_status"], "succeeded")
            self.assertEqual(state["lab_reset_status"], "succeeded")
            self.assertFalse((Path(temp) / run_id / "runtime").exists())
            events = [json.loads(line) for line in (Path(temp) / run_id / "events.jsonl").read_text().splitlines()]
            self.assertFalse(any(item["kind"] == "exec_started" for item in events))

    def test_code_error_reaches_model_then_revision_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            answers = iter([reply("run_python", code="print(missing_name)"),
                            reply("run_python", code="print('repair worked')"),
                            reply("finish")])
            observations = []
            def transport(payload):
                observations.append(json.loads(payload["messages"][1]["content"]))
                yield next(answers)
                yield {"done": True, "done_reason": "stop"}
            explorer = ExplorerV3(Path(temp), transport=transport,
                                  lab_reset=lambda: {"verified": True, "probe": "mock"})
            run_id = explorer.start(goal="inspect local app", model="mock:model")
            explorer.worker.join(20)
            self.assertFalse(explorer.worker.is_alive())
            state = explorer.get_run(run_id)
            self.assertEqual(state["run_result"], "completed")
            self.assertEqual(state["phase"], "finished")
            self.assertIn("NameError", json.dumps(observations[1]))
            self.assertIn("repair worked", json.dumps(observations[2]))
            events = [json.loads(line) for line in (Path(temp) / run_id / "events.jsonl").read_text().splitlines()]
            executed = [event for event in events if event["kind"] == "exec_finished"]
            self.assertEqual([event["exit_code"] for event in executed], [1, 0])
            self.assertEqual([event["call_id"] for event in executed], ["CALL-0001", "CALL-0002"])
            self.assertEqual([event["exec_id"] for event in executed], ["EXEC-0001", "EXEC-0002"])
            self.assertTrue(any(event["kind"] == "exec_output" and event["call_id"] == "CALL-0002"
                                and event["exec_id"] == "EXEC-0002" and "repair worked" in event["text"]
                                for event in events))
            first_code = Path(temp) / run_id / executed[0]["artifact_path"]
            revised_code = Path(temp) / run_id / executed[1]["artifact_path"]
            self.assertTrue(first_code.is_file())
            self.assertTrue(revised_code.is_file())
            self.assertNotEqual(first_code.read_text(), revised_code.read_text())
            self.assertFalse((Path(temp) / run_id / "runtime").exists())


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from harness import Harness, ModelFailure
from planning import Command, validate_command
from session_log import SessionLog


RUN_ID = "d" * 32


class CommandContractTests(unittest.TestCase):
    def test_generic_commands_have_no_web_action_ids_or_search_limit(self):
        payload = {"action": "run_python", "plan": "Inspect rendered page", "reason": "Need current DOM",
                   "path": "", "content": "", "command": "", "code": "await page.goto(target)\nprint(await page.title())"}
        command = validate_command(payload)
        self.assertIsInstance(command, Command)
        self.assertEqual(command.action, "run_python")
        self.assertNotIn("element_id", command.__dict__)
        with self.assertRaises(ValueError):
            validate_command(dict(payload, action="click"))

    def test_file_and_finish_fields_are_action_scoped(self):
        write = {"action": "write_file", "plan": "Save code", "reason": "reuse",
                 "path": "tools/probe.py", "content": "print(1)", "command": "", "code": ""}
        self.assertEqual(validate_command(write).path, "tools/probe.py")
        with self.assertRaises(ValueError):
            validate_command(dict(write, command="echo wrong"))


class HarnessTests(unittest.TestCase):
    def test_logs_actual_model_transport_and_separates_thinking(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            sent = []
            def transport(payload):
                sent.append(payload)
                yield {"message": {"thinking": "private"}}
                yield {"message": {"content": '{"action":"finish","plan":"done","reason":"enough","path":"","content":"","command":"","code":""}'}}
                yield {"done": True, "done_reason": "stop", "eval_count": 20, "prompt_eval_count": 100, "total_duration": 123}
            harness = Harness(log, transport=transport)
            command = harness.decide(RUN_ID, {"goal": "inspect"}, model="test:model", settings={"think": True, "options": {"num_predict": 700}})
            self.assertEqual(command.action, "finish")
            self.assertEqual(sent[0]["think"], True)
            self.assertEqual(harness.calls, 1)
            events = log.read_events()
            self.assertTrue(any(event["kind"] == "model_delta" and event.get("channel") == "thinking" for event in events))
            complete = next(event for event in events if event["kind"] == "model_response")
            self.assertEqual(complete["done_reason"], "stop")
            self.assertEqual(complete["output"], '{"action":"finish","plan":"done","reason":"enough","path":"","content":"","command":"","code":""}')
            self.assertEqual(complete["eval_count"], 20)
            log.close()

    def test_output_limit_is_model_failure_with_raw_response_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            def transport(_payload):
                yield {"message": {"content": "{bad"}}
                yield {"done": True, "done_reason": "length", "eval_count": 700}
            harness = Harness(log, transport=transport, max_calls=1)
            with self.assertRaises(ModelFailure) as raised:
                harness.decide(RUN_ID, {"goal": "inspect"}, model="test:model", settings={"think": False, "options": {"num_predict": 700}})
            self.assertEqual(raised.exception.kind, "format")
            events = log.read_events()
            self.assertTrue(any(event["kind"] == "model_response" and event["done_reason"] == "length" for event in events))
            log.close()


if __name__ == "__main__":
    unittest.main()

import json
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
        finish = dict(write, action="finish", path="", content="", plan="Finish observation", reason="Title observed")
        self.assertEqual(validate_command(finish).action, "finish")
        for field in ("plan", "reason"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_command(dict(finish, **{field: ""}))


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
            self.assertEqual(raised.exception.kind, "output_limit")
            events = log.read_events()
            response = next(event for event in events if event["kind"] == "model_response")
            self.assertEqual(response["output"], "{bad")
            self.assertEqual(response["done_reason"], "length")
            self.assertFalse(any(event["kind"] == "command_decoded" for event in events))
            log.close()

    def test_identical_unused_payloads_are_normalized_and_logged(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            answer = {"action": "run_python", "plan": "Read title", "reason": "Observe current page",
                      "path": "", "content": " \n\t", "command": "print(await page.title())",
                      "code": "print(await page.title())"}
            raw = json.dumps(answer)
            def transport(_payload):
                yield {"message": {"content": raw}, "done": True, "done_reason": "stop"}
            command = Harness(log, transport=transport).decide(
                RUN_ID, {}, model="test:model", settings={})
            self.assertEqual(command.code, answer["code"])
            self.assertEqual(command.command, "")
            self.assertEqual(command.content, "")
            events = log.read_events()
            normalized = next(event for event in events if event["kind"] == "command_normalized")
            self.assertEqual(normalized["fields"], ["content", "command"])
            self.assertEqual(normalized["call_id"], command.call_id)
            self.assertEqual(next(event for event in events if event["kind"] == "model_response")["output"], raw)
            # The pure contract remains strict outside the decoding boundary.
            with self.assertRaises(ValueError):
                validate_command(answer)
            log.close()

    def test_conflicting_or_merely_similar_unused_payloads_still_fail(self):
        for extra in ("print('different')", "print(1) "):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                log = SessionLog(Path(temp), RUN_ID, {})
                answer = {"action": "run_python", "plan": "Inspect", "reason": "Observe",
                          "path": "", "content": "", "command": extra, "code": "print(1)"}
                def transport(_payload):
                    yield {"message": {"content": json.dumps(answer)}, "done": True, "done_reason": "stop"}
                with self.assertRaises(ModelFailure) as raised:
                    Harness(log, transport=transport, max_calls=1).decide(
                        RUN_ID, {}, model="test:model", settings={})
                self.assertEqual(raised.exception.kind, "format")
                self.assertIn("command", str(raised.exception))
                self.assertFalse(any(event["kind"] == "command_decoded" for event in log.read_events()))
                log.close()

    def test_output_limit_retries_with_previous_response_and_same_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            sent = []
            # Even syntactically valid JSON is not accepted after a length stop.
            cut = json.dumps({"action": "run_python", "plan": "Inspect", "reason": "Observe",
                              "path": "", "content": "", "command": "", "code": "print('cut')"})
            repaired = json.dumps({"action": "finish", "plan": "Done", "reason": "Enough evidence",
                                   "path": "", "content": "", "command": "", "code": ""})
            def transport(payload):
                sent.append(payload)
                first = len(sent) == 1
                yield {"message": {"content": cut if first else repaired}, "done": True,
                       "done_reason": "length" if first else "stop", "eval_count": 700}
            command = Harness(log, transport=transport).decide(
                RUN_ID, {}, model="test:model", settings={"think": False, "options": {"num_predict": 700}})
            self.assertEqual(command.action, "finish")
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0]["options"], sent[1]["options"])
            self.assertEqual(sent[1]["options"]["num_predict"], 700)
            self.assertEqual(sent[1]["messages"][-2], {"role": "assistant", "content": cut})
            self.assertIn("output limit", sent[1]["messages"][-1]["content"].lower())
            events = log.read_events()
            self.assertEqual([e["error_kind"] for e in events if e["kind"] == "model_error"], ["output_limit"])
            self.assertEqual([e["call_id"] for e in events if e["kind"] == "command_decoded"], ["CALL-0002"])
            self.assertEqual([e["output"] for e in events if e["kind"] == "model_response"], [cut, repaired])
            log.close()


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import Harness, ModelFailure, repeating_explanation
from model_providers import OpenRouterError
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
    def test_python_await_is_not_accepted_as_a_shell_command(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            self.addCleanup(log.close)
            sent = []
            bad = {"action": "run_command", "plan": "Observe", "reason": "Fetch page",
                   "path": "", "content": "", "command": "await http_get('https://fixture.invalid/')", "code": ""}
            good = {**bad, "action": "run_python", "command": "", "code": bad["command"]}
            def transport(payload):
                sent.append(payload)
                yield {"message": {"content": json.dumps(bad if len(sent) == 1 else good)}, "done": True}
            result = Harness(log, transport=transport).decide(RUN_ID, {}, model="fixture", settings={})
            self.assertEqual(result.action, "run_python")
            self.assertEqual(len(sent), 2)

    def test_slow_internal_repairs_leave_time_for_the_reporting_call(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            self.addCleanup(log.close)
            clock, sent = [100.0], []
            finish = {"action": "finish", "plan": "Report", "reason": "No target observations",
                      "path": "", "content": "", "command": "", "code": ""}
            def transport(payload):
                sent.append(payload)
                reporting = len(sent) > 1 and '"finish_soon": true' in payload["messages"][1]["content"]
                clock[0] += 10 if reporting else 80
                yield {"message": {"content": json.dumps(finish) if reporting else "{"}, "done": True}
            harness = Harness(log, transport=transport, max_calls=40, report_reserve=1)
            with patch("harness.time.monotonic", side_effect=lambda: clock[0]):
                with self.assertRaises(ModelFailure) as caught:
                    harness.decide(RUN_ID, {"remaining": {"finish_soon": False}}, model="fixture", settings={}, deadline=240)
                self.assertEqual(caught.exception.kind, "budget")
                result = harness.decide(RUN_ID, {"remaining": {"finish_soon": True}}, model="fixture", settings={}, deadline=240)
            self.assertEqual(result.action, "finish")
            self.assertEqual(len(sent), 2)
            self.assertLess(clock[0], 240)

    def test_j1_wrong_runtime_actions_are_repaired_before_command_acceptance(self):
        for invalid in (
                {"action": "read_file", "path": "https://fixture.invalid/", "code": ""},
                {"action": "read_file", "path": "/runtime/index.html", "code": ""},
                {"action": "run_python", "path": "", "code": "asyncio.get_event_loop().run_until_complete(task())"}):
            with self.subTest(action=invalid["action"]), tempfile.TemporaryDirectory() as temp:
                log = SessionLog(Path(temp), RUN_ID, {})
                self.addCleanup(log.close)
                sent = []
                bad = {"plan": "Observe", "reason": "Inspect target", "content": "", "command": "", **invalid}
                good = {"action": "run_python", "plan": "Observe", "reason": "Use supplied runtime",
                        "path": "", "content": "", "command": "", "code": "print(await page.title())"}
                def transport(payload):
                    sent.append(payload)
                    yield {"message": {"content": json.dumps(bad if len(sent) == 1 else good)}, "done": True}
                command = Harness(log, transport=transport, system_prompt_version="J1").decide(RUN_ID, {}, model="fixture", settings={})
                self.assertEqual(command.code, good["code"])
                self.assertEqual(len(sent), 2)
                self.assertEqual(sum(e["kind"] == "command_decoded" for e in log.read_events()), 1)

    def test_j2_truncated_code_still_grows_budget_when_a_completed_reason_contains_repeated_test_data(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            self.addCleanup(log.close)
            sent = []
            cut = json.dumps({"action": "run_python", "plan": "Inspect length handling", "reason": "Use a bounded test value: " + "A" * 160,
                              "path": "", "content": "", "command": "", "code": "print('fixture')\n" * 500})[:-20]
            good = json.dumps({"action": "finish", "plan": "Report", "reason": "No verified effect",
                               "path": "", "content": "", "command": "", "code": ""})
            def transport(payload):
                sent.append(payload)
                first = len(sent) == 1
                yield {"message": {"content": cut if first else good}, "done": True, "done_reason": "length" if first else "stop"}
            Harness(log, transport=transport, system_prompt_version="J2").decide(
                RUN_ID, {}, model="fixture", settings={"options": {"num_predict": 4096}})
            self.assertEqual([item["options"]["num_predict"] for item in sent], [4096, 8192])
            self.assertFalse(any(item["kind"] == "output_repetition_detected" for item in log.read_events()))

    def test_repeated_explanation_is_detected_before_a_different_truncated_tail(self):
        repeated = '{"reason":"' + ("Inspecting the same empty notes is described repeatedly without any new observation. " * 60)
        self.assertTrue(repeating_explanation(repeated + "A different incomplete final sentence"))
        code = json.dumps({"reason": "Read an actual source", "code": "print('same')\n" * 500})
        self.assertFalse(repeating_explanation(code))

    def test_j2_repeated_explanation_does_not_raise_the_output_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            self.addCleanup(log.close)
            sent = []
            repeated = '{"action":"read_file","plan":"Inspect notes","reason":"' + ("This is the beginning of the current investigation and the same rationale is repeated. " * 100)
            good = json.dumps({"action": "finish", "plan": "Report limits", "reason": "No target observations yet",
                               "path": "", "content": "", "command": "", "code": ""})
            def transport(payload):
                sent.append(payload)
                first = len(sent) == 1
                yield {"message": {"content": repeated if first else good}, "done": True,
                       "done_reason": "length" if first else "stop"}
            harness = Harness(log, transport=transport, system_prompt_version="J2")
            harness.decide(RUN_ID, {}, model="fixture", settings={"options": {"num_predict": 4096}})
            harness.decide(RUN_ID, {}, model="fixture", settings={"options": {"num_predict": 4096}})
            self.assertEqual([item["options"]["num_predict"] for item in sent], [4096, 4096, 4096])
            events = log.read_events()
            self.assertTrue(any(item["kind"] == "output_repetition_detected" for item in events))
            self.assertFalse(any(item["kind"] == "output_budget_raised" for item in events))

    def test_j2_ignoring_short_explanation_schema_is_repaired_without_limiting_the_report(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            self.addCleanup(log.close)
            sent = []
            long = {"action": "read_file", "plan": "Inspect notes", "reason": "x" * 801,
                    "path": "work/notes.json", "content": "", "command": "", "code": ""}
            report = {**long, "action": "finish", "path": "", "reason": "r" * 5000}
            def transport(payload):
                sent.append(payload)
                yield {"message": {"content": json.dumps(long if len(sent) == 1 else report)}, "done": True}
            result = Harness(log, transport=transport, system_prompt_version="J2").decide(RUN_ID, {}, model="fixture", settings={})
            self.assertEqual(len(sent), 2)
            self.assertEqual(result.action, "finish")
            self.assertEqual(result.reason, "r" * 5000)
            branches = sent[0]["format"]["oneOf"]
            read = next(branch for branch in branches if branch["properties"]["action"]["const"] == "read_file")
            finish = next(branch for branch in branches if branch["properties"]["action"]["const"] == "finish")
            self.assertEqual(read["properties"]["reason"]["maxLength"], 800)
            self.assertNotIn("maxLength", finish["properties"]["reason"])

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

    def test_final_report_in_content_is_preserved_without_an_extra_call(self):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder), RUN_ID, {})
            self.addCleanup(log.close)
            answer = {"action": "finish", "plan": "보고", "reason": "범위와 한계 요약",
                "path": "", "content": "## 최종 보고서\n일부 요청은 실행되지 않아 미평가 상태다.", "command": "", "code": ""}
            def transport(payload):
                schema = payload["format"]["properties"]
                self.assertEqual(schema["action"]["const"], "finish")
                self.assertTrue(all(schema[key]["const"] == "" for key in ("path", "content", "command", "code")))
                yield {"message": {"content": json.dumps(answer)}, "done": True}
            harness = Harness(log, transport=transport, max_calls=1, report_reserve=1)
            command = harness.decide(RUN_ID, {"remaining": {"finish_soon": True}}, model="fixture", settings={})
            self.assertEqual(command.action, "finish")
            self.assertIn(answer["content"], command.reason)
            self.assertIn(answer["reason"], command.reason)
            self.assertEqual(command.content, "")
            self.assertEqual(harness.calls, 1)

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

    def test_trailing_data_after_json_object_is_discarded(self):
        # Robustness: when a provider appends framing AFTER a complete JSON object, raw_decode keeps the
        # first object and drops the trailing noise. (This is not the GLM bug where framing is embedded
        # INSIDE the code string — that is handled provider-side in the OpenRouter adapter.)
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            command_json = json.dumps({"action": "run_python", "plan": "Inspect title", "reason": "Observe page",
                                       "path": "", "content": "", "command": "", "code": "print(await page.title())"})
            raw = command_json + "'}</arg_value></tool_call><tool_call>:{\t\t,\t\"action\"\t:\t\"run_python\"\t}"
            def transport(_payload):
                yield {"message": {"content": raw}, "done": True, "done_reason": "stop"}
            command = Harness(log, transport=transport, max_calls=1).decide(
                RUN_ID, {}, model="openrouter:~z-ai/glm-flash-latest", settings={})
            self.assertEqual(command.action, "run_python")
            self.assertEqual(command.code, "print(await page.title())")
            self.assertNotIn("</tool_call>", command.code)
            salvaged = next(event for event in log.read_events() if event["kind"] == "command_salvaged")
            self.assertGreater(salvaged["trailing_chars"], 0)
            self.assertEqual(salvaged["call_id"], command.call_id)
            log.close()

    def test_leading_non_json_prefix_still_fails_as_format(self):
        # Salvage only trims trailing noise after a leading JSON value; junk BEFORE the object is a
        # genuine format error and must not be silently searched past.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            raw = "```json\n" + json.dumps({"action": "finish", "plan": "d", "reason": "e",
                                            "path": "", "content": "", "command": "", "code": ""})
            def transport(_payload):
                yield {"message": {"content": raw}, "done": True, "done_reason": "stop"}
            with self.assertRaises(ModelFailure) as raised:
                Harness(log, transport=transport, max_calls=1).decide(
                    RUN_ID, {}, model="test:model", settings={})
            self.assertEqual(raised.exception.kind, "format")
            self.assertFalse(any(event["kind"] == "command_decoded" for event in log.read_events()))
            log.close()

    def test_openrouter_tool_call_framing_is_stripped_from_code(self):
        # The real GLM failure: a single valid JSON object whose code string has tool-call framing
        # embedded in it. It must be cut back to clean, compilable Python before execution.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            dirty = {"action": "run_python", "plan": "Inspect", "reason": "Observe",
                     "path": "", "content": "", "command": "",
                     "code": "print(await page.title())'}</arg_value></tool_call><tool_call>:{"}
            def transport(_payload):
                yield {"message": {"content": json.dumps(dirty)}, "done": True, "done_reason": "stop"}
            command = Harness(log, transport=transport, max_calls=1).decide(
                RUN_ID, {}, model="openrouter:~z-ai/glm-flash-latest", settings={})
            self.assertEqual(command.code, "print(await page.title())")
            self.assertNotIn("</tool_call>", command.code)
            compile(command.code, "<c>", "exec", flags=__import__("ast").PyCF_ALLOW_TOP_LEVEL_AWAIT)
            sanitized = next(e for e in log.read_events() if e["kind"] == "command_sanitized")
            self.assertIn("code", sanitized["fields"])
            log.close()

    def test_openrouter_injection_payload_angle_brackets_are_preserved(self):
        # An XSS payload carries <svg>/<script>, never a tool-call delimiter, so it must survive intact.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            payload_code = "await submit_input(0, \"<svg onload=alert(1)>\", \".mat-card\")"
            answer = {"action": "run_python", "plan": "XSS probe", "reason": "Test reflection",
                      "path": "", "content": "", "command": "", "code": payload_code}
            def transport(_payload):
                yield {"message": {"content": json.dumps(answer)}, "done": True, "done_reason": "stop"}
            command = Harness(log, transport=transport, max_calls=1).decide(
                RUN_ID, {}, model="openrouter:~z-ai/glm-flash-latest", settings={})
            self.assertEqual(command.code, payload_code)
            self.assertFalse(any(e["kind"] == "command_sanitized" for e in log.read_events()))
            log.close()

    def test_non_openrouter_content_is_not_sanitized(self):
        # Framing removal is provider-scoped: an Ollama command is left exactly as decoded.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            code = "x = 1  # </tool_call> literal kept"
            answer = {"action": "run_python", "plan": "p", "reason": "r",
                      "path": "", "content": "", "command": "", "code": code}
            def transport(_payload):
                yield {"message": {"content": json.dumps(answer)}, "done": True, "done_reason": "stop"}
            command = Harness(log, transport=transport, max_calls=1).decide(
                RUN_ID, {}, model="qwen3.5:9b", settings={})
            self.assertEqual(command.code, code)
            self.assertFalse(any(e["kind"] == "command_sanitized" for e in log.read_events()))
            log.close()

    def test_output_limit_raises_output_budget_and_retries(self):
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
            # A length truncation doubles num_predict for the retry instead of re-sending the same cap.
            self.assertEqual(sent[0]["options"]["num_predict"], 700)
            self.assertEqual(sent[1]["options"]["num_predict"], 1400)
            self.assertEqual({k: v for k, v in sent[1]["options"].items() if k != "num_predict"},
                             {k: v for k, v in sent[0]["options"].items() if k != "num_predict"})
            self.assertEqual(sent[1]["messages"][-2], {"role": "assistant", "content": cut})
            self.assertIn("output limit", sent[1]["messages"][-1]["content"].lower())
            events = log.read_events()
            self.assertEqual([e["error_kind"] for e in events if e["kind"] == "model_error"], ["output_limit"])
            raised = next(e for e in events if e["kind"] == "output_budget_raised")
            self.assertEqual((raised["from_num_predict"], raised["to_num_predict"]), (700, 1400))
            self.assertEqual([e["call_id"] for e in events if e["kind"] == "command_decoded"], ["CALL-0002"])
            self.assertEqual([e["output"] for e in events if e["kind"] == "model_response"], [cut, repaired])
            log.close()

    def test_raised_output_budget_persists_to_later_steps(self):
        # Once a length-truncation forces the budget up, later decide() calls in the same run start
        # at the raised floor so a reasoning model does not re-truncate at the original cap every step.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            sent = []
            good = json.dumps({"action": "finish", "plan": "Done", "reason": "Enough",
                               "path": "", "content": "", "command": "", "code": ""})
            def transport(payload):
                sent.append(payload)
                truncate = len(sent) == 1   # step 1 call 1 truncates; everything else completes
                yield {"message": {"content": "" if truncate else good}, "done": True,
                       "done_reason": "length" if truncate else "stop", "eval_count": 4096}
            harness = Harness(log, transport=transport)
            settings = {"think": None, "options": {"num_ctx": 262144, "num_predict": 4096}}
            harness.decide(RUN_ID, {}, model="openrouter:z-ai/glm-5.3-flash", settings=settings)
            harness.decide(RUN_ID, {}, model="openrouter:z-ai/glm-5.3-flash", settings=settings)
            self.assertEqual(sent[0]["options"]["num_predict"], 4096)   # initial cap
            self.assertEqual(sent[1]["options"]["num_predict"], 8192)   # doubled on the retry
            self.assertEqual(sent[2]["options"]["num_predict"], 8192)   # floor carried into the next step
            log.close()

    def test_transient_transport_error_retries_then_succeeds(self):
        # A provider that drops the stream once should be retried, not fail the whole run.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            attempts = []
            good = json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                               "path": "", "content": "", "command": "", "code": ""})
            def transport(_payload):
                attempts.append(1)
                if len(attempts) == 1:
                    raise OpenRouterError("OpenRouter 응답이 정상 종료되지 않았습니다")
                yield {"message": {"content": good}, "done": True, "done_reason": "stop"}
            command = Harness(log, transport=transport, transport_retry_backoff=0).decide(
                RUN_ID, {}, model="openrouter:z-ai/glm-5.3-flash", settings={})
            self.assertEqual(command.action, "finish")
            self.assertEqual(len(attempts), 2)
            events = log.read_events()
            self.assertEqual([e["error_kind"] for e in events if e["kind"] == "model_error"], ["transport"])
            self.assertTrue(any(e["kind"] == "transport_retry" for e in events))
            log.close()

    def test_non_retryable_transport_error_aborts_without_retry(self):
        # Auth/billing/permission failures (retryable=False) must fail fast, not burn retries.
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {})
            attempts = []
            def transport(_payload):
                attempts.append(1)
                raise OpenRouterError("OpenRouter HTTP 402: 크레딧 잔액을 확인하세요.", retryable=False)
                yield  # unreachable; makes this a generator
            with self.assertRaises(ModelFailure) as raised:
                Harness(log, transport=transport, transport_retry_backoff=0, max_calls=5).decide(
                    RUN_ID, {}, model="openrouter:z-ai/glm-5.3-flash", settings={})
            self.assertEqual(raised.exception.kind, "transport")
            self.assertEqual(len(attempts), 1)
            self.assertFalse(any(e["kind"] == "transport_retry" for e in log.read_events()))
            log.close()


if __name__ == "__main__":
    unittest.main()

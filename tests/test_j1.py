"""J1 process controls with fixed execution results; no site, shell, browser or model calls."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from command_runtime import ExecutionResult
from explorer import ExplorerV3, compose_report
from harness import Harness, ModelFailure
from model_providers import OpenRouterError
from j1_policy import J1Policy
from planning import Command, available_prompt_versions, load_prompt
from session_log import SessionLog


TARGET = "http://127.0.0.1:3000/"


def command(plan, code="print('observed')", action="run_python", refs=(1,)):
    reason = "observations and limits " + " ".join(f"[step:{step}]" for step in refs)
    return Command(action, plan, reason, code=code if action == "run_python" else "")


def reply(value):
    return {"message": {"content": json.dumps({**value.runtime_payload(), "plan": value.plan, "reason": value.reason})},
            "done": True, "done_reason": "stop"}


class J1PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = J1Policy(TARGET)

    def observed(self, cmd, step):
        result = ExecutionResult(f"EXEC-{step}", cmd.action, path=f"tools/EXEC-{step}.py", exit_code=0,
                                 stdout='{"status":200,"observation":"fixture"}', artifact_path=f"artifacts/code/{step}.py")
        self.policy.record(cmd, result, step)

    def test_observed_xss_surface_can_be_examined_before_sqli_and_idor(self):
        self.observed(command("RECON/discover: actual input and display context"), 1)
        for step, stage, refs in ((2, "baseline", (1,)), (3, "compare", (2,)), (4, "verify", (2, 3))):
            cmd = command(f"XSS/{stage}: inspect observed surface", refs=refs)
            self.policy.validate(cmd, False)
            self.observed(cmd, step)
        self.assertEqual(self.policy.coverage["XSS"]["state"], "reviewed_unverified")
        self.assertEqual(self.policy.coverage["SQLi"]["state"], "not_started")
        self.assertEqual(self.policy.coverage["IDOR"]["state"], "not_started")
        self.policy.validate(command("REPORT/report: report without unrelated tests", action="finish"), False)

    def test_secondary_candidate_is_allowed_with_observation_basis(self):
        self.observed(command("RECON/discover: observed upload form"), 1)
        self.policy.validate(command("OTHER/baseline: normal authorized request"), False)

    def test_no_observation_means_no_blind_baseline_for_any_check(self):
        for check in ("SQLi", "IDOR", "XSS", "OTHER"):
            with self.subTest(check=check), self.assertRaises(ValueError):
                self.policy.validate(command(f"{check}/baseline: guessed endpoint"), False)
        self.policy.validate(command("REPORT/report: no applicable candidate observed", action="finish"), False)

    def test_failed_empty_or_missing_observation_references_do_not_enable_testing(self):
        for step, result in ((1, ExecutionResult("failed", "run_python", exit_code=1, stdout="error")),
                             (2, ExecutionResult("empty", "run_python", exit_code=0, stdout=""))):
            self.policy.record(command("RECON/discover: inspect"), result, step)
        for refs in ((), (1,), (2,), (999,)):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.policy.validate(command("SQLi/baseline: no usable observation", refs=refs), False)

    def test_comparison_must_reference_this_candidates_baseline(self):
        self.observed(command("RECON/discover: observed search"), 1)
        self.observed(command("SQLi/baseline: normal", refs=(1,)), 2)
        with self.assertRaises(ValueError):
            self.policy.validate(command("SQLi/compare: unrelated basis", refs=(1,)), False)
        self.policy.validate(command("SQLi/compare: reuse baseline", refs=(2,)), False)

    def test_missing_baseline_cannot_be_skipped_and_report_cannot_execute_tools(self):
        with self.assertRaises(ValueError):
            self.policy.validate(command("SQLi/compare: compare"), False)
        with self.assertRaises(ValueError):
            self.policy.validate(command("SQLi/baseline: more work"), True)
        self.policy.validate(command("REPORT/report: final report", action="finish"), True)

    def test_successful_preparation_is_not_abandoned_after_three_steps(self):
        for step in range(4):
            self.observed(command("SQLi/discover: follow a new reference", code=f"print({step})"), step + 1)
        self.assertIn("SQLi", self.policy.context()["open_checks"])
        self.assertNotEqual(self.policy.context()["coverage"]["SQLi"]["state"], "blocked")

    def test_execution_failures_do_not_decide_applicability_or_force_another_check(self):
        cmd = command("SQLi/baseline: attempt")
        for step in range(3):
            self.policy.record(cmd, ExecutionResult(str(step), "run_python", exit_code=1, exception="NameError: missing"), step + 1)
        self.assertEqual(self.policy.context()["coverage"]["SQLi"]["state"], "in_progress")
        self.assertIn("Execution failed", self.policy.coverage["SQLi"]["limits"])
        self.assertEqual(self.policy.coverage["IDOR"]["state"], "not_started")

    def test_baseline_request_fields_survive_long_history_and_guard_against_drift(self):
        code = "files = {'popinquiryfile': ('fixture.txt', b'fixture')}\nr = requests.post('http://127.0.0.1:3000/upload', files=files)\nprint(r.status_code)"
        baseline = command("SQLi/baseline: normal request", code)
        self.observed(baseline, 1)
        for step in range(2, 40):
            self.observed(command("SQLi/discover: inspect related data", code=f"print({step})"), step)
        memory = self.policy.context()["request_memory"]
        self.assertEqual(memory[0]["form_fields"], ["popinquiryfile"])
        self.assertEqual(memory[0]["source_path"], "tools/EXEC-1.py")
        self.assertEqual(len(memory[0]["source_sha256"]), 64)
        with self.assertRaisesRegex(ValueError, "popinquiryfile"):
            self.policy.validate(command("SQLi/compare: changed input", code.replace("popinquiryfile", "file")), False)
        self.policy.validate(command("SQLi/compare: same request fields", code), False)

    def test_failed_baseline_does_not_replace_or_create_request_memory(self):
        cmd = command("SQLi/baseline: normal", "requests.get('http://127.0.0.1:3000/search')")
        self.policy.record(cmd, ExecutionResult("bad", "run_python", exit_code=1), 1)
        self.assertEqual(self.policy.context()["request_memory"], [])

    def test_interleaved_candidates_of_same_type_can_resume_their_own_baseline(self):
        self.observed(command("RECON/discover: two observed inputs"), 1)
        self.observed(command("SQLi/baseline: first search", refs=(1,)), 2)
        self.observed(command("SQLi/baseline: second search", refs=(1,)), 3)
        self.policy.validate(command("SQLi/compare: resume first search", refs=(2,)), False)
        self.observed(command("SQLi/compare: resume first search", refs=(2,)), 4)
        self.policy.validate(command("SQLi/verify: verify first search", refs=(2, 4)), False)
        with self.assertRaises(ValueError):
            self.policy.validate(command("SQLi/verify: mixed candidate references", refs=(3, 4)), False)

    def test_earlier_candidate_progress_survives_new_baseline_and_fallback_report(self):
        self.observed(command("RECON/discover: observed inputs"), 1)
        for step, stage, refs in ((2, "baseline", (1,)), (3, "compare", (2,)), (4, "verify", (2, 3))):
            self.observed(command(f"XSS/{stage}: first input", refs=refs), step)
        self.observed(command("XSS/baseline: second input", refs=(1,)), 5)
        candidates = list(self.policy.snapshot().get("candidates", {}).values())
        self.assertEqual([item["state"] for item in candidates], ["reviewed_unverified", "in_progress"])
        report = compose_report([], "error", "model unavailable", j1_state=self.policy.snapshot())
        self.assertIn("baseline=2", report)
        self.assertIn("baseline=5", report)

    def test_memory_retains_newest_baseline_when_full(self):
        for step in range(1, 10):
            self.observed(command("SQLi/baseline: normal", f"requests.get('{TARGET}search{step}')"), step)
        memory = self.policy.context()["request_memory"]
        self.assertEqual(len(memory), 8)
        self.assertIn("tools/EXEC-9.py", [item["source_path"] for item in memory])
        self.assertNotIn("tools/EXEC-1.py", [item["source_path"] for item in memory])

    def test_same_shape_new_baseline_keeps_its_fresh_request_code(self):
        for step, token in ((1, "old-csrf"), (2, "new-csrf")):
            self.observed(command("SQLi/baseline: fresh request",
                f"requests.post('{TARGET}search', data={{'q':'normal','csrf':{token!r}}})"), step)
        memory = self.policy.context()["request_memory"]
        self.assertEqual(memory[0]["source_path"], "tools/EXEC-2.py")

    def test_field_guard_uses_referenced_candidate_not_any_historical_schema(self):
        old = f"requests.post('{TARGET}search', data={{'old_field':'normal'}})"
        new = f"requests.post('{TARGET}search', data={{'current_field':'normal'}})"
        self.observed(command("SQLi/baseline: old schema", old), 1)
        self.observed(command("SQLi/baseline: new schema", new), 2)
        with self.assertRaisesRegex(ValueError, "current_field"):
            self.policy.validate(command("SQLi/compare: accidentally reused old schema", old, refs=(2,)), False)
        self.policy.validate(command("SQLi/compare: use current schema", new, refs=(2,)), False)

    def test_unknown_dynamic_fields_are_not_treated_as_an_empty_form(self):
        known = f"requests.post('{TARGET}upload', files={{'popinquiryfile':('note.txt', b'ok')}})"
        self.observed(command("OTHER/baseline: known form", known), 1)
        dynamic = f"requests.post('{TARGET}upload', files=load_observed_files())"
        self.policy.validate(command("OTHER/compare: runtime-resolved form", dynamic), False)

    def test_dynamic_keyword_arguments_do_not_create_a_false_empty_field_mismatch(self):
        self.observed(command("OTHER/baseline: normal form", f"requests.post('{TARGET}form', data={{'q':'normal'}})"), 1)
        self.policy.validate(command("OTHER/compare: runtime-resolved arguments", f"requests.post('{TARGET}form', **load_request_kwargs())"), False)

    def test_blocked_reason_updates_the_referenced_candidate(self):
        self.observed(command("IDOR/baseline: owner request"), 1)
        blocked = Command("run_python", "IDOR/blocked: no comparison identity", "[step:1] Missing approved non-owner account.", code="print('missing comparison identity')")
        self.observed(blocked, 2)
        candidate = self.policy.snapshot().get("candidates", {}).get(1, {})
        self.assertEqual(candidate.get("state"), "blocked")
        self.assertIn("non-owner", candidate.get("limits", ""))

    def test_generic_request_call_still_guards_known_field_drift(self):
        code = f"requests.request('POST', '{TARGET}upload', files={{'popinquiryfile':('note.txt', b'ok')}})"
        self.observed(command("OTHER/baseline: normal upload", code), 1)
        with self.assertRaisesRegex(ValueError, "popinquiryfile"):
            self.policy.validate(command("OTHER/compare: wrong field", code.replace("popinquiryfile", "file")), False)

    def test_dictionary_get_is_not_recorded_as_an_http_request(self):
        self.observed(command("OTHER/baseline: inspect local data", f"cached.get('{TARGET}upload')"), 1)
        self.assertEqual(self.policy.request_memory[0]["method"], "unknown")

    def test_client_alias_and_constant_fstring_keep_known_field_guard(self):
        for prefix, call in (("import httpx as hx\nclient = hx.AsyncClient()\n", "client.request('POST', "),
                             ("session = requests.Session()\n", "session.post(")):
            with self.subTest(call=call):
                self.policy = J1Policy(TARGET)
                code = prefix + call + "f'{target_origin}/upload', files={'popinquiryfile':('note.txt', b'ok')})"
                self.observed(command("OTHER/baseline: normal", code), 1)
                with self.assertRaisesRegex(ValueError, "popinquiryfile"):
                    self.policy.validate(command("OTHER/compare: changed field", code.replace("popinquiryfile", "file")), False)

    def test_formatted_url_conversion_is_not_guessed_as_a_known_request(self):
        self.observed(command("OTHER/baseline: inspect", "requests.get(f'{target_origin!r}/upload')"), 1)
        self.assertEqual(self.policy.request_memory[0]["method"], "unknown")

    def test_recon_can_collect_more_prerequisites_without_forcing_an_attack_type(self):
        self.observed(command("RECON/discover: inspect initial links"), 1)
        self.policy.validate(command("RECON/discover: inspect another relevant prerequisite"), False)
        self.policy.validate(command("SQLi/discover: read another prerequisite"), False)

    def test_equivalent_query_parameter_encoding_does_not_trigger_field_drift(self):
        self.observed(command("SQLi/baseline: normal", "requests.get('http://127.0.0.1:3000/search', params={'q':'normal'})"), 1)
        self.policy.validate(command("SQLi/compare: changed value", "requests.get('http://127.0.0.1:3000/search?q=test')"), False)

    def test_reported_failed_http_baseline_is_not_saved_as_working_request(self):
        cmd = command("SQLi/baseline: normal", "requests.get('http://127.0.0.1:3000/search')")
        self.policy.record(cmd, ExecutionResult("denied", "run_python", exit_code=0,
                           stdout='{"status":403}', path="tools/denied.py"), 1)
        self.assertEqual(self.policy.context()["request_memory"], [])
        self.assertNotIn("baseline", self.policy.context()["coverage"]["SQLi"]["stages"])

    def test_large_memory_keeps_source_references_without_filling_context(self):
        for step in range(8):
            url = TARGET + str(step) + "a" * 900
            fields = {f"field{i}" + "z" * 70: "value" for i in range(20)}
            code = f"requests.post({url!r}, data={fields!r})"
            self.observed(command("SQLi/baseline: normal request", code), step + 1)
        cards = self.policy.context()["request_memory"]
        self.assertEqual(len(cards), 8)
        self.assertLessEqual(len(json.dumps(cards, ensure_ascii=False)), 2800)
        self.assertTrue(all(card.get("source_path") and card.get("source_sha256") for card in cards))

    def test_new_baseline_cannot_reuse_an_old_comparison_as_validation(self):
        for step, stage in enumerate(("baseline", "compare", "verify"), 1):
            self.observed(command(f"SQLi/{stage}: first candidate"), step)
        self.observed(command("SQLi/baseline: new candidate", "print('new normal result')"), 4)
        self.assertEqual(self.policy.context()["coverage"]["SQLi"]["stages"], ["baseline"])
        with self.assertRaises(ValueError):
            self.policy.validate(command("SQLi/verify: missing new comparison"), False)

    def test_failed_new_baseline_cannot_reuse_old_stage_progress(self):
        for step, stage in enumerate(("baseline", "compare", "verify"), 1):
            self.observed(command(f"SQLi/{stage}: first candidate"), step)
        self.policy.record(command("SQLi/baseline: new candidate"),
                           ExecutionResult("bad", "run_python", exit_code=1), 4)
        with self.assertRaises(ValueError):
            self.policy.validate(command("SQLi/compare: failed normal request", refs=(4,)), False)

    def test_repeated_comparison_invalidates_its_old_verification(self):
        for step, stage in enumerate(("baseline", "compare", "verify"), 1):
            self.observed(command(f"XSS/{stage}: candidate"), step)
        self.observed(command("XSS/compare: changed comparison"), 4)
        self.assertEqual(self.policy.coverage["XSS"]["stages"], ["baseline", "compare"])
        self.assertEqual(self.policy.coverage["XSS"]["state"], "in_progress")
        with self.assertRaises(ValueError):
            self.policy.validate(command("XSS/verify: stale comparison", refs=(1, 2)), False)
        self.policy.validate(command("XSS/verify: current comparison", refs=(1, 4)), False)

    def test_failed_recheck_cannot_keep_old_verified_stage(self):
        for step, stage in enumerate(("baseline", "compare", "verify"), 1):
            self.observed(command(f"XSS/{stage}: candidate"), step)
        self.policy.record(command("XSS/verify: recheck", refs=(1, 2)),
                           ExecutionResult("bad", "run_python", exit_code=1), 4)
        self.assertNotIn("verify", self.policy.coverage["XSS"]["stages"])

    def test_populated_control_packet_still_fits_default_context_with_long_goal(self):
        for step in range(1, 9):
            check = ("SQLi", "IDOR", "XSS", "OTHER")[(step - 1) % 4]
            code = f"requests.get({(TARGET + str(step) + 'a' * 900)!r}, params={{'q':'normal'}})"
            cmd = Command("run_python", f"{check}/baseline: normal", "basis " * 100 + "[step:1]", code=code)
            self.observed(cmd, step)
        context = {"goal": "검" * 2000, "target": TARGET, "j1": self.policy.context(),
                   "runtime_path": "/session/runtime", "browser_api": "available runtime helpers " * 30,
                   "request_templates": [{"id": f"REQ-{step:04d}", "url": TARGET + "x" * 130,
                       "method": "POST", "fields": [f"observed_field_{i}" for i in range(4)],
                       "step": f"EXEC-{step}"} for step in range(8)],
                   "history": [{"observed": "x" * 1000}], "recent": [{"stdout": "x" * 1000}]}
        from j1_policy import pack_j1_context
        prompt, _ = load_prompt("J1")
        budget = int(8192 * 3.3 * .65) - len(prompt)
        packed = pack_j1_context(context, budget)
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), budget)
        self.assertEqual(packed["goal"], context["goal"])
        self.assertEqual(packed["request_templates"], context["request_templates"])
        self.assertEqual([item["source_path"] for item in packed["j1"]["request_memory"]],
                         [item["source_path"] for item in context["j1"]["request_memory"]])
        self.assertEqual(context["j1"], self.policy.context())

    def test_large_context_and_repair_keep_control_memory_within_input_budget(self):
        prompt, digest = load_prompt("J1")
        char_budget = int(8192 * 3.3 * 0.65)
        context = {"goal": "검증", "j1": self.policy.context(),
                   "recent": [{"command": "x" * 30000, "stdout": "y" * 30000}],
                   "history": [{"observed": "z" * 1000} for _ in range(30)]}
        original = json.dumps(context)
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), "b" * 32, {})
            self.addCleanup(log.close)
            sent = []
            def transport(payload):
                sent.append(payload)
                yield ({"message": {"content": "{" + "x" * 20000}, "done": True} if len(sent) == 1
                       else reply(command("RECON/discover: observe")))
            harness = Harness(log, transport=transport, max_calls=3, report_reserve=1,
                              system_prompt=prompt, system_prompt_version="J1", system_prompt_sha256=digest,
                              command_guard=self.policy.validate)
            harness.decide(log.run_id, context, model="fixture", settings={"options": {"num_ctx": 8192}})
            self.assertEqual(len(sent), 2)
            for payload in sent:
                self.assertLessEqual(sum(len(m["content"]) for m in payload["messages"]), char_budget)
                self.assertEqual(json.loads(payload["messages"][1]["content"])["j1"], context["j1"])
            self.assertEqual(json.dumps(context), original)


class J1LoopTests(unittest.TestCase):
    def run_loop(self, answers, *, calls=5, tools=30, command_seconds=30, execution_results=None, prompt_version="J1"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = SessionLog(root, "a" * 32, {"target": TARGET, "goal": "검증 후 한계를 보고해줘", "prompt_version": prompt_version,
                "model": "fixture", "model_settings": {"think": False, "options": {"num_ctx": 8192}},
                "budgets": {"seconds": 1200, "model_calls": calls, "tool_calls": tools, "command_seconds": command_seconds}})
            sent, executed = [], []
            answers = iter(answers)
            def transport(payload):
                sent.append(json.loads(payload["messages"][1]["content"]))
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                yield answer
            def run(run_id, payload, **kwargs):
                executed.append({**payload, "timeout": kwargs["timeout"]})
                if execution_results is not None:
                    return next(execution_results)
                return ExecutionResult(f"EXEC-{len(executed)}", payload["action"], path=f"tools/EXEC-{len(executed)}.py",
                                       exit_code=0, stdout="fixture observation")
            runtime = SimpleNamespace(runtime_dir=root / "runtime", python="python", run=run)
            try:
                result = ExplorerV3(root, transport=transport, browser_enabled=False)._loop(log.run_id, log, runtime)
                return result, sent, executed, dict(log.metadata)
            finally:
                log.close()

    def test_prompt_is_selectable_and_immutable_old_versions_remain(self):
        self.assertIn("J1", available_prompt_versions())
        for version in ("v1", "v2", "J1"):
            self.assertTrue(load_prompt(version)[0].strip())

    def test_no_supported_candidate_can_finish_early_without_fabricating_attempts(self):
        result, sent, executed, state = self.run_loop([
            reply(command("RECON/discover: inspect static content")),
            reply(command("REPORT/report: no applicable candidate established", action="finish"))], calls=10)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(executed), 1)
        self.assertFalse(sent[-1]["remaining"]["finish_soon"])
        self.assertTrue(all(row["state"] == "not_started" for row in state["j1_state"]["coverage"].values()))

    def test_failed_final_report_keeps_missing_account_reason_without_trying_idor(self):
        blocked = Command("run_python", "IDOR/blocked: missing prerequisites", "No approved owner/non-owner test accounts.",
                          code="print('No approved owner/non-owner test accounts.')")
        invalid = {"message": {"content": "{"}, "done": True, "done_reason": "stop"}
        result, _, executed, state = self.run_loop([reply(blocked), invalid], calls=2)
        report = compose_report(state.get("findings", []), result[0], result[1], j1_state=state["j1_state"])
        self.assertEqual(len(executed), 1)
        self.assertNotEqual(result[0], "completed")
        self.assertIn("No approved owner/non-owner test accounts", report)
        self.assertEqual(state["j1_state"]["coverage"]["SQLi"]["state"], "not_started")

    def test_reserved_report_rejects_exploration_even_with_time_left(self):
        answers = [reply(command("RECON/discover: inspect")), reply(command("XSS/baseline: normal")),
                   reply(command("XSS/compare: compare", refs=(2,)))]
        answers += [reply(command("OTHER/discover: ignore report instruction")), reply(command("REPORT/report: report", action="finish"))]
        result, sent, executed, state = self.run_loop(answers, calls=4)
        self.assertEqual(result[0], "error")
        self.assertEqual(len(executed), 3)
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent[-1]["remaining"]["model_calls"], 1)
        self.assertTrue(sent[-1]["remaining"]["finish_soon"])
        self.assertIn("j1_state", state)

    def test_only_final_call_is_reserved_for_a_successful_report(self):
        answers = [reply(command("RECON/discover: inspect")), reply(command("XSS/baseline: normal")),
                   reply(command("XSS/compare: compare", refs=(2,))),
                   reply(command("XSS/verify: recheck", refs=(2, 3))),
                   reply(command("REPORT/report: report", action="finish"))]
        result, sent, executed, state = self.run_loop(answers)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(executed), 4)
        self.assertFalse(sent[3]["remaining"]["finish_soon"])
        self.assertTrue(sent[4]["remaining"]["finish_soon"])
        self.assertGreater(sent[4]["remaining"]["seconds"], 1000)
        self.assertEqual(state["j1_state"]["coverage"]["SQLi"]["state"], "not_started")
        self.assertIn("j1_state", state)

    def test_custom_call_limits_reserve_the_last_call_for_every_prompt(self):
        for version in ("v1", "v2", "J1"):
            for limit in (1, 40, 50, 60):
                with self.subTest(version=version, limit=limit):
                    answers = [reply(command("RECON/discover: observe"))] * (limit - 1)
                    answers.append(reply(command("REPORT/report: actual observations", action="finish")))
                    result, sent, executed, _ = self.run_loop(
                        answers, calls=limit, tools=limit + 1, prompt_version=version)
                    self.assertEqual(result[0], "completed")
                    self.assertEqual(len(sent), limit)
                    self.assertEqual(len(executed), limit - 1)
                    self.assertEqual(sent[-1]["remaining"]["model_calls"], 1)
                    self.assertTrue(sent[-1]["remaining"]["finish_soon"])
                    self.assertIn("Call finish now", sent[-1]["instruction"])

    def test_every_prompt_blocks_exploration_in_the_reserved_call(self):
        for version in ("v1", "v2", "J1"):
            with self.subTest(version=version):
                result, sent, executed, state = self.run_loop(
                    [reply(command("RECON/discover: observe"))] * 50,
                    calls=50, tools=60, prompt_version=version)
                self.assertEqual(result[0], "error")
                self.assertEqual(len(sent), 50)
                self.assertEqual(len(executed), 49)
                self.assertTrue(sent[-1]["remaining"]["finish_soon"])
                self.assertIn("RECON/discover", compose_report(
                    state["findings"], result[0], result[1], j1_state=state.get("j1_state")))

    def test_custom_limit_format_repairs_cannot_consume_any_prompts_report_call(self):
        invalid = {"message": {"content": "{"}, "done": True, "done_reason": "stop"}
        for version in ("v1", "v2", "J1"):
            with self.subTest(version=version):
                answers = [reply(command("RECON/discover: observe"))] * 57
                answers += [invalid, invalid, reply(command("REPORT/report: observations", action="finish"))]
                result, sent, executed, _ = self.run_loop(answers, calls=60, tools=80, prompt_version=version)
                self.assertEqual(result[0], "completed")
                self.assertEqual(len(sent), 60)
                self.assertEqual(len(executed), 57)
                self.assertTrue(sent[-1]["remaining"]["finish_soon"])

    def test_format_retries_cannot_consume_report_reserve(self):
        invalid = {"message": {"content": "{"}, "done": True, "done_reason": "stop"}
        result, sent, executed, _ = self.run_loop([invalid, invalid, reply(command("REPORT/report: no verified results", action="finish"))], calls=3)
        self.assertEqual(result[0], "completed")
        self.assertEqual(executed, [])
        self.assertTrue(sent[2]["remaining"]["finish_soon"])

    def test_report_failure_does_not_add_untested_category_sections(self):
        invalid = {"message": {"content": "{"}, "done": True, "done_reason": "stop"}
        result, _, _, state = self.run_loop([invalid], calls=1)
        report = compose_report(state.get("findings", []), result[0], result[1], j1_state=state["j1_state"])
        for check in ("SQLi", "IDOR", "XSS"):
            self.assertNotIn(check, report)
        self.assertIn("관찰 없음", report)
        self.assertNotEqual(result[0], "completed")

    def test_report_keeps_observed_other_check_without_untouched_categories(self):
        result, _, _, state = self.run_loop([
            reply(command("OTHER/discover: inspect observed configuration")),
            reply(command("REPORT/report: observations", action="finish"))])
        report = compose_report(state["findings"], result[0], result[1], j1_state=state["j1_state"])
        self.assertIn("OTHER", report)
        for check in ("SQLi", "IDOR", "XSS"):
            self.assertNotIn(check, report)

    def test_last_allowed_tool_is_usable_and_report_needs_no_tool_slot(self):
        for version in ("v1", "v2", "J1"):
            with self.subTest(version=version):
                result, sent, executed, _ = self.run_loop(
                    [reply(command("RECON/discover: normal")), reply(command("REPORT/report: partial report", action="finish"))],
                    tools=1, prompt_version=version)
                self.assertEqual(len(executed), 1)
                self.assertEqual(result[0], "completed")
                self.assertTrue(sent[-1]["remaining"]["finish_soon"])

    def test_retryable_transport_failure_cannot_strand_reserved_report_call(self):
        with patch("harness.time.sleep"):
            result, sent, executed, _ = self.run_loop([OSError("temporary disconnect"), OSError("temporary disconnect"),
                reply(command("REPORT/report: partial observations", action="finish"))], calls=3)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(sent), 3)
        self.assertTrue(sent[-1]["remaining"]["finish_soon"])
        self.assertEqual(executed, [])

    def test_auth_or_billing_failures_are_not_retried_for_a_model_report(self):
        with self.assertRaises(ModelFailure) as error:
            self.run_loop([OpenRouterError("insufficient credits", retryable=False)], calls=3)
        self.assertEqual(error.exception.kind, "transport")

    def test_cancelled_execution_output_is_not_recorded_as_successful_observation(self):
        cancelled = ExecutionResult("cancelled", "run_python", exit_code=0, cancelled=True, stdout="partial output")
        result, _, _, state = self.run_loop([reply(command("RECON/discover: observe")),
            reply(command("REPORT/report: cancelled observation", action="finish"))],
            execution_results=iter([cancelled]))
        self.assertEqual(result[0], "completed")
        self.assertFalse(state["findings"][0]["evidence"])
        self.assertEqual(state["findings"][0]["status"], "error")
        self.assertTrue(state["findings"][0]["partial_observation"])
        self.assertIn("partial output", state["findings"][0]["observed"])

    def test_error_and_timeout_keep_partial_observations_without_marking_success(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                partial = ExecutionResult("partial", "run_python", exit_code=None if timed_out else 1,
                    timed_out=timed_out, stdout="HTTP status=200; observed header: max-age=300", exception="unfinished")
                result, sent, _, state = self.run_loop([
                    reply(command("RECON/discover: observe headers")),
                    reply(command("REPORT/report: partial observations", action="finish"))],
                    execution_results=iter([partial]))
                finding = state["findings"][0]
                self.assertNotEqual(finding["status"], "ok")
                self.assertIn("max-age=300", finding["observed"])
                self.assertTrue(finding["partial_observation"])
                self.assertFalse(finding["evidence"])
                self.assertIn("max-age=300", sent[-1]["history"][0]["observed"])
                report = compose_report(state["findings"], result[0], result[1], j1_state=state["j1_state"])
                self.assertIn("부분 출력", report)
                self.assertIn("max-age=300", report)

    def test_guard_block_is_visible_as_a_harness_limit_not_a_site_error(self):
        blocked = [{"url": "https://outside.invalid/", "method": "GET", "reason": "outside_target_origin"}]
        execution = ExecutionResult("blocked", "run_python", exit_code=0, blocked_requests=blocked)
        result, sent, _, state = self.run_loop([
            reply(command("RECON/discover: observe")),
            reply(command("REPORT/report: report limitation", action="finish"))], execution_results=iter([execution]))
        self.assertEqual(sent[-1]["history"][0]["blocked_requests"], blocked)
        report = compose_report(state["findings"], result[0], result[1], j1_state=state["j1_state"])
        self.assertIn("하네스 요청 차단", report)
        self.assertIn("outside_target_origin", report)

    def test_request_templates_remain_available_after_an_unrelated_code_failure(self):
        cards = [{"id": "REQ-0001", "url": TARGET + "form", "method": "POST", "fields": ["q"], "step": "EXEC-1"}]
        executions = iter([
            ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout="form submitted", request_templates=cards),
            ExecutionResult("EXEC-2", "run_python", exit_code=1, exception="NameError")])
        _, sent, _, state = self.run_loop([
            reply(command("RECON/discover: observe form")), reply(command("RECON/discover: read related resource")),
            reply(command("REPORT/report: report", action="finish"))], execution_results=executions)
        self.assertEqual(sent[-1]["request_templates"], cards)
        self.assertEqual(state["findings"][0]["request_refs"], ["REQ-0001"])
        self.assertNotIn("request_templates", sent[-1]["recent"][0]["result"])

    def test_empty_template_list_from_a_new_worker_clears_expired_context(self):
        cards = [{"id": "REQ-old-0001", "url": TARGET + "form", "method": "POST", "fields": ["q"], "step": "EXEC-1"}]
        executions = iter([
            ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout="form submitted", request_templates=cards),
            ExecutionResult("EXEC-2", "run_python", exit_code=0, stdout="worker restarted", request_templates=[])])
        _, sent, _, _ = self.run_loop([
            reply(command("RECON/discover: observe form")), reply(command("RECON/discover: observe after restart")),
            reply(command("REPORT/report: report", action="finish"))], execution_results=executions)
        self.assertFalse(sent[-1].get("request_templates"))

    def test_command_timeout_cannot_exceed_remaining_run_budget(self):
        result, _, executed, _ = self.run_loop([reply(command("RECON/discover: observe")),
            reply(command("REPORT/report: report", action="finish"))], command_seconds=1800)
        self.assertEqual(result[0], "completed")
        self.assertGreater(executed[0]["timeout"], 0)
        self.assertLessEqual(executed[0]["timeout"], 1200)

    def test_intentional_field_removal_runs_without_model_repair(self):
        baseline = f"requests.post('{TARGET}form', data={{'q':'normal','optional':'value'}})"
        changed = f"requests.post('{TARGET}form', data={{'q':'test'}})"
        result, sent, executed, _ = self.run_loop([
            reply(command("RECON/discover: observed form")),
            reply(command("OTHER/baseline: normal", baseline)),
            reply(command("OTHER/compare: intentionally omit optional field", changed, refs=(2,))),
            reply(command("REPORT/report: report", action="finish"))], calls=5)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(sent), 4)
        self.assertEqual(executed[-1]["code"], changed)
        self.assertTrue(sent[-1]["j1"]["advisories"])

    def test_changed_endpoint_or_method_runs_but_does_not_advance_candidate(self):
        baseline = f"requests.post('{TARGET}form', data={{'q':'normal'}})"
        for changed in (f"requests.post('{TARGET}other', data={{'q':'test'}})",
                        f"requests.get('{TARGET}form', params={{'q':'test'}})"):
            with self.subTest(changed=changed):
                result, sent, executed, state = self.run_loop([
                    reply(command("RECON/discover: observed form")),
                    reply(command("OTHER/baseline: normal", baseline)),
                    reply(command("OTHER/compare: changed request", changed, refs=(2,))),
                    reply(command("REPORT/report: report", action="finish"))], calls=5)
                self.assertEqual(result[0], "completed")
                self.assertEqual(executed[-1]["code"], changed)
                self.assertEqual(state["j1_state"]["candidates"][2]["stages"], ["baseline"])
                self.assertIn(3, state["j1_state"]["unlinked_steps"])
                self.assertTrue(sent[-1]["j1"]["advisories"])

    def test_direct_probe_without_stage_metadata_executes_but_remains_unlinked(self):
        result, sent, executed, state = self.run_loop([
            reply(command("XSS/verify: combined observation and check", refs=())),
            reply(command("REPORT/report: unlinked observation", action="finish"))], calls=3)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(executed), 1)
        self.assertEqual(len(state["j1_state"]["unlinked_steps"]), 1)
        self.assertNotEqual(state["j1_state"]["coverage"]["XSS"]["state"], "reviewed_unverified")

    def test_missing_progress_prefix_does_not_block_a_valid_command_or_finish(self):
        result, sent, executed, state = self.run_loop([
            reply(command("Inspect the observed endpoint")),
            reply(command("Final observations", action="finish"))], calls=3)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(executed), 1)
        self.assertTrue(state["j1_state"]["unlinked_steps"])
        self.assertIn("추적 정보가 부족한 실행 단계: [1]", compose_report(
            state["findings"], result[0], result[1], j1_state=state["j1_state"]))

    def test_unlinked_discovery_output_can_support_a_later_labelled_baseline(self):
        result, _, executed, state = self.run_loop([
            reply(command("Inspect the observed form")),
            reply(command("OTHER/baseline: normal input", refs=(1,))),
            reply(command("REPORT/report: report", action="finish"))], calls=4)
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(executed), 2)
        self.assertEqual(state["j1_state"]["candidates"][2]["stages"], ["baseline"])

"""J2 bookkeeping and loop checks without browsers, targets or live models."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from command_runtime import CommandRuntime, ExecutionResult
from explorer import ExplorerV3, compose_report
from harness import Harness
from j2_policy import CandidateNotebook, J2Policy, bounded_discovery, pack_j2_context
from planning import Command, load_prompt
from session_log import SessionLog


class NotebookTests(unittest.TestCase):
    def test_notes_are_optional_and_invalid_notes_do_not_interrupt_code(self):
        notebook = CandidateNotebook()
        self.assertFalse(notebook.note_candidate("form", state="confirmed")["recorded"])
        self.assertFalse(notebook.note_candidate("form", evidence_paths=["../secret"])["recorded"])
        self.assertEqual(notebook.notes, [])
        self.assertTrue(notebook.note_candidate("export", basis="observed link")["recorded"])

    def test_separate_notebooks_do_not_reuse_previous_steps_notes(self):
        first = CandidateNotebook()
        first.note_candidate("download", next_observation="check owner identity")
        self.assertEqual(CandidateNotebook().notes, [])

    def test_invalid_paths_never_raise_or_get_recorded_as_evidence(self):
        notebook = CandidateNotebook()
        for path in (".", "work", "/work/a", "tools/../private", "C:/private", "work/a\x7f"):
            with self.subTest(path=path):
                self.assertFalse(notebook.note_candidate("download", evidence_paths=[path])["recorded"])

    def test_note_size_and_count_are_bounded_without_raising(self):
        notebook = CandidateNotebook()
        self.assertFalse(notebook.note_candidate("x" * 500)["recorded"])
        for i in range(100):
            notebook.note_candidate(str(i))
        self.assertLessEqual(len(notebook.notes), 16)


class PolicyTests(unittest.TestCase):
    def test_discovery_references_survive_later_steps_without_stdout_parsing(self):
        policy = J2Policy()
        summary = {"source_url": "https://example.test/", "assets": [{"url": "https://example.test/exact-hash.js", "kind": "script"}],
                   "surfaces": [], "hints": [{"kind": "navigation", "url": "https://example.test/sign-in"}], "truncated": False}
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, discovery_summaries=[summary]), 1)
        for i in range(2, 7):
            policy.record(ExecutionResult(f"E{i}", "run_python", exit_code=0, stdout="unrelated"), i)
        discovery = policy.context()["discovery"]
        self.assertEqual(discovery["assets"][0]["url"], "https://example.test/exact-hash.js")
        self.assertEqual(discovery["hints"][0]["url"], "https://example.test/sign-in")
        self.assertEqual(discovery["verification"], "provided_text_declarations_only")
        self.assertEqual(len(policy.snapshot()["discovery_summaries"]), 1)

    def test_narrow_rescan_keeps_previous_declarations_with_their_original_steps(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, discovery_summaries=[{
            "source_url": "https://example.test/", "assets": [{"url": "https://example.test/exact-hash.js", "kind": "script"}]}]), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=0, discovery_summaries=[{
            "source_url": "https://example.test/", "hints": [{"kind": "navigation", "url": "https://example.test/sign-in"}]}]), 2)
        discovery = policy.context()["discovery"]
        self.assertEqual(discovery["assets"][0]["url"], "https://example.test/exact-hash.js")
        self.assertEqual(discovery["assets"][0]["step"], 1)
        self.assertEqual(discovery["hints"][0]["step"], 2)

    def test_caught_helper_error_is_visible_without_turning_the_step_into_a_crash(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, stdout="caught timeout",
                      helper_errors=[{"helper": "goto_and_render", "error_type": "TimeoutError", "url": "https://example.test/"}]), 1)
        record = policy.snapshot()["executions"][0]
        self.assertEqual(record["outcome"], "executed")
        self.assertEqual(record["helper_errors"][0]["error_type"], "TimeoutError")

    def test_discovery_capture_is_bounded_detached_and_rejects_malformed_sources(self):
        summary = {"source_url": "https://example.test/", "assets": [{"url": "https://example.test/" + str(i) + "x" * 1800, "kind": "script"} for i in range(30)],
                   "surfaces": [], "hints": [{"kind": "navigation", "url": "https://example.test/sign-in", "snippet": "private body"}]}
        captured = bounded_discovery(summary)
        self.assertLessEqual(len(json.dumps(captured, ensure_ascii=False)), 12_000)
        self.assertTrue(captured["truncated"])
        self.assertNotIn("private body", json.dumps(captured))
        summary["assets"].clear()
        self.assertTrue(captured["assets"])
        for bad in (None, {}, {"source_url": "https://user:secret@example.test/"}, {"source_url": "file:///private"}):
            self.assertIsNone(bounded_discovery(bad))

    def test_discovery_compaction_marks_omissions_and_preserves_exact_retained_urls(self):
        policy = J2Policy()
        assets = [{"url": f"https://example.test/{i}-exact-hash.js", "kind": "script"} for i in range(20)]
        policy.record(ExecutionResult("E1", "run_python", exit_code=0,
            discovery_summaries=[{"source_url": "https://example.test/", "assets": assets}]), 1)
        context = {"goal": "inspect", "remaining": {"finish_soon": False}, "j2": policy.context()}
        packed = pack_j2_context(context, 1800)
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), 1800)
        discovery = packed["j2"]["discovery"]
        self.assertTrue(discovery["truncated"])
        self.assertGreater(discovery["omitted_assets"], 0)
        self.assertTrue(all(item["url"] in {a["url"] for a in assets} for item in discovery["assets"]))
        self.assertEqual(len(context["j2"]["discovery"]["assets"]), 20)

    def test_source_retention_is_bounded_and_rescan_updates_latest_step(self):
        policy = J2Policy()
        for i in range(34):
            policy.record(ExecutionResult(str(i), "run_python", exit_code=0,
                discovery_summaries=[{"source_url": f"https://example.test/{i}", "assets": []}]), i)
        self.assertEqual(len(policy.snapshot()["discovery_summaries"]), 32)
        self.assertEqual(policy.context()["discovery"]["omitted_sources"], 2)
        policy.record(ExecutionResult("last", "run_python", exit_code=0,
            discovery_summaries=[{"source_url": "https://example.test/33", "assets": []}]), 35)
        self.assertEqual(len(policy.snapshot()["discovery_summaries"]), 32)
        self.assertEqual(policy.snapshot()["discovery_summaries"][-1]["step"], 35)

    def test_known_missing_import_is_rejected_but_guarded_fallback_is_allowed(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=1,
            exception_type="ModuleNotFoundError", exception="No module named 'requests'"), 1)
        self.assertTrue(policy.command_problems(Command("run_python", "inspect", "use library", code="import requests\nprint('later')")))
        for code in ("try:\n    import requests\nexcept ImportError:\n    print('fallback')",
                     "import json\nprint('new approach')", "print('prepare environment')\nimport requests"):
            self.assertEqual(policy.command_problems(Command("run_python", "inspect", "fallback", code=code)), [])
        policy.record(ExecutionResult("E2", "write_file", exit_code=0), 2)
        self.assertEqual(policy.command_problems(Command("run_python", "retry", "environment changed", code="import requests")), [])

    def test_plain_output_and_failed_execution_are_recorded_without_candidate_notes(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=0, stdout="normal output", path="tools/E1.py"), 1)
        policy.record(ExecutionResult("E2", "run_python", exit_code=1, stdout="partial", exception="failed"), 2)
        snapshot = policy.snapshot()
        self.assertEqual(snapshot["candidates"], [])
        self.assertEqual([item["outcome"] for item in snapshot["executions"]], ["executed", "failed"])
        self.assertEqual(snapshot["executions"][0]["source_path"], "tools/E1.py")
        self.assertTrue(snapshot["executions"][1]["partial"])
        self.assertEqual(snapshot["executions"][0]["verification"], "execution_only")

    def test_reading_a_python_file_does_not_invent_an_executed_source_path(self):
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "read_file", exit_code=0, path="/runtime/work/script.py", stdout="saved code"), 1,
                      Command("read_file", "read source", "inspect only", path="work/script.py"))
        record = policy.snapshot()["executions"][0]
        self.assertEqual(record["source_path"], "")
        self.assertEqual(record["file_path"], "work/script.py")

    def test_failed_step_retains_only_safe_partial_http_metadata(self):
        policy = J2Policy()
        response = {"url": "https://user:secret@example.test/cart?token=secret#private",
                    "method": "GET", "status": 200, "body": "private body"}
        policy.record(ExecutionResult("E1", "run_python", exit_code=1,
            http_observations=[response, {"url": "invalid", "status": "200"}]), 1)
        record = policy.snapshot()["executions"][0]
        self.assertTrue(record["partial"])
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["http_observations"], [{"url": "https://example.test/cart", "method": "GET", "status": 200}])

    def test_full_execution_metadata_can_be_compacted_without_losing_latest_source(self):
        policy = J2Policy()
        notebook = CandidateNotebook()
        notebook.note_candidate("download", basis="b" * 800, next_observation="n" * 800,
                                evidence_paths=["work/" + "x" * 240 + str(i) for i in range(4)])
        output = ExecutionResult("E1", "run_python", exit_code=0, path="tools/E1.py", stdout="observed",
            investigation_notes=notebook.notes,
            http_observations=[{"url": "https://example.test/" + "u" * 160 + str(i), "method": "GET", "status": 200} for i in range(16)])
        policy.record(output, 1)
        context = {"goal": "g" * 2000, "remaining": {"finish_soon": False}, "j2": policy.context()}
        packed = pack_j2_context(context, 4200)
        self.assertLessEqual(len(json.dumps(packed)), 4200)
        self.assertEqual(packed["goal"], context["goal"])
        self.assertEqual(packed["j2"]["executions"][0]["source_path"], "tools/E1.py")
        self.assertEqual(packed["j2"]["memory_path"], "work/j2-memory.json")
        with self.assertRaises(ValueError):
            pack_j2_context(context, 100)
    def execution(self, name="download", **changes):
        notebook = CandidateNotebook()
        notebook.note_candidate(name, basis="observed export button", next_observation="compare approved identities")
        return ExecutionResult("EXEC-1", "run_python", path="tools/EXEC-1.py", exit_code=0,
                               stdout="observation", investigation_notes=notebook.notes, **changes)

    def test_stdout_that_looks_like_a_note_does_not_create_candidates(self):
        policy = J2Policy()
        result = ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout='J2_NOTE {"name":"fake"}')
        policy.record(result, 1)
        self.assertEqual(policy.snapshot()["candidates"], [])

    def test_pending_work_has_step_source_and_no_attack_checklist(self):
        policy = J2Policy()
        policy.record(self.execution(), 1)
        memory = policy.context()
        self.assertNotIn("coverage", memory)
        self.assertEqual(memory["candidates"][0]["source_path"], "tools/EXEC-1.py")
        self.assertEqual(memory["candidates"][0]["next_observation"], "compare approved identities")
        self.assertEqual(policy.investigation_status(), "incomplete")

    def test_later_updates_keep_the_first_saved_source(self):
        policy = J2Policy()
        policy.record(self.execution(), 1)
        next_result = self.execution()
        next_result.path = "tools/EXEC-2.py"
        policy.record(next_result, 2)
        item = policy.context()["candidates"][0]
        self.assertEqual(item["source_path"], "tools/EXEC-2.py")
        self.assertEqual(item["first_source_path"], "tools/EXEC-1.py")

    def test_failed_execution_cannot_close_a_candidate(self):
        policy = J2Policy()
        policy.record(self.execution(), 1)
        notebook = CandidateNotebook()
        notebook.note_candidate("download", state="closed")
        failed = ExecutionResult("EXEC-2", "run_python", exit_code=1, stdout="partial output",
                                 investigation_notes=notebook.notes)
        policy.record(failed, 2)
        candidate = policy.snapshot()["candidates"][0]
        self.assertEqual(candidate["state"], "open")
        self.assertEqual(candidate["next_observation"], "compare approved identities")
        self.assertTrue(candidate["partial"])

    def test_successful_close_is_not_a_security_verdict(self):
        policy = J2Policy()
        policy.record(self.execution(), 1)
        notebook = CandidateNotebook()
        notebook.note_candidate("download", state="closed", basis="limited test finished")
        policy.record(ExecutionResult("EXEC-2", "run_python", exit_code=0, investigation_notes=notebook.notes), 2)
        self.assertEqual(policy.investigation_status(), "recorded_work_closed")
        self.assertNotIn("confirmed", policy.snapshot()["candidates"][0])

    def test_closing_with_pending_work_leaves_the_candidate_open(self):
        policy = J2Policy()
        notebook = CandidateNotebook()
        notebook.note_candidate("download", state="closed", next_observation="check controls")
        policy.record(ExecutionResult("EXEC-1", "run_python", exit_code=0, investigation_notes=notebook.notes), 1)
        self.assertEqual(policy.snapshot()["candidates"][0]["state"], "open")

    def test_invalid_worker_metadata_does_not_create_a_security_claim(self):
        policy = J2Policy()
        output = ExecutionResult("EXEC-1", "run_python", exit_code=0,
            investigation_notes=[{"name": "download", "state": "confirmed"}, {"state": "open"}, "page text"])
        policy.record(output, 1)
        self.assertEqual(policy.snapshot()["candidates"], [])
        self.assertTrue(policy.snapshot()["advisories"])

    def test_memory_capacity_cannot_hide_pending_work_as_completed(self):
        policy = J2Policy()
        for batch in range(4):
            notebook = CandidateNotebook()
            for i in range(16):
                notebook.note_candidate(f"closed-{batch}-{i}", state="closed")
            policy.record(ExecutionResult(str(batch), "run_python", exit_code=0, investigation_notes=notebook.notes), batch)
        policy.record(self.execution("pending export"), 5)
        self.assertEqual(policy.investigation_status(), "incomplete")
        self.assertEqual(policy.snapshot()["omitted_notes"], 1)
        self.assertEqual(len(policy.snapshot()["candidates"]), 64)

    def test_cancelled_execution_retains_prior_evidence_and_followup(self):
        policy = J2Policy()
        first = CandidateNotebook()
        first.note_candidate("download", next_observation="check owner", evidence_paths=["work/baseline.txt"])
        policy.record(ExecutionResult("EXEC-1", "run_python", path="tools/EXEC-1.py", exit_code=0,
                                      investigation_notes=first.notes), 1)
        second = CandidateNotebook()
        second.note_candidate("download", state="closed")
        policy.record(ExecutionResult("EXEC-2", "run_python", cancelled=True, investigation_notes=second.notes), 2)
        item = policy.snapshot()["candidates"][0]
        self.assertEqual(item["state"], "open")
        self.assertEqual(item["evidence_paths"], ["work/baseline.txt"])
        self.assertEqual(item["source_path"], "tools/EXEC-1.py")

    def test_context_compaction_keeps_latest_followup_and_memory_path(self):
        policy = J2Policy()
        for i in range(12):
            policy.record(self.execution(str(i)), i + 1)
        original = {"goal": "inspect", "remaining": {"finish_soon": False}, "j2": policy.context(),
                    "history": [{"observed": "x" * 3000} for _ in range(20)]}
        packed = pack_j2_context(original, 2200)
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), 2200)
        self.assertEqual(packed["j2"]["candidates"][0]["name"], "11")
        self.assertEqual(packed["j2"]["candidates"][0]["source_path"], "tools/EXEC-1.py")
        self.assertEqual(packed["j2"]["candidates"][0]["next_observation"], "compare approved identities")
        self.assertEqual(packed["j2"]["memory_path"], "work/j2-memory.json")
        self.assertEqual(packed["remaining"], original["remaining"])
        self.assertEqual(len(original["history"]), 20)


def answer(action="finish"):
    return {"message": {"content": json.dumps({"action": action, "plan": "inspect observed feature",
        "reason": "Actual results and limits", "path": "", "content": "", "command": "",
        "code": "print('observed')" if action == "run_python" else ""})}, "done": True}


class LoopTests(unittest.TestCase):
    def test_large_helper_metadata_does_not_consume_the_reporting_call(self):
        for field in ("helper_outcomes", "helper_errors"):
            with self.subTest(field=field):
                items = [{"helper": "goto_and_render", "url": "https://example.test/" + "x" * 210 + str(i),
                          **({"outcome": "completed"} if field == "helper_outcomes" else {"error_type": "TimeoutError"})} for i in range(16)]
                output = ExecutionResult("E1", "run_python", path="tools/E1.py", exit_code=0, stdout="observed", **{field: items})
                _, sent, executed, _, memory = self.run_loop([answer("run_python"), answer()], [output], calls=2)
                self.assertEqual(len(sent), 2)
                self.assertEqual(len(executed), 1)
                context = json.loads(sent[-1]["messages"][1]["content"])
                self.assertTrue(context["remaining"]["finish_soon"])
                self.assertEqual(context["j2"]["executions"][0]["source_path"], "tools/E1.py")
                for record in (context["j2"]["executions"][0], context["recent"][0]["result"]):
                    self.assertEqual(len(record.get(field, [])) + record.get("omitted_" + field, 0), 16)
                self.assertEqual(len(memory["executions"][0][field]), 16)

    def test_report_labels_comparison_claim_without_promoting_it_to_a_finding(self):
        notes = CandidateNotebook()
        notes.note_candidate("widget", evidence_level="controlled_comparison", observation_steps=[1, 99], state="closed")
        outputs = [ExecutionResult("E1", "run_python", exit_code=0, stdout="normal output"),
                   ExecutionResult("E2", "run_python", exit_code=0, stdout="comparison output", investigation_notes=notes.notes)]
        result, _, _, state, _ = self.run_loop([answer("run_python"), answer("run_python"), answer()], outputs)
        report = compose_report(state["findings"], *result, j2_state=state["j2_state"])
        self.assertIn("통제 비교 주장", report)
        self.assertIn("99", report)
        self.assertIn("보안 영향의 독립 검증이 아님", report)

    def test_finish_review_reaches_report_without_forcing_an_extra_visit(self):
        notes = CandidateNotebook()
        notes.note_candidate("static page", basis="stopping within this scope", state="closed")
        output = ExecutionResult("E1", "run_python", exit_code=0, stdout="observed",
            investigation_notes=notes.notes, discovery_summaries=[{"source_url": "https://example.test/",
                "assets": [{"kind": "script", "url": "https://example.test/exact.js"}]}])
        result, sent, executed, state, memory = self.run_loop([answer("run_python"), answer()], [output])
        self.assertEqual(len(executed), 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual(result[0], "completed")
        context = json.loads(sent[-1]["messages"][1]["content"])
        self.assertEqual(context["j2"]["finish_review"]["unreviewed_resources"], 1)
        report = compose_report(state["findings"], *result, j2_state=state["j2_state"])
        self.assertIn("분석 기록 없는 리소스 1", report)
        self.assertIn("exact.js", report)
        self.assertEqual(state["finish_review"]["unreviewed_resources"], 1)
        self.assertEqual(memory["pipeline"]["review"]["unreviewed_resources"], 1)

    def test_declarations_and_caught_errors_reach_next_model_input_memory_and_report(self):
        summary = {"source_url": "https://example.test/", "assets": [{"url": "https://example.test/exact-hash.js", "kind": "script"}],
                   "surfaces": [], "hints": [{"kind": "navigation", "url": "https://example.test/sign-in"}]}
        output = ExecutionResult("E1", "run_python", exit_code=0, stdout="caught timeout",
            discovery_summaries=[summary], helper_errors=[{"helper": "goto_and_render", "error_type": "TimeoutError", "url": "https://example.test/?secret=value"}])
        result, sent, _, state, memory = self.run_loop([answer("run_python"), answer()], [output])
        context = json.loads(sent[-1]["messages"][1]["content"])
        self.assertEqual(context["j2"]["discovery"]["assets"][0]["url"], "https://example.test/exact-hash.js")
        self.assertEqual(memory["discovery_summaries"][0]["step"], 1)
        self.assertNotIn("secret", json.dumps(memory["executions"][0]["helper_errors"]))
        self.assertEqual(context["j2"]["executions"][0]["helper_errors"][0]["error_type"], "TimeoutError")
        report = compose_report(state["findings"], *result, j2_state=state["j2_state"])
        self.assertIn("도우미 오류: goto_and_render", report)
        self.assertIn("리소스 1 · 입력 선언 0 · 힌트 1", report)
        self.assertIn("실제 접근·분석 완료나 입력 처리 확인을 뜻하지 않음", report)
        self.assertEqual(result[0], "incomplete")

    def test_preflight_repair_does_not_execute_invalid_code_or_spend_a_tool_slot(self):
        bad = answer("run_python")
        value = json.loads(bad["message"]["content"])
        value["code"] = "page.goto('https://example.test/')"
        bad["message"]["content"] = json.dumps(value)
        output = ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout="normal observation")
        _, sent, executed, state, memory = self.run_loop([bad, answer("run_python"), answer()], [output], calls=3)
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(executed), 1)
        self.assertEqual(state["tool_calls"], 1)
        rejection = memory["rejected_actions"][0]
        self.assertEqual(rejection["call_id"], "CALL-0001")
        self.assertEqual(rejection["outcome"], "not_run")
        self.assertTrue(rejection["source_path"].startswith("tools/"))
        self.assertIn(rejection["source_path"], sent[1]["messages"][-1]["content"])
        self.assertEqual(len(memory["executions"]), 1)

    def test_repeat_missing_import_is_stopped_before_execution(self):
        missing = answer("run_python")
        value = json.loads(missing["message"]["content"])
        value["code"] = "import requests"
        missing["message"]["content"] = json.dumps(value)
        outputs = [ExecutionResult("EXEC-1", "run_python", exit_code=1, exception_type="ModuleNotFoundError",
                                   exception="No module named 'requests'"),
                   ExecutionResult("EXEC-2", "run_python", exit_code=0, stdout="used provided helper")]
        _, _, executed, _, memory = self.run_loop([missing, missing, answer("run_python"), answer()], outputs, calls=4)
        self.assertEqual(len(executed), 2)
        self.assertEqual(len(memory["rejected_actions"]), 1)

    def test_report_marks_failed_and_nonexistent_citations_without_promoting_stdout(self):
        findings = [{"step": 1, "status": "error", "evidence": False, "plan": "inspect cart",
                     "observed": "(no evidence)", "error": "missing module"}]
        policy = J2Policy()
        policy.record(ExecutionResult("E1", "run_python", exit_code=1, exception="missing module"), 1)
        report = compose_report(findings, "incomplete", "검사를 완료했다 [step:1] [step:99]", j2_state=policy.snapshot())
        self.assertIn("존재하지 않는 단계 참조: 99", report)
        self.assertIn("실패 단계 참조: 1", report)
        self.assertIn("HTTP 응답 메타데이터", report)
        self.assertIn("보안 영향", report)
        self.assertLess(report.index("존재하지 않는 단계 참조"), report.index("검사를 완료했다"))
    def run_loop(self, replies, outputs=(), calls=4):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder), "d" * 32, {"target": "https://example.test/", "goal": "inspect",
                "prompt_version": "J2", "model": "fixture", "model_settings": {"options": {"num_ctx": 8192}},
                "budgets": {"seconds": 1200, "model_calls": calls, "tool_calls": 80, "command_seconds": 30}})
            runtime = CommandRuntime(Path(folder), log)
            runtime.start_session(log.run_id, browser=False)
            sent, executed = [], []
            replies, outputs = iter(replies), iter(outputs)
            def transport(payload):
                sent.append(payload)
                yield next(replies)
            def execute(*args, **kwargs):
                executed.append(args[1])
                output = next(outputs)
                if output.path.startswith("tools/"):
                    output.path = str(runtime.runtime_dir / output.path)
                return output
            try:
                with patch.object(runtime, "run", side_effect=execute):
                    result = ExplorerV3(Path(folder), transport=transport, browser_enabled=False)._loop(log.run_id, log, runtime)
                memory = json.loads((runtime.runtime_dir / "work/j2-memory.json").read_text())
                return result, sent, executed, dict(log.metadata), memory
            finally:
                runtime.stop(log.run_id)
                runtime.destroy(log.run_id)
                log.close()

    def test_pending_candidate_reaches_next_turn_saved_memory_and_report(self):
        notebook = CandidateNotebook()
        notebook.note_candidate("invoice download", basis="observed link", next_observation="owner comparison")
        output = ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout="observed", investigation_notes=notebook.notes)
        result, sent, _, state, memory = self.run_loop([answer("run_python"), answer()], [output])
        context = json.loads(sent[-1]["messages"][1]["content"])
        self.assertNotIn("j1", context)
        self.assertEqual(context["j2"]["candidates"][0]["name"], "invoice download")
        self.assertEqual(memory["candidates"][0]["next_observation"], "owner comparison")
        self.assertEqual(result[0], "incomplete")
        report = compose_report(state["findings"], *result, j2_state=state["j2_state"])
        self.assertIn("owner comparison", report)
        self.assertIn("미검증", report)

    def test_last_call_is_for_reporting_at_custom_limits(self):
        for calls in (1, 40, 50, 60):
            with self.subTest(calls=calls):
                outputs = [ExecutionResult(f"EXEC-{i}", "run_python", exit_code=0, stdout=f"observation {i}")
                           for i in range(calls - 1)]
                result, sent, executed, _, _ = self.run_loop([answer("run_python")] * (calls - 1) + [answer()], outputs, calls)
                self.assertEqual(len(sent), calls)
                self.assertEqual(len(executed), calls - 1)
                self.assertTrue(json.loads(sent[-1]["messages"][1]["content"])["remaining"]["finish_soon"])

    def test_reporting_call_cannot_execute_another_probe(self):
        notebook = CandidateNotebook()
        notebook.note_candidate("download", next_observation="approved owner comparison remains")
        output = ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout="observation", investigation_notes=notebook.notes)
        result, sent, executed, state, _ = self.run_loop([answer("run_python"), answer("run_python")], [output], 2)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(executed), 1)
        self.assertEqual(result[0], "error")
        report = compose_report(state["findings"], *result, j2_state=state["j2_state"])
        self.assertIn("approved owner comparison remains", report)

    def test_early_finish_without_observations_is_incomplete(self):
        result, _, executed, _, _ = self.run_loop([answer()])
        self.assertEqual(result[0], "incomplete")
        self.assertEqual(executed, [])

    def test_closed_work_can_finish_without_forcing_another_attack(self):
        notebook = CandidateNotebook()
        notebook.note_candidate("static page", basis="no supported test remains", state="closed")
        output = ExecutionResult("EXEC-1", "run_python", exit_code=0, stdout="observed",
                                  investigation_notes=notebook.notes)
        result, sent, executed, _, _ = self.run_loop([answer("run_python"), answer()], [output])
        self.assertEqual(result[0], "completed")
        self.assertEqual(len(executed), 1)

    def test_retry_packing_keeps_j2_memory_and_goal(self):
        prompt, digest = load_prompt("J2")
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder), "e" * 32, {})
            self.addCleanup(log.close)
            sent = []
            def transport(payload):
                sent.append(payload)
                yield ({"message": {"content": "{" + "x" * 20000}, "done": True} if len(sent) == 1 else answer())
            harness = Harness(log, transport=transport, system_prompt=prompt, system_prompt_version="J2",
                              system_prompt_sha256=digest, max_calls=3)
            context = {"goal": "inspect", "j2": J2Policy().context(), "recent": [{"stdout": "x" * 20000}]}
            harness.decide(log.run_id, context, model="fixture", settings={"options": {"num_ctx": 8192}})
            self.assertEqual(len(sent), 2)
            for payload in sent:
                self.assertLessEqual(sum(len(m["content"]) for m in payload["messages"]), int(8192 * 3.3 * .65))
                self.assertEqual(json.loads(payload["messages"][1]["content"])["j2"]["memory_path"], "work/j2-memory.json")

    def test_failed_code_recovery_keeps_both_ends_and_a_usable_source_path(self):
        code = "first_binding = 'normal'\n" + "# intermediate code\n" * 100 + "raise ValueError('retry')"
        initial = answer("run_python")
        decoded = json.loads(initial["message"]["content"])
        decoded["code"] = code
        initial["message"]["content"] = json.dumps(decoded)
        output = ExecutionResult("EXEC-1", "run_python", path="tools/EXEC-1.py", exit_code=1,
                                 exception="ValueError: retry")
        _, sent, _, _, _ = self.run_loop([initial, answer()], [output])
        recent = json.loads(sent[-1]["messages"][1]["content"])["recent"][0]
        self.assertTrue(recent["command"]["code"].startswith("first_binding = 'normal'"))
        self.assertTrue(recent["command"]["code"].endswith("raise ValueError('retry')"))
        self.assertEqual(recent["result"]["path"], "tools/EXEC-1.py")

    def test_working_source_remains_findable_after_leaving_recent_window(self):
        outputs = [ExecutionResult(f"EXEC-{i}", "run_python", path=f"tools/EXEC-{i}.py",
                                   exit_code=0, stdout=f"normal observation {i}") for i in range(1, 4)]
        _, sent, _, _, _ = self.run_loop([answer("run_python")] * 3 + [answer()], outputs)
        context = json.loads(sent[-1]["messages"][1]["content"])
        self.assertEqual(context["history"][0]["source_path"], "tools/EXEC-1.py")
        self.assertEqual(len(context["recent"]), 2)

    def test_missing_module_feedback_survives_other_steps(self):
        outputs = [ExecutionResult("EXEC-1", "run_python", exit_code=1,
                     exception="ModuleNotFoundError: No module named 'requests'", exception_type="ModuleNotFoundError"),
                   ExecutionResult("EXEC-2", "run_python", exit_code=0, stdout="observed normal page")]
        _, sent, _, _, _ = self.run_loop([answer("run_python"), answer("run_python"), answer()], outputs)
        context = json.loads(sent[-1]["messages"][1]["content"])
        self.assertEqual(context["browser_api"]["unavailable_modules"], ["requests"])
        self.assertEqual(context["browser_api"]["http_get"]["body_limit"], 2_000_000)

    def test_report_recovery_does_not_turn_untracked_execution_failures_into_completion(self):
        outputs = [ExecutionResult("EXEC-1", "run_python", exit_code=1, exception="missing module"),
                   ExecutionResult("EXEC-2", "run_python", exit_code=0, stdout="initial HTML only")]
        final = answer()
        value = json.loads(final["message"]["content"])
        value["content"] = "보고서: 입력 검증과 공격 진행은 미완료"
        final["message"]["content"] = json.dumps(value)
        result, sent, executed, _, _ = self.run_loop([answer("run_python"), answer("run_python"), final], outputs, calls=3)
        self.assertEqual(result[0], "incomplete")
        self.assertIn("입력 검증과 공격 진행은 미완료", result[1])
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(executed), 2)

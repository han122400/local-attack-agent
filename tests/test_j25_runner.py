import json
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from command_runtime import ExecutionResult
from j25_runner import J25Runner, compose_j25_report, rank_j25_references
from browser_http import BrowserHTTP
from j25_actions import J25Actions
from session_log import SessionLog
from scorecard import score_run


RUN_ID = "7" * 32


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def run(self, run_id, command, *, call_id, timeout):
        self.calls.append(command["decision"])
        action = command["decision"]["action"]
        refs = {"pages": [{"id": "P1", "url": "https://fixture.invalid/", "source": "target"}],
                "assets": [], "inputs": [{"id": "I1", "url": "https://fixture.invalid/search",
                                           "field": "q", "basis": "declared", "source": "P1"}]}
        result = {"action": action, "ref": "P1" if action == "start" else "I1", "status": "ok",
                  "references": refs, "omitted": {"pages": 0, "assets": 0, "inputs": 0}, "request_count": 0}
        if action == "compare":
            result.update(request_count=3, comparison={"samples": [{"stage": "normal", "status": 200},
                {"stage": "test", "status": 500}, {"stage": "recheck", "status": 200}],
                "baseline_stable": True, "test_differs": True})
        return ExecutionResult(f"EXEC-{len(self.calls):04d}", "j25", exit_code=0, j25_result=result)


class RunnerTests(unittest.TestCase):
    def test_equal_priority_references_keep_discovery_order_and_avoid_recent_ref(self):
        refs = {"pages": [
            {"id": "first", "url": "https://fixture.invalid/first"},
            {"id": "last", "url": "https://fixture.invalid/last"}],
            "assets": [], "inputs": []}
        self.assertEqual([x["id"] for x in rank_j25_references(refs, "inspect", set(), set())["pages"]],
                         ["first", "last"])
        self.assertEqual([x["id"] for x in rank_j25_references(refs, "inspect", set(), {"first"})["pages"]],
                         ["last", "first"])

    def test_page_that_failed_twice_is_not_selectable_after_other_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 3,
                    "tool_calls": 3, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            runner = J25Runner(RUN_ID, log, FakeRuntime())
            runner.state["references"]["pages"] = [{"id": "P1", "url": "https://fixture.invalid/redirect"}]
            runner.state["steps"] = [
                {"action": "observe", "ref": "P1", "status": "incomplete", "error": "ValueError"},
                {"action": "observe", "ref": "P1", "status": "incomplete", "error": "ValueError"},
                {"action": "analyze", "ref": "A1", "status": "ok", "error": ""}]
            self.assertIn("P1", runner._failed_observations())
            with self.assertRaises(ValueError):
                runner._guard(SimpleNamespace(action="observe", ref="P1"), False)

    def test_recent_page_focuses_one_matching_verification_step(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {"goal": "inspect",
                "target": "https://fixture.invalid/", "model": "fixture", "model_settings": {},
                "j25_identity_names": ["public", "owner"],
                "budgets": {"seconds": 300, "model_calls": 3, "tool_calls": 3, "command_seconds": 30}})
            self.addCleanup(log.close)
            runner = J25Runner(RUN_ID, log, FakeRuntime())
            runner.state["steps"] = [{"action": "observe", "status": "ok", "ref": "P2",
                "exec_id": "EXEC-2", "reference": {"url": "https://fixture.invalid/search"}}]
            runner.state["references"]["inputs"] = [{"id": "I1", "url": "https://fixture.invalid/search",
                "source": "P2", "supported": True, "field": "q"}]
            self.assertEqual(runner._focus_for_recent(), {"action": "verify", "refs": ["I1"]})
            runner.state["references"]["inputs"] = []
            self.assertEqual(runner._focus_for_recent(), {"action": "verify_access", "refs": ["P2"]})

    def test_verified_case_ledger_is_saved_and_used_by_report(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 2,
                    "tool_calls": 2, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            runner = J25Runner(RUN_ID, log, FakeRuntime())
            runner.state["steps"].append({"step": 1, "exec_id": "EXEC-1", "action": "verify",
                "ref": "I1", "status": "ok", "identity": "public", "verification_kind": "disclosure",
                "reference": {"url": "https://fixture.invalid/private", "field": "q", "method": "GET"},
                "verification": {"kind": "disclosure", "status": "independently_verified",
                                 "information": ["private-note"], "reason": "private_canary_observed"},
                "comparison": {"samples": [{"stage": "test", "body_sha256": "a" * 64}]}})
            runner._save()
            self.assertEqual(log.metadata["j25_state"]["evidence"]["counts"]["independently_verified"], 1)
            report = compose_j25_report(log.metadata["j25_state"], "completed", "done")
            self.assertIn("검증된 고유 사례 1건", report)
            self.assertIn("private-note", report)
            self.assertIn("전체 취약점 대비 비율: 측정 불가", report)
            self.assertIn("시험 순서: 기준→시험→재확인", report)
            self.assertIn("수정 방향:", report)

    def test_model_identity_and_verification_kind_reach_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "j25_identity_names": ["public", "owner"],
                "budgets": {"seconds": 300, "model_calls": 6, "tool_calls": 5, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            replies = iter([
                {"action": "identity", "value": "owner", "reason": "select owner"},
                {"action": "verify", "ref": "I1", "kind": "injection", "hypothesis": "private data",
                 "normal": "plain", "test": "probe", "reason": "verify input"},
                {"action": "finish", "reason": "done"},
            ])
            def transport(_payload):
                yield {"message": {"content": json.dumps(next(replies))}, "done": True}
            runtime = FakeRuntime()
            J25Runner(RUN_ID, log, runtime, transport=transport).run()
            self.assertEqual(runtime.calls[1]["value"], "owner")
            self.assertEqual(runtime.calls[2]["kind"], "injection")

    def test_korean_search_goal_ranks_english_search_path(self):
        refs = {"pages": [], "assets": [], "inputs": [
            {"id": "search", "url": "https://fixture.invalid/api/search", "field": "q", "supported": True},
            {"id": "status", "url": "https://fixture.invalid/api/status", "field": "q", "supported": True},
        ]}
        ranked = rank_j25_references(refs, "검색", set(), set())
        self.assertEqual([item["id"] for item in ranked["inputs"]], ["search", "status"])

    def test_korean_input_goal_ranks_search_before_unrelated_api(self):
        refs = {"pages": [], "assets": [], "inputs": [
            {"id": "challenge", "url": "https://fixture.invalid/api/challenges", "field": "q", "supported": True},
            {"id": "search", "url": "https://fixture.invalid/rest/products/search", "field": "q", "supported": True},
        ]}
        ranked = rank_j25_references(refs, "입력과 정보 노출을 조사", set(), set())
        self.assertEqual([item["id"] for item in ranked["inputs"]], ["search", "challenge"])

    def test_stopped_run_without_a_model_decision_does_not_attribute_an_assessment(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 2, "tool_calls": 2, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            result, _ = J25Runner(RUN_ID, log, FakeRuntime(), stop_requested=lambda: True).run()
            self.assertEqual(result, "stopped")
            self.assertEqual(log.metadata.get("goal_status_source"), "not_assessed")

    def test_search_candidate_ranks_ahead_of_socket_noise_without_losing_saved_references(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "compare search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 4, "tool_calls": 3, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class NoisyRuntime(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    result.j25_result["references"]["inputs"] = [
                        {"id": f"I{i}", "url": "https://fixture.invalid/socket.io/", "field": "sid",
                         "supported": True, "basis": "observed", "source": "EXEC-1"} for i in range(40)]
                    result.j25_result["references"]["inputs"].append(
                        {"id": "I-search", "url": "https://fixture.invalid/search", "field": "q",
                         "supported": True, "basis": "observed", "source": "EXEC-1"})
                    if command["decision"]["action"] == "compare":
                        result.j25_result["ref"] = "I-search"
                    return result
            contexts = []
            def transport(payload):
                context = json.loads(payload["messages"][1]["content"])
                contexts.append(context)
                decision = ({"action": "compare", "ref": "I-search", "reason": "search", "hypothesis": "different",
                             "normal": "apple", "test": "banana"} if len(contexts) == 1 else
                            {"action": "finish", "reason": "report"})
                yield {"message": {"content": json.dumps(decision)}, "done": True}
            J25Runner(RUN_ID, log, NoisyRuntime(), transport=transport).run()
            inputs = contexts[0]["references"]["inputs"]
            self.assertLessEqual(len(inputs), 12)
            self.assertEqual(inputs[0]["id"], "I-search")
            self.assertGreater(contexts[0]["context_omitted"]["inputs"], 0)
            self.assertIn("I-search", [item["id"] for item in contexts[1]["references"]["inputs"]])
            self.assertEqual(len(log.metadata["j25_state"]["references"]["inputs"]), 41)
            report = compose_j25_report(log.metadata["j25_state"], "incomplete", "report")
            self.assertIn("40건", report)
            self.assertIn("20건 표시", report)

    def test_comparison_without_three_samples_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "compare search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 3, "tool_calls": 3, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class MissingSamples(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    if command["decision"]["action"] == "compare":
                        result.j25_result["comparison"] = {"samples": [], "baseline_stable": True}
                    return result
            replies = iter([
                {"action": "compare", "ref": "I1", "reason": "compare", "hypothesis": "different",
                 "normal": "apple", "test": "banana"},
                {"action": "finish", "reason": "report"},
            ])
            def transport(_payload):
                yield {"message": {"content": json.dumps(next(replies))}, "done": True}
            result, _ = J25Runner(RUN_ID, log, MissingSamples(), transport=transport).run()
            self.assertEqual(result, "incomplete")
            self.assertEqual(log.metadata["j25_state"]["steps"][-1]["status"], "incomplete")
            self.assertIsNone(log.metadata["first_comparison_step"])

    def test_model_goal_assessment_is_separate_from_unvisited_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "compare search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 4, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class AdditionalPage(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    result.j25_result["references"]["pages"].append({"id": "P2", "url": "https://fixture.invalid/other"})
                    return result
            answers = iter([
                {"action": "compare", "ref": "I1", "reason": "compare", "hypothesis": "different",
                 "normal": "apple", "test": "banana"},
                {"action": "finish", "reason": "comparison observed", "goal_status": "completed"},
            ])
            contexts = []
            def transport(payload):
                contexts.append(json.loads(payload["messages"][1]["content"]))
                yield {"message": {"content": json.dumps(next(answers, {"action": "finish", "reason": "report"}))}, "done": True}
            result, reason = J25Runner(RUN_ID, log, AdditionalPage(), transport=transport).run()
            self.assertEqual(result, "incomplete")
            self.assertEqual(log.metadata.get("goal_status"), "completed")
            self.assertEqual(log.metadata.get("goal_status_source"), "model_unverified")
            self.assertEqual(log.metadata.get("coverage_status"), "partial")
            self.assertEqual([item["id"] for item in contexts[0]["references"]["pages"]], ["P2"])
            self.assertIn("P1", [item["id"] for item in log.metadata["j25_state"]["references"]["pages"]])
            report = compose_j25_report(log.metadata["j25_state"], result, reason)
            self.assertIn("모델 판단", report)
            self.assertIn("미검증", report)
            self.assertIn("P2", report)

    def test_model_cannot_mark_goal_completed_without_any_successful_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 2, "tool_calls": 2, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class FailedStart(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    result.j25_result.update(status="incomplete", error="TimeoutError")
                    return result
            def transport(_payload):
                yield {"message": {"content": '{"action":"finish","reason":"done","goal_status":"completed"}'}, "done": True}
            result, _ = J25Runner(RUN_ID, log, FailedStart(), transport=transport).run()
            self.assertEqual(result, "incomplete")
            self.assertEqual(log.metadata.get("goal_status"), "unknown")
            self.assertEqual(log.metadata.get("coverage_status"), "partial")

    def test_metrics_count_retries_and_record_first_successful_comparison(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "compare search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 6, "tool_calls": 5, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class UnstableFirstComparison(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    if command["decision"].get("test") == "banana":
                        result.j25_result["comparison"]["baseline_stable"] = False
                    return result
            answers = iter([
                {"action": "invalid", "reason": "bad"},
                {"action": "compare", "ref": "I1", "reason": "try", "hypothesis": "different",
                 "normal": "apple", "test": "banana"},
                {"action": "compare", "ref": "I1", "reason": "try again", "hypothesis": "different",
                 "normal": "apple", "test": "grape"},
                {"action": "finish", "reason": "report"},
            ])
            def transport(_payload):
                yield {"message": {"content": json.dumps(next(answers))}, "done": True}
            runner = J25Runner(RUN_ID, log, UnstableFirstComparison(), transport=transport)
            runner.run()
            self.assertEqual(log.metadata.get("model_calls"), 4)
            self.assertGreaterEqual(log.metadata["model_decision_seconds"], 0)
            metrics = score_run(log.metadata, Path(temp))["j25_metrics"]
            self.assertEqual(metrics["first_comparison_step"], 3)
            self.assertEqual(metrics["model_calls"], 4)
            self.assertEqual(metrics["repeat_blocks"], 0)

    def test_repeated_identical_failure_is_blocked_until_another_action_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 7, "tool_calls": 6, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class FailingPageRuntime(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    if command["decision"]["action"] in {"start", "observe"}:
                        result.j25_result.update(ref="P1", status="incomplete", error="TimeoutError")
                    return result
            answers = iter([
                {"action": "observe", "ref": "P1", "reason": "retry once"},
                {"action": "observe", "ref": "P1", "reason": "repeat"},
                {"action": "compare", "ref": "I1", "reason": "try input", "hypothesis": "different",
                 "normal": "apple", "test": "banana"},
                {"action": "observe", "ref": "P1", "reason": "try after other work"},
                {"action": "finish", "reason": "report"},
            ])
            def transport(_payload):
                yield {"message": {"content": json.dumps(next(answers))}, "done": True}
            runtime = FailingPageRuntime()
            runner = J25Runner(RUN_ID, log, runtime, transport=transport)
            runner.run()
            self.assertEqual([item["action"] for item in runtime.calls], ["start", "observe", "compare", "observe"])
            self.assertEqual(log.metadata["j25_state"]["repeat_blocks"], 1)

    def test_failed_comparison_does_not_block_a_different_test_value(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "compare search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 6, "tool_calls": 5, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class FailingValue(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    if command["decision"].get("test") == "banana":
                        result.j25_result.update(status="incomplete", error="TimeoutError")
                    return result
            answers = iter([
                {"action": "compare", "ref": "I1", "reason": "compare", "hypothesis": "different",
                 "normal": "apple", "test": "banana"},
                {"action": "compare", "ref": "I1", "reason": "retry", "hypothesis": "different",
                 "normal": "apple", "test": "banana"},
                {"action": "compare", "ref": "I1", "reason": "try new value", "hypothesis": "different",
                 "normal": "apple", "test": "grape"},
                {"action": "finish", "reason": "report"},
            ])
            def transport(_payload):
                yield {"message": {"content": json.dumps(next(answers))}, "done": True}
            runtime = FailingValue()
            J25Runner(RUN_ID, log, runtime, transport=transport).run()
            self.assertEqual([item.get("test") for item in runtime.calls], [None, "banana", "banana", "grape"])
            self.assertEqual(log.metadata["repeat_blocks"], 0)

    def test_successfully_observed_page_is_not_reobserved(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "compare search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 4, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            try:
                runner = J25Runner(RUN_ID, log, FakeRuntime())
                runner.state["references"]["pages"] = [{"id": "P1", "url": metadata["target"]}]
                runner.state["examined"].append("P1")
                with self.assertRaisesRegex(ValueError, "already observed"):
                    runner._guard(SimpleNamespace(action="observe", ref="P1"), False)
                runner.state["references"]["inputs"] = [{"id": "I1", "supported": True}]
                runner.state["steps"].append({"action": "compare", "ref": "I1", "normal": "alpha",
                                              "test": "beta", "status": "ok"})
                with self.assertRaisesRegex(ValueError, "already compared"):
                    runner._guard(SimpleNamespace(action="compare", ref="I1", normal="alpha", test="beta"), False)
            finally:
                log.close()

    def test_full_lifecycle_uses_private_target_and_persists_fixed_action_report(self):
        from explorer import ExplorerV3
        with tempfile.TemporaryDirectory() as temp:
            observed_targets = []
            class LifecycleRuntime(FakeRuntime):
                def __init__(self, root, log):
                    super().__init__()
                    self.exists = False
                def start_session(self, run_id, *, target, browser, j25_test_scope=None):
                    self_test.assertIsNone(j25_test_scope)
                    observed_targets.append(target)
                    self.exists = True
                def inspect(self, run_id):
                    return {"runtime_exists": self.exists, "managed_processes": []}
                def stop(self, run_id, timeout, **kwargs):
                    pass
                def export_evidence(self, run_id):
                    return []
                def destroy(self, run_id, timeout):
                    self.exists = False
            calls = []
            def transport(payload):
                calls.append(payload)
                value = ({"action": "compare", "ref": "I1", "reason": "compare", "hypothesis": "test",
                          "normal": "a", "test": "b"} if len(calls) == 1 else {"action": "finish", "reason": "done"})
                yield {"message": {"content": json.dumps(value)}, "done": True}
            target = "https://fixture.invalid/?token=private-value"
            self_test = self
            explorer = ExplorerV3(runs_root=Path(temp), browser_enabled=False, transport=transport)
            with patch("explorer.CommandRuntime", LifecycleRuntime):
                run_id = explorer.start(target=target, goal="inspect", model="fixture", prompt_version="J2.5",
                                        budgets={"seconds": 300, "model_calls": 3, "tool_calls": 4})
                explorer.worker.join(5)
            self.assertFalse(explorer.worker.is_alive())
            self.assertEqual(observed_targets, [target])
            session = json.loads((Path(temp) / run_id / "session.json").read_text())
            self.assertEqual(session["phase"], "finished")
            self.assertIn("EXEC-0002", session["report"])
            self.assertNotIn("private-value", json.dumps(session))
            self.assertIsNone(explorer._private_target)

    def test_j25_initial_target_query_is_not_written_to_session(self):
        from explorer import ExplorerV3
        with tempfile.TemporaryDirectory() as temp:
            explorer = ExplorerV3(runs_root=Path(temp), browser_enabled=False)
            target = "https://fixture.invalid/search?token=secret-value#/private"
            try:
                with patch("explorer.threading.Thread"):
                    run_id = explorer.start(target=target, goal="inspect", model="fixture", prompt_version="J2.5")
                session = (Path(temp) / run_id / "session.json").read_text()
                self.assertNotIn("secret-value", session)
                self.assertEqual(json.loads(session)["target"], "https://fixture.invalid/search")
                self.assertEqual(explorer._private_target, target)
            finally:
                explorer.lease.release()

    def test_worker_restart_keeps_previous_unfinished_references_in_report(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 3, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class RestartRuntime(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    if command["decision"]["action"] == "compare":
                        result.exit_code = 1
                        result.j25_result = {"action": "compare", "ref": "I1", "status": "incomplete",
                            "error": "expired", "references": {"pages": [{"id": "P2", "url": "https://fixture.invalid/"}],
                            "assets": [], "inputs": []}, "omitted": {}}
                    return result
            calls = []
            def transport(payload):
                calls.append(payload)
                decision = ({"action": "compare", "ref": "I1", "reason": "compare", "hypothesis": "test",
                             "normal": "a", "test": "b"} if len(calls) == 1 else {"action": "finish", "reason": "stopped"})
                yield {"message": {"content": json.dumps(decision)}, "done": True}
            status, reason = J25Runner(RUN_ID, log, RestartRuntime(), transport=transport, stop_requested=lambda: False).run()
            report = compose_j25_report(log.metadata["j25_state"], status, reason)
            self.assertEqual(status, "incomplete")
            self.assertIn("https://fixture.invalid/search", report)
            self.assertIn("I1", log.metadata["j25_state"]["pending"])

    def test_truncated_observation_and_visible_input_reach_model_and_incomplete_report(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 2, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            class TruncatedRuntime(FakeRuntime):
                def run(self, run_id, command, *, call_id, timeout):
                    result = super().run(run_id, command, call_id=call_id, timeout=timeout)
                    result.j25_result.update(truncated=True, visible_inputs=[{"name": "search"}])
                    return result
            contexts = []
            def transport(payload):
                contexts.append(json.loads(payload["messages"][1]["content"]))
                yield {"message": {"content": '{"action":"finish","reason":"limited"}'}, "done": True}
            status, _ = J25Runner(RUN_ID, log, TruncatedRuntime(), transport=transport, stop_requested=lambda: False).run()
            self.assertEqual(status, "incomplete")
            self.assertTrue(contexts[0]["recent"][0]["truncated"])
            self.assertEqual(contexts[0]["recent"][0]["visible_inputs"][0]["name"], "search")

    def test_real_fixed_actions_connect_to_model_selection_without_a_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 3, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            page = SimpleNamespace(url=metadata["target"], content=AsyncMock(return_value=
                '<form action="/search" method="get"><input name="q"></form>'))
            http = BrowserHTTP(SimpleNamespace(), metadata["target"], capture_gets=True)
            samples = [{"status": status, "body": text, "length": len(text), "body_truncated": False, "binary": False}
                       for status, text in ((200, "normal"), (200, "changed"), (200, "normal"))]
            http.http_get = AsyncMock(side_effect=samples)
            async def goto(url):
                page.url = url
            actions = J25Actions(metadata["target"], page, http, goto, AsyncMock(return_value={"inputs": []}))
            class InProcessRuntime:
                def run(self, run_id, command, *, call_id, timeout):
                    value = asyncio.run(actions.perform(command["decision"]))
                    return ExecutionResult("EXEC-" + str(len(log.metadata.get("j25_state", {}).get("steps", [])) + 1),
                                           "j25", exit_code=0 if value["status"] == "ok" else 1, j25_result=value)
            calls = []
            def transport(payload):
                calls.append(payload)
                if len(calls) == 1:
                    context = json.loads(payload["messages"][1]["content"])
                    decision = {"action": "verify", "ref": context["references"]["inputs"][0]["id"],
                        "kind": "injection", "reason": "verify", "hypothesis": "input changes response",
                        "normal": "hello", "test": "hello2"}
                else:
                    decision = {"action": "finish", "reason": "comparison recorded"}
                yield {"message": {"content": json.dumps(decision)}, "done": True}
            status, _ = J25Runner(RUN_ID, log, InProcessRuntime(), transport=transport, stop_requested=lambda: False).run()
            self.assertEqual(status, "completed")
            self.assertEqual(http.http_get.await_count, 3)
            comparison = log.metadata["j25_state"]["steps"][-1]["comparison"]
            self.assertTrue(comparison["baseline_stable"])
            self.assertTrue(comparison["test_differs"])

    def test_explorer_dispatches_j25_to_the_fixed_runner(self):
        from explorer import ExplorerV3
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"prompt_version": "J2.5", "budgets": {"seconds": 300, "model_calls": 2,
                        "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            explorer = ExplorerV3(runs_root=Path(temp), browser_enabled=False)
            with patch("j25_runner.J25Runner.run", return_value=("incomplete", "fixture")) as run:
                result = explorer._loop(RUN_ID, log, object())
            self.assertEqual(result, ("incomplete", "fixture"))
            run.assert_called_once()

    def test_fixed_execution_reaches_comparison_and_unverified_report(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect search", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 3, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            sent = []
            def transport(payload):
                sent.append(payload)
                result = ({"action": "compare", "ref": "I1", "reason": "compare input",
                           "hypothesis": "input affects response", "normal": "hello", "test": "hello2"}
                          if len(sent) == 1 else {"action": "finish", "reason": "comparison recorded"})
                if len(sent) == 2:
                    recent = json.loads(payload["messages"][1]["content"])["recent"][-1]
                    self.assertEqual((recent["normal"], recent["test"]), ("hello", "hello2"))
                yield {"message": {"content": json.dumps(result)}, "done": True}
            runtime = FakeRuntime()
            runner = J25Runner(RUN_ID, log, runtime, transport=transport, stop_requested=lambda: False)
            status, reason = runner.run()
            self.assertEqual([x["action"] for x in runtime.calls], ["start", "compare"])
            self.assertEqual(status, "completed")
            self.assertEqual(log.metadata["j25_state"]["request_count"], 3)
            scored = score_run(log.metadata, Path(temp))
            self.assertEqual(scored["steps"], 2)
            self.assertEqual(scored["j25_metrics"]["comparisons"], 1)
            self.assertEqual(scored["j25_metrics"]["verified_findings"], 0)
            report = compose_j25_report(log.metadata["j25_state"], status, reason)
            self.assertIn("미검증", report)
            self.assertIn("안전성", report)
            self.assertIn("EXEC-0002", report)
            self.assertIn("https://fixture.invalid/search", report)
            self.assertIn("q", report)
            self.assertIn("hello2", report)
            self.assertNotIn("취약점 확정", report)

    def test_single_model_call_is_reserved_for_report_after_initial_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"goal": "inspect", "target": "https://fixture.invalid/", "model": "fixture",
                "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 1, "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), RUN_ID, metadata)
            self.addCleanup(log.close)
            runtime = FakeRuntime()
            def transport(payload):
                yield {"message": {"content": '{"action":"finish","reason":"report"}'}, "done": True}
            status, _ = J25Runner(RUN_ID, log, runtime, transport=transport, stop_requested=lambda: False).run()
            self.assertEqual([x["action"] for x in runtime.calls], ["start"])
            self.assertEqual(status, "incomplete")
            self.assertEqual(log.metadata.get("goal_status_source"), "not_assessed")

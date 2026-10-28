import json
import tempfile
import threading
import unittest
from pathlib import Path

from command_runtime import ExecutionResult
from session_log import SessionLog
from v4_loop import V4Runner, BudgetGate
from v4_policy import V4BudgetStop
from v4_tree import TreeStore
from test_scorer_session import FakeAdapter, good
from test_v4_tree import surface


class Runtime:
    """Orchestration fixture: preserves actual files and execution audit events."""
    def __init__(self, log, *, missing=False):
        self.log, self.missing = log, missing
        self.runtime_dir = log.run_dir / "runtime"
        (self.runtime_dir / "work" / "node-results").mkdir(parents=True)
        self.python, self.calls = "python", 0

    def run(self, run_id, payload, *, call_id, timeout):
        self.calls += 1
        exec_id = f"EXEC-{self.calls:04d}"
        self.log.append("exec_started", {"exec_id": exec_id, "call_id": call_id, "action": payload["action"]})
        result = ExecutionResult(exec_id=exec_id, action=payload["action"], exit_code=0)
        if payload["action"] == "run_command" and not self.missing:
            try:
                fixture = json.loads(payload["command"])
            except json.JSONDecodeError:
                fixture = None  # a probe command can submit the bound result in repair
            if fixture is not None:
                result_path = fixture.pop("result_path")
                output = self.runtime_dir / result_path
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(fixture), encoding="utf-8")
        if payload["action"] == "write_file":
            path = self.runtime_dir / payload["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload["content"], encoding="utf-8")
        self.log.append("exec_finished", {"exec_id": exec_id, "call_id": call_id, "action": payload["action"],
            "exit_code": 0, "timed_out": False, "cancelled": False, "failure_source": None})
        return result


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), "a" * 32, {"experiment_mode": "v4", "target": "https://example.test/",
            "model": "mock:model", "goal": "inspect", "model_settings": {"think": False, "options": {"num_ctx": 8192, "num_predict": 4096}},
            "budgets": {"seconds": 120, "model_calls": 30, "tool_calls": 30, "command_seconds": 10, "finalize_seconds": 10},
            "scorer_provider": "openrouter", "scorer_model": "typesafe/jev-1.14", "scorer_settings": {}, "v4_settings": {}})
        self.addCleanup(self.log.close)

    def transport(self, callback):
        def reply(payload):
            context = json.loads(payload["messages"][1]["content"])
            action = callback(context)
            yield {"message": {"content": json.dumps(action)}}
            yield {"done": True, "done_reason": "stop"}
        return reply

    def command(self, context, discovered=(), action="run_command"):
        result = {"schema_version": 1, "node_id": context["node"]["id"], "attempt_id": context["attempt"]["attempt_id"],
                  "discovered": list(discovered), "proposed_verdict": "confirmed", "claim": "model claim",
                  "evidence_refs": [], "next_observation": "", "result_path": context["attempt"]["result_path"]}
        return {"action": action, "plan": "inspect", "reason": "investigate current node", "path": "", "content": "",
                "code": "", "command": json.dumps(result) if action == "run_command" else ""}

    def test_greedy_descends_current_siblings_then_backtracks_and_exhausts(self):
        visited = []
        def callback(context):
            node = context["node"]["id"]
            visited.append(node)
            self.assertNotIn("dbSchemaChallenge", json.dumps(context))
            self.assertEqual(context["result_template"]["attempt_id"], context["attempt"]["attempt_id"])
            self.assertEqual(context["result_template"]["node_id"], node)
            self.assertIn("next_observation", context["result_template"])
            if node == "N0001":
                return self.command(context, [surface("https://example.test/low"), surface("https://example.test/high")])
            if node == "N0003":
                return self.command(context, [surface("https://example.test/high/deep")])
            return self.command(context)
        def score(ids):
            result = good(ids)
            from risk_rubric import validate_answer
            from test_scorer_contract import answer
            for key in ids:
                index = 1 if key == "N0002" else 3
                result["scores"][key] = validate_answer({**answer(index), "probabilities": {str(i): int(i == index) for i in range(5)}})
            return result
        runner = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter(score))
        result, reason = runner.run()
        self.assertEqual((result, reason), ("completed", "tree_exhausted"))
        self.assertEqual(visited, ["N0001", "N0003", "N0004", "N0002"])
        tree = TreeStore(self.log)
        tree.rebuild()
        self.assertTrue(all(node["exploration_state"] == "done" for node in tree.state["nodes"].values()))
        self.assertFalse(any(node["verification_state"] == "confirmed" for node in tree.state["nodes"].values()))

    def test_node_finish_does_not_stop_other_siblings(self):
        visited = []
        def callback(context):
            visited.append(context["node"]["id"])
            return self.command(context, [surface("https://example.test/a"), surface("https://example.test/b")]) if len(visited) == 1 else self.command(context, action="finish")
        result = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result, ("completed", "tree_exhausted"))
        self.assertEqual(visited, ["N0001", "N0002", "N0003"])

    def test_missing_result_is_closed_after_bounded_node_attempts(self):
        runtime = Runtime(self.log, missing=True)
        result = V4Runner(self.log, runtime, transport=self.transport(lambda ctx: self.command(ctx)), scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result, ("completed", "tree_exhausted"))
        self.assertEqual(runtime.calls, 2)
        state = TreeStore(self.log).rebuild()
        self.assertTrue(all(item["status"] == "failed" for item in state["attempts"].values()))

    def test_invalid_browser_code_is_corrected_before_spending_tool_attempt(self):
        responses = 0
        def callback(context):
            nonlocal responses
            responses += 1
            if responses == 1:
                return {"action": "run_python", "plan": "inspect input", "reason": "fetch baseline",
                        "path": "", "content": "", "command": "", "code": "http_get('https://example.test/search')"}
            return self.command(context)
        runtime = Runtime(self.log)
        runner = V4Runner(self.log, runtime, transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(responses, 2)
        self.assertEqual([event["action"] for event in self.log.snapshot_events() if event["kind"] == "exec_started"],
                         ["run_command"])
        self.assertTrue(any(event["kind"] == "model_error" and "add await" in event.get("message", "")
                            for event in self.log.snapshot_events()))

    def test_browser_code_without_result_path_is_corrected_before_execution(self):
        responses = 0
        def callback(context):
            nonlocal responses
            responses += 1
            if responses == 1:
                return {"action": "run_python", "plan": "inspect input", "reason": "fetch baseline",
                        "path": "", "content": "", "command": "",
                        "code": "response = await http_get('https://example.test/search'); print(response.status)"}
            return self.command(context)
        runtime = Runtime(self.log)
        runner = V4Runner(self.log, runtime, transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(responses, 2)
        self.assertTrue(any(event["kind"] == "model_error" and "result_path" in event.get("message", "")
                            for event in self.log.snapshot_events()))

    def test_shell_probe_without_result_path_uses_bound_result_repair(self):
        responses = 0
        def callback(context):
            nonlocal responses
            responses += 1
            if responses == 1:
                return {"action": "run_command", "plan": "probe", "reason": "read status", "path": "",
                        "content": "", "code": "", "command": "curl -I https://example.test/"}
            self.assertIn("result JSON", context["instruction"])
            return {"action": "write_file", "plan": "record probe", "reason": "bind result", "path": context["attempt"]["result_path"],
                    "content": json.dumps(context["result_template"]), "command": "", "code": ""}
        runtime = Runtime(self.log)
        runner = V4Runner(self.log, runtime, transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        self.assertEqual(runtime.calls, 2)
        self.assertEqual(responses, 2)
        self.assertEqual(TreeStore(self.log).rebuild()["attempts"]["A0001"]["status"], "imported")

    def test_missing_result_can_be_submitted_by_a_separate_bound_write(self):
        visited = []
        def callback(context):
            visited.append(context["node"]["id"])
            if "result JSON" in context.get("instruction", ""):
                discovered = surface("https://example.test/search")
                discovered["observations"] = {"input_control": "free text should be dropped"}
                value = {**context["result_template"], "discovered": [discovered]}
                return {"action": "write_file", "plan": "save observed surface", "reason": "complete the bound result",
                        "path": context["attempt"]["result_path"], "content": json.dumps(value), "command": "", "code": ""}
            if context["node"]["id"] != "N0001":
                return self.command(context, action="finish")
            return self.command(context)
        runner = V4Runner(self.log, Runtime(self.log, missing=True),
                          transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        state = TreeStore(self.log).rebuild()
        self.assertEqual(len(state["nodes"]), 2)
        self.assertEqual(state["attempts"]["A0001"]["status"], "imported")
        self.assertIn("v4_result_repair_finished", [event["kind"] for event in self.log.snapshot_events()])
        self.assertIn("v4_result_observations_dropped", [event["kind"] for event in self.log.snapshot_events()])
        self.assertEqual(visited, ["N0001", "N0001", "N0002"])

    def test_invalid_optional_route_template_preserves_discovery_without_model_repair(self):
        visited = []
        def callback(context):
            visited.append(context["node"]["id"])
            if context["node"]["id"] == "N0001":
                found = surface("https://example.test/search?q=")
                found["locator"]["route_template"] = "/search?q="
                return self.command(context, [found])
            return self.command(context, action="finish")
        runner = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        tree = TreeStore(self.log).rebuild()
        self.assertEqual(len(tree["nodes"]), 2)
        self.assertNotIn("route_template", tree["nodes"]["N0002"]["locator"])
        self.assertEqual(visited, ["N0001", "N0002"])
        normalized = [event for event in self.log.snapshot_events() if event["kind"] == "v4_result_optional_fields_dropped"]
        self.assertEqual(len(normalized), 1)
        original = self.log.run_dir / normalized[0]["original_ref"]["path"]
        self.assertIn('"route_template": "/search?q="', original.read_text(encoding="utf-8"))

    def test_invalid_discovery_container_still_reaches_bounded_result_repair(self):
        responses = 0
        def callback(context):
            nonlocal responses
            responses += 1
            if responses == 1:
                command = self.command(context)
                result = json.loads(command["command"])
                result["discovered"] = None
                result["observations"] = {"input_control": "invalid"}
                command["command"] = json.dumps(result)
                return command
            self.assertIn("result JSON", context["instruction"])
            return {"action": "write_file", "plan": "repair result", "reason": "submit valid JSON",
                    "path": context["attempt"]["result_path"], "content": json.dumps(context["result_template"]),
                    "command": "", "code": ""}
        runner = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        self.assertEqual(responses, 2)
        self.assertEqual(TreeStore(self.log).rebuild()["attempts"]["A0001"]["status"], "imported")

    def test_repair_uses_remaining_node_tool_budget(self):
        self.log.update(v4_settings={"node_tool_calls": 1})
        called = []
        def callback(context):
            called.append(context)
            return self.command(context)
        runner = V4Runner(self.log, Runtime(self.log, missing=True),
                          transport=self.transport(callback), scorer_adapter=FakeAdapter())
        self.assertEqual(runner.run(), ("completed", "tree_exhausted"))
        self.assertEqual(len(called), 1)
        self.assertEqual(TreeStore(self.log).rebuild()["attempts"]["A0001"]["status"], "failed")

    def test_optional_observation_sanitizer_keeps_valid_fields(self):
        value = {"observations": {"input_control": "full", "baseline_compared": True,
                                  "query_behavior": "free form", "unexpected": "ignored"}}
        clean, removed = V4Runner._sanitize_result_observations(value)
        self.assertEqual(clean["observations"]["input_control"], "full")
        self.assertTrue(clean["observations"]["baseline_compared"])
        self.assertEqual(clean["observations"]["query_behavior"], "unknown")
        self.assertEqual(set(removed), {"result.query_behavior", "result.unexpected"})

    def test_stop_is_honored_before_model_or_scorer(self):
        stop = threading.Event()
        stop.set()
        def unexpected(payload):
            self.fail("no model request after stop")
        result = V4Runner(self.log, Runtime(self.log), transport=unexpected, stop_event=stop, scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result, ("stopped", "user_stopped"))

    def test_execution_and_scoring_share_total_model_budget(self):
        self.log.update(budgets={**self.log.metadata["budgets"], "model_calls": 2})
        visited = []
        def callback(context):
            visited.append(context["node"]["id"])
            return self.command(context, [surface("https://example.test/a")])
        result = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result[1], "call_budget")
        self.assertEqual(visited, ["N0001"])
        self.assertEqual(self.log.metadata["v4_usage"]["model_calls"], 2)

    def test_external_discoveries_are_never_selected_or_executed(self):
        visited = []
        def callback(context):
            visited.append(context["node"]["id"])
            return self.command(context, [surface("https://outside.test/q")])
        result = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result, ("completed", "tree_exhausted"))
        self.assertEqual(visited, ["N0001"])
        state = TreeStore(self.log).rebuild()
        self.assertEqual(state["nodes"]["N0002"]["exploration_state"], "blocked")

    def test_root_leaves_a_recorded_model_reserve_for_deeper_nodes(self):
        self.log.update(budgets={**self.log.metadata["budgets"], "model_calls": 4})
        TreeStore(self.log).initialize(self.log.metadata["target"])
        gate = BudgetGate(self.log, stop_event=threading.Event())
        payload = {"model": "mock:model", "messages": [{}, {"content": json.dumps({"node": {"id": "N0001"}})}]}
        for index in range(3):
            gate.before_model(f"CALL-{index + 1:04d}", payload)
        with self.assertRaises(V4BudgetStop) as caught:
            gate.before_model("CALL-0004", payload)
        self.assertEqual(caught.exception.reason, "node_limit")
        self.assertEqual(self.log.metadata["v4_budget_policy"]["deep_reserve_calls"], 1)

    def test_actual_remote_cost_and_ambiguous_cost_reservations_both_stop_admission(self):
        TreeStore(self.log).initialize(self.log.metadata["target"])
        self.log.update(v4_settings={"max_cost_usd": .01, "remote_call_reserve_usd": .006})
        gate = BudgetGate(self.log, stop_event=threading.Event())
        payload = {"model": "openrouter:test/model", "messages": [{}, {"content": json.dumps({"node": {"id": "N0002"}})}]}
        gate.before_model("CALL-0001", payload)
        self.assertIsNone(gate.usage()["cost_usd"])
        with self.assertRaises(V4BudgetStop):
            gate.before_model("CALL-0002", payload)
        self.log.append("model_response", {"call_id": "CALL-0001", "cost": .02})
        self.assertEqual(gate.usage()["cost_usd"], .02)
        with self.assertRaises(V4BudgetStop) as caught:
            gate.check()
        self.assertEqual(caught.exception.reason, "cost_budget")

    def test_exhausted_tool_budget_does_not_spend_another_model_call(self):
        self.log.update(budgets={**self.log.metadata["budgets"], "tool_calls": 1})
        calls = []
        def callback(context):
            calls.append(context)
            return {**self.command(context), "action": "write_file", "path": "work/prep.py", "content": "pass", "command": ""}
        result = V4Runner(self.log, Runtime(self.log), transport=self.transport(callback), scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result[1], "call_budget")
        self.assertEqual(len(calls), 1)

    def test_lab_oracle_is_wired_to_attempt_and_can_confirm_a_known_positive(self):
        from test_v4_oracle import OracleTests
        fixture = OracleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.log.update(**{key: value for key, value in self.log.metadata.items() if key not in (
            "run_id", "created_at", "target", "initial_lab_status", "initial_lab_probe", "v4_settings")}, v4_settings={"exclusive_lab": True})
        runtime = Runtime(fixture.log)
        original = runtime.run
        def execute(*args, **kwargs):
            output = original(*args, **kwargs)
            if runtime.calls == 2:
                fixture.solved = True
            return output
        runtime.run = execute
        def callback(context):
            self.assertNotIn("dbSchemaChallenge", json.dumps(context))
            discoveries = [surface("http://127.0.0.1:3000/rest/products/search", "candidate")] if context["node"]["id"] == "N0001" else []
            return self.command(context, discoveries)
        result = V4Runner(fixture.log, runtime, transport=self.transport(callback), oracle=fixture.oracle, scorer_adapter=FakeAdapter()).run()
        self.assertEqual(result, ("completed", "tree_exhausted"))
        state = TreeStore(fixture.log).rebuild()
        self.assertEqual(state["nodes"]["N0002"]["verification_state"], "confirmed")
        self.assertEqual(fixture.log.metadata["v4_usage"]["oracle_calls"], 5)


if __name__ == "__main__":
    unittest.main()

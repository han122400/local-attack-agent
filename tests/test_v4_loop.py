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
            fixture = json.loads(payload["command"])
            (self.runtime_dir / "work" / "node-results" / (fixture["attempt_id"] + ".json")).write_text(json.dumps(fixture), encoding="utf-8")
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
                  "evidence_refs": [], "next_observation": ""}
        return {"action": action, "plan": "inspect", "reason": "investigate current node", "path": "", "content": "",
                "code": "", "command": json.dumps(result) if action == "run_command" else ""}

    def test_greedy_descends_current_siblings_then_backtracks_and_exhausts(self):
        visited = []
        def callback(context):
            node = context["node"]["id"]
            visited.append(node)
            self.assertNotIn("dbSchemaChallenge", json.dumps(context))
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

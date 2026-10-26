"""Policy evaluation uses an independent completion oracle, not stdout or finish counts."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.harness_policy_eval import CASES, evaluate, main, profile_events
from planning import Command
from session_log import SessionLog


class PolicyEvaluationTests(unittest.TestCase):
    def reader(self, context):
        current = context["current"]
        if current["complete"]:
            return Command("finish", "done", "case reviewed")
        return Command("read_file", "read fixture", "follow next reference", path=current["next_file"])

    def test_three_step_rule_can_abandon_a_solvable_four_step_case(self):
        original = evaluate(CASES["four_step_setup"], "original", self.reader)
        limited = evaluate(CASES["four_step_setup"], "stall3", self.reader)
        self.assertEqual(original["completed_tasks"], 1)
        self.assertEqual(limited["completed_tasks"], 0)
        self.assertEqual(limited["abandoned_tasks"], 1)
        self.assertFalse(limited["success"])

    def test_finish_semantics_can_reach_a_second_case_but_global_finish_cannot(self):
        original = evaluate(CASES["two_cases"], "original", self.reader)
        scoped = evaluate(CASES["two_cases"], "case_finish", self.reader)
        self.assertEqual(original["completed_tasks"], 1)
        self.assertEqual(scoped["completed_tasks"], 2)
        self.assertTrue(scoped["success"])

    def test_finish_without_reading_any_fixture_does_not_earn_completion(self):
        for policy in ("original", "stall3", "case_finish", "combined"):
            with self.subTest(policy=policy):
                row = evaluate(CASES["two_cases"], policy,
                               lambda context: Command("finish", "done", "I completed both tasks"))
                self.assertEqual(row["completed_tasks"], 0)
                self.assertFalse(row["success"])

    def test_finishing_first_case_is_not_a_final_report_for_the_second(self):
        row = evaluate(CASES["two_cases"], "case_finish", self.reader, max_calls=3)
        self.assertEqual(row["completed_tasks"], 2)
        self.assertFalse(row["success"], "The second case has not been finished/reported yet")

    def test_stalled_case_can_release_budget_for_another_case(self):
        def repeating(context):
            if context["current"]["id"] == "A":
                return Command("read_file", "retry", "same missing file", path="work/missing.json")
            return self.reader(context)
        original = evaluate(CASES["two_cases"], "original", repeating, max_calls=8)
        limited = evaluate(CASES["two_cases"], "stall3", repeating, max_calls=8)
        self.assertEqual(original["completed_tasks"], 0)
        self.assertEqual(limited["completed_tasks"], 1)
        self.assertEqual(limited["abandoned_tasks"], 1)

    def test_cli_saves_real_harness_metrics_without_a_live_model(self):
        received = []
        def transport(payload):
            received.append(payload["options"])
            command = self.reader(json.loads(payload["messages"][1]["content"]))
            answer = {**command.runtime_payload(), "plan": command.plan, "reason": command.reason}
            yield {"message": {"thinking": "brief"}}
            yield {"message": {"content": json.dumps(answer)}, "done": True, "done_reason": "stop", "eval_count": 12,
                   "eval_duration": 2_000_000_000, "prompt_eval_duration": 1_000_000_000, "load_duration": 500_000_000}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            arguments = ["eval", "--model", "fixture", "--cases", "two_cases", "--policies", "case_finish",
                         "--max-calls", "4", "--output", str(output)]
            with patch.object(sys, "argv", arguments), patch("harness._model_stream", transport), contextlib.redirect_stdout(io.StringIO()):
                main()
            row = json.loads(output.read_text())["results"][0]
            self.assertEqual(row["completed_tasks"], 2)
            self.assertEqual(row["provider_calls"], 4)
            self.assertEqual(row["generated_tokens"], 48)
            self.assertEqual(received[0]["num_ctx"], 8192)
            self.assertEqual(received[0]["num_predict"], 4096)
            self.assertEqual(row["valid_commands"], 4)
            self.assertEqual(row["valid_command_rate"], 1)
            self.assertEqual(row["thinking_chars"], 20)
            self.assertEqual(row["completed_generation_tps"], 6)
            self.assertEqual(row["load_seconds"], 2)
            self.assertIsNotNone(row["mean_time_to_content_seconds"])

    def test_profile_keeps_production_output_retry_and_counts_its_cost(self):
        received = []
        def transport(payload):
            received.append(payload["options"])
            if len(received) == 1:
                yield {"message": {"thinking": "budget exhausted"}, "done": True,
                       "done_reason": "length", "eval_count": 4096}
                return
            command = self.reader(json.loads(payload["messages"][1]["content"]))
            answer = {**command.runtime_payload(), "plan": command.plan, "reason": command.reason}
            yield {"message": {"content": json.dumps(answer)}, "done": True, "done_reason": "stop", "eval_count": 12}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            arguments = ["eval", "--model", "fixture", "--cases", "two_cases", "--policies", "case_finish",
                         "--output", str(output)]
            with patch.object(sys, "argv", arguments), patch("harness._model_stream", transport), contextlib.redirect_stdout(io.StringIO()):
                main()
            row = json.loads(output.read_text())["results"][0]
            self.assertEqual(row["completed_tasks"], 2)
            self.assertEqual(row["provider_calls"], 5)
            self.assertEqual(received[1]["num_predict"], 8192)
            self.assertEqual(row["output_limit_count"], 1)
            self.assertEqual(row["valid_command_rate"], 0.8)

    def test_partial_thinking_and_missing_usage_are_not_reported_as_zero_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), "a" * 32, {})
            try:
                log.append("model_request", {"call_id": "CALL-1"})
                log.append("model_delta", {"call_id": "CALL-1", "channel": "thinking", "text": "partial"})
                metrics = profile_events(log)
                self.assertEqual(metrics["thinking_chars"], 7)
                self.assertEqual(metrics["valid_commands"], 0)
                self.assertIsNone(metrics["generated_tokens"])
                self.assertIsNone(metrics["load_seconds"])
                log.append("model_response", {"call_id": "CALL-1", "output": "{}"})
                self.assertIsNone(profile_events(log)["generated_tokens"])
            finally:
                log.close()


if __name__ == "__main__":
    unittest.main()

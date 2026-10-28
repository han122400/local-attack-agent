import json
import tempfile
import unittest
from pathlib import Path

import a5_benchmark
from a5_benchmark import (CONFIG_PATH, config_digest, final_observation,
                          summarize_run, validate_config)
from planning import load_prompt


class A5BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_protocol_is_explicit_and_local_model_is_required(self):
        validate_config(self.config)
        self.assertEqual(64, len(config_digest(self.config)))
        changed = {**self.config, "execution_model": "openrouter:example/model"}
        with self.assertRaisesRegex(ValueError, "local Ollama"):
            validate_config(changed)

    def test_final_observation_uses_post_initial_reset_identity(self):
        entry = {"baseline_identity": {"started_at": "manual-reset"},
                 "effective_baseline_identity": {"started_at": "controller-reset"},
                 "reset_observations": [{"phase": "preparing", "identity": {"started_at": "manual-reset"}},
                                        {"phase": "resetting", "identity": {"started_at": "controller-reset"}}]}
        self.assertEqual("controller-reset", final_observation(entry)["baseline_identity"]["started_at"])

    def test_v3_observation_is_not_a_confirmed_vulnerability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "session.json").write_text(json.dumps({"run_id": "a" * 32, "experiment_mode": "v3",
                "prompt_version": "v3", "phase": "finished", "started_at": 10, "finished_at": 25,
                "findings": [{"evidence": True}, {"evidence": True, "repeat_of": 1}],
                "report": "confirmed observation"}), encoding="utf-8")
            events = [
                {"kind": "model_request", "call_id": "CALL-0001", "system_prompt_sha256": load_prompt("v3")[1]},
                {"kind": "model_response", "call_id": "CALL-0001", "prompt_eval_count": 100, "eval_count": 20},
                {"kind": "exec_started"},
            ]
            (root / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
            observation = {"challenge": {"solved": False}, "identity": {"container_id": "one"},
                           "baseline_identity": {"container_id": "one"}}
            result = summarize_run(root, "v3", observation)
            self.assertIsNone(result["confirmed_by_harness"])
            self.assertEqual(2, result["reported_evidence_observations"])
            self.assertEqual(1, result["repeated_observation_digests"])
            self.assertEqual(1, result["model_calls"])
            self.assertEqual(1, result["tool_calls"])
            self.assertEqual(0.0, result["total_api_cost_usd"])
            self.assertFalse(result["challenge_solved_before_cleanup"])
            self.assertTrue(result["same_lab_identity"])

    def test_saved_run_uses_its_recorded_prompt_hash_for_historical_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_hash = "a" * 64
            (root / "session.json").write_text(json.dumps({"run_id": "a" * 32, "experiment_mode": "v3",
                "prompt_version": "v3", "phase": "finished", "started_at": 10, "finished_at": 20}), encoding="utf-8")
            events = [{"kind": "model_request", "call_id": "CALL-0001", "system_prompt_sha256": old_hash},
                      {"kind": "model_response", "call_id": "CALL-0001", "prompt_eval_count": 1, "eval_count": 1}]
            (root / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
            self.assertEqual(old_hash, summarize_run(root, "v3", None)["prompt_sha256"])
            with self.assertRaisesRegex(ValueError, "different prompt"):
                summarize_run(root, "v3", None, expected_prompt_sha256="b" * 64)

    def test_resumed_pair_rejects_prompt_change_after_first_mode(self):
        self.assertTrue(hasattr(a5_benchmark, "check_prompt_pins"))
        check_prompt_pins = a5_benchmark.check_prompt_pins
        pins = {"v3": "a" * 64, "v4": "b" * 64}
        self.assertEqual(pins, check_prompt_pins({"runs": {}}, pins))
        prior = {"runs": {"v3": {"metrics": {"prompt_sha256": "a" * 64}}}}
        self.assertEqual(pins, check_prompt_pins(prior, pins))
        with self.assertRaisesRegex(ValueError, "prompt changed"):
            check_prompt_pins(prior, {**pins, "v3": "c" * 64})
        with self.assertRaisesRegex(ValueError, "prompt changed"):
            check_prompt_pins({"prompt_pins": pins, "runs": {}}, {**pins, "v4": "c" * 64})

    def test_v4_confirmation_is_checked_against_pre_cleanup_lab_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "session.json").write_text(json.dumps({"run_id": "b" * 32, "experiment_mode": "v4",
                "prompt_version": "v4", "phase": "finished", "started_at": 10, "finished_at": 30}), encoding="utf-8")
            events = [
                {"kind": "model_request", "call_id": "CALL-0001", "system_prompt_sha256": load_prompt("v4")[1]},
                {"kind": "model_response", "call_id": "CALL-0001", "prompt_eval_count": 120, "eval_count": 30},
                {"kind": "node_result_imported", "payload": {"result": {"discovered": [{"kind": "surface"}, {"kind": "surface"}]} }},
                {"kind": "exec_started"},
            ]
            (root / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
            (root / "tree.json").write_text(json.dumps({"node_ids": ["N0001", "N0002"]}), encoding="utf-8")
            (root / "report.json").write_text(json.dumps({"status": "complete", "stats": {"confirmed": 1,
                "candidates": 1, "tested_candidates": 1, "inconclusive": 0, "unscored": 0},
                "usage": {"scoring_calls": 1, "cost_usd": 0.0001, "unknown_cost_requests": 0}}), encoding="utf-8")
            observation = {"challenge": {"solved": False}, "identity": {"container_id": "one"},
                           "baseline_identity": {"container_id": "one"}}
            result = summarize_run(root, "v4", observation)
            self.assertEqual(1, result["confirmed_by_harness"])
            self.assertTrue(result["known_case_false_positive"])
            self.assertFalse(result["known_case_unconfirmed_solve"])
            self.assertEqual(1, result["repeated_discoveries"])


if __name__ == "__main__":
    unittest.main()

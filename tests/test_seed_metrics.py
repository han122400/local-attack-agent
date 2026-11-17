import unittest

from engine.contracts import dedup_key
from engine.seed_metrics import seed_metrics


def discovery(url, kind="surface"):
    return {"kind": kind, "relation": "discovered",
            "locator": {"url": url, "method": "GET", "input": "q", "auth_context": "anonymous"},
            "hypothesis": {"type": "sqli", "statement": "test"} if kind == "candidate" else None}


class SeedMetricsTests(unittest.TestCase):
    def comparison_records(self, first_hashes, second_hashes):
        shared = {"experiment_mode": "v4", "prompt_version": "v4", "target": "https://example.test/"}
        def record(mode, hashes):
            return {"metadata": {**shared, "seed_settings": {"start_mode": mode},
                    "seed_source": {"run_id": "a" * 32, "last_event_seq": 7}},
                    "events": [{"seq": index + 1, "kind": "model_request", "system_prompt_sha256": value}
                               for index, value in enumerate(hashes)],
                    "state": {"nodes": {}, "attempts": {}}}
        return record("fresh", first_hashes), record("seeded", second_hashes)

    def test_comparison_rejects_different_actual_prompts_with_same_version(self):
        from engine.seed_metrics import compare_seed_runs
        with self.assertRaisesRegex(ValueError, "prompt"):
            compare_seed_runs(*self.comparison_records(["1" * 64], ["2" * 64]))

    def test_comparison_rejects_inconsistent_missing_or_invalid_prompt_pins(self):
        from engine.seed_metrics import compare_seed_runs
        for hashes in (["1" * 64, "2" * 64], [None], ["x" * 64], ["short"]):
            with self.subTest(hashes=hashes), self.assertRaisesRegex(ValueError, "prompt"):
                compare_seed_runs(*self.comparison_records(hashes, ["1" * 64]))

    def test_comparison_records_observed_prompt_and_does_not_certify_no_call_run(self):
        from engine.seed_metrics import compare_seed_runs
        result = compare_seed_runs(*self.comparison_records(["1" * 64], ["1" * 64]))
        self.assertTrue(result["prompt_equivalence_verified"])
        self.assertEqual(result["system_prompt_sha256"], "1" * 64)
        result = compare_seed_runs(*self.comparison_records([], ["1" * 64]))
        self.assertFalse(result["prompt_equivalence_verified"])
        self.assertIsNone(result["runs"][0]["system_prompt_sha256"])
        self.assertIn("no model requests", result["prompt_equivalence_note"])

    def test_coverage_requires_actual_execution_not_reserved_attempt(self):
        state = {"nodes": {"N0002": {"kind": "surface", "seeded": True, "attempts": ["A0001"]}},
                 "attempts": {"A0001": {"node_id": "N0002", "call_id": "CALL-1", "status": "failed", "reason": "not_executed"}}}
        result = seed_metrics([], state, {"seed_settings": {"start_mode": "seeded"}})
        self.assertEqual(result["coverage_reuse"], {"used": 0, "total": 1, "ratio": 0.0})
        result = seed_metrics([{"seq": 1, "kind": "exec_started", "call_id": "CALL-1"}], state,
                              {"seed_settings": {"start_mode": "seeded"}})
        self.assertEqual(result["coverage_reuse"]["used"], 1)

    def test_comparison_uses_identical_imported_seed_keys_for_fresh_and_seeded(self):
        from engine.seed_metrics import compare_seed_runs
        surface = discovery("https://example.test/a")
        shared = {"target": "https://example.test/", "model": "m", "model_settings": {},
                  "scorer_provider": "local", "scorer_model": "s", "scorer_settings": {},
                  "v4_settings": {}, "budgets": {}, "prompt_version": "v4", "experiment_mode": "v4"}
        event = {"seq": 1, "kind": "node_result_imported", "payload": {
            "execution_status": "succeeded", "result": {"discovered": [surface]}}}
        fresh = {"metadata": {**shared, "seed_settings": {"start_mode": "fresh"}},
                 "events": [event], "state": {"nodes": {}, "attempts": {}}}
        seeded = {"metadata": {**shared, "seed_settings": {"start_mode": "seeded", "reverify": "lazy"},
                               "seed_source": {"run_id": "a" * 32, "last_event_seq": 7}},
                  "events": [event], "state": {"nodes": {"N0002": {"kind": "surface", "seeded": True,
                           "dedup_key": dedup_key(surface), "attempts": []}}, "attempts": {}}}
        comparison = compare_seed_runs(fresh, seeded)
        self.assertEqual(comparison["fresh"]["redundant_rediscovery"]["ratio"], 1.0)
        self.assertEqual(comparison["seeded"]["redundant_rediscovery"]["ratio"], 1.0)
        with self.assertRaises(ValueError):
            compare_seed_runs({**fresh, "metadata": {**fresh["metadata"], "model": "other"}}, seeded)

    def test_seeded_comparison_rejects_different_initial_observations(self):
        from engine.seed_metrics import compare_seed_runs
        surface = discovery("https://example.test/a")
        def record(run_id, status):
            events = [
                {"seq": 1, "run_id": run_id, "kind": "tree_initialized", "tree_schema": 1,
                 "payload": {"target": "https://example.test/"}},
                {"seq": 2, "run_id": run_id, "kind": "node_discovered", "tree_schema": 1,
                 "payload": {"parent_id": "N0001", "discovery": surface}},
                {"seq": 3, "run_id": run_id, "kind": "node_seeded", "tree_schema": 1,
                 "payload": {"node_id": "N0002", "source_run_id": "a" * 32, "source_node_id": "N0002",
                             "observations": {"response_status": status}, "reused": False}},
                {"seq": 4, "run_id": run_id, "kind": "seed_completed", "counts": {}}]
            from engine.tree import replay_tree
            return {"metadata": {"run_id": run_id, "experiment_mode": "v4", "target": "https://example.test/",
                    "seed_settings": {"start_mode": "seeded"}, "seed_source": {"run_id": "a" * 32, "last_event_seq": 7}},
                    "events": events, "state": replay_tree(events, run_id)}
        with self.assertRaisesRegex(ValueError, "seed state"):
            compare_seed_runs(record("b" * 32, 200), record("c" * 32, 404))

    def test_fresh_rediscovery_uses_explicit_seed_baseline(self):
        surface = discovery("https://example.test/a")
        events = [{"seq": 1, "kind": "node_result_imported", "payload": {
            "execution_status": "succeeded", "result": {"discovered": [surface]}}}]
        result = seed_metrics(events, {"nodes": {}, "attempts": {}}, {}, baseline_keys={dedup_key(surface)})
        self.assertEqual(result["redundant_rediscovery"], {"duplicates": 1, "discoveries": 1, "ratio": 1.0})

    def test_candidate_test_budget_only_counts_calls_with_bound_execution(self):
        state = {"nodes": {"N0002": {"kind": "candidate"}}, "attempts": {
            "A0001": {"node_id": "N0002", "call_id": "CALL-1"},
            "A0002": {"node_id": "N0002", "call_id": "CALL-2"}}}
        events = [{"seq": 1, "kind": "v4_execution_reserved", "call_id": "CALL-1"},
                  {"seq": 2, "kind": "v4_execution_reserved", "call_id": "CALL-2"},
                  {"seq": 3, "kind": "scorer_request_started"},
                  {"seq": 4, "kind": "exec_started", "call_id": "CALL-1", "at": 1}]
        result = seed_metrics(events, state, {})
        self.assertEqual(result["candidate_test_budget"]["model_calls"], 1)
        self.assertAlmostEqual(result["candidate_test_budget"]["model_call_ratio"], 1 / 3)
        self.assertIsNone(result["budget_to_attack_ratio"])

    def test_attack_budget_requires_explicit_reviewed_executions(self):
        events = [{"seq": 1, "kind": "v4_execution_reserved", "call_id": "CALL-1"},
                  {"seq": 2, "kind": "scorer_request_started"},
                  {"seq": 3, "kind": "exec_started", "call_id": "CALL-1"}]
        state = {"nodes": {}, "attempts": {}}
        result = seed_metrics(events, state, {}, attack_call_ids={"CALL-1"})
        self.assertEqual(result["budget_to_attack_ratio"], 0.5)
        self.assertEqual(result["attack_budget_classification"], "operator_reviewed_call_ids")
        with self.assertRaises(ValueError):
            seed_metrics(events, state, {}, attack_call_ids={"unrecorded"})

    def test_seed_metrics_count_current_events_without_treating_prior_as_tests(self):
        surface, candidate = discovery("https://example.test/a"), discovery("https://example.test/b", "candidate")
        state = {"nodes": {
            "N0001": {"kind": "root", "parent_id": None},
            "N0002": {"kind": "surface", "seeded": True, "parent_id": "N0001", "dedup_key": dedup_key(surface), "attempts": []},
            "N0003": {"kind": "candidate", "seeded": True, "parent_id": "N0002", "dedup_key": dedup_key(candidate), "attempts": ["A0001"]},
            "N0004": {"kind": "surface", "parent_id": "N0002", "attempts": []},
        }, "attempts": {"A0001": {"node_id": "N0003", "call_id": "CALL-1"}}}
        events = [
            {"seq": 1, "kind": "v4_execution_reserved", "call_id": "CALL-1"},
            {"seq": 2, "kind": "scorer_request_started"},
            {"seq": 3, "kind": "node_attempt_started", "payload": {"node_id": "N0003", "attempt_id": "A0001"}},
            {"seq": 4, "kind": "exec_started", "call_id": "CALL-1", "at": 104},
            {"seq": 5, "kind": "node_result_imported", "payload": {"execution_status": "succeeded", "result": {"discovered": [surface, candidate]}}},
        ]
        result = seed_metrics(events, state, {"started_at": 100, "seed_settings": {"start_mode": "seeded"}})
        self.assertEqual(result["time_to_first_candidate_test_seconds"], 4)
        self.assertEqual(result["model_calls_to_first_candidate_test"], 2)
        self.assertEqual(result["coverage_reuse"], {"used": 1, "total": 2, "ratio": 0.5})
        self.assertEqual(result["redundant_rediscovery"], {"duplicates": 2, "discoveries": 2, "ratio": 1.0})
        self.assertIsNone(result["budget_to_attack_ratio"])
        self.assertIsNone(result["source_creation_cost_usd"])

    def test_fresh_and_no_attempts_have_explicit_unknown_values(self):
        result = seed_metrics([], {"nodes": {}, "attempts": {}}, {})
        self.assertIsNone(result["time_to_first_candidate_test_seconds"])
        self.assertIsNone(result["model_calls_to_first_candidate_test"])
        self.assertIsNone(result["coverage_reuse"]["ratio"])
        self.assertIsNone(result["redundant_rediscovery"]["ratio"])


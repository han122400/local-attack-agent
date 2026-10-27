"""Durable scoring behavior; all inference here is fake, no target traffic."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from decision_provider import ProviderError
from risk_rubric import validate_answer
from scorer_session import RiskScorer
from session_log import SessionLog, LogFailure
from v4_contracts import ContractError
from v4_tree import TreeStore, replay_tree
from test_scorer_contract import answer
from test_v4_tree import surface


PIN = {"id": "typesafe/jev-1.14", "canonical_slug": "typesafe/jev-1.14-20261014",
       "catalog_retrieved_at": "2026-10-27T00:00:00+00:00", "output_modalities": ["decisions"],
       "pricing": {"prompt": .000000042, "completion": 0, "request": 0}}


class FakeAdapter:
    def __init__(self, behavior=None):
        self.calls, self.resolutions = [], 0
        self.behavior = behavior

    def resolve(self, model, **kwargs):
        self.resolutions += 1
        return copy.deepcopy(PIN)

    def evaluate(self, pin, parent, siblings, node_ids, **kwargs):
        self.calls.append(list(node_ids))
        if self.behavior:
            return self.behavior(node_ids)
        return good(node_ids)


def good(ids):
    return {"model": PIN["canonical_slug"], "scores": {key: validate_answer(answer()) for key in ids},
            "node_errors": {}, "diagnostics": {},
            "usage": {"input_tokens": 100, "output_tokens": 5, "cost": .0000042}}


class ScorerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), "a" * 32, {"experiment_mode": "v4",
            "scorer_provider": "openrouter", "scorer_model": PIN["id"], "scorer_settings": {}})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize("https://example.test/")
        self.children = [self.tree.discover(self.root, surface(f"https://example.test/api/{i}")) for i in range(3)]

    def scorer(self, adapter=None, **settings):
        self.log.update(scorer_settings=settings)
        return RiskScorer(self.tree, adapter=adapter or FakeAdapter())

    def test_all_siblings_batched_before_ordering_and_cache_survives_replay(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter, batch_size=2)
        result = scorer.score_children(self.root)
        self.assertTrue(result["ready"])
        self.assertEqual(adapter.calls, [self.children[:2], self.children[2:]])
        self.assertEqual(result["ordered_node_ids"], self.children)
        self.assertEqual(scorer.usage()["requests"], 2)
        self.assertEqual(scorer.usage()["questions"], 3)
        self.assertEqual(scorer.usage()["cost_usd"], .0000084)
        for key in self.children:
            self.assertEqual(self.tree.state["nodes"][key]["risk"]["score"], 75)
            self.assertEqual(self.tree.state["nodes"][key]["verification_state"], "untested")
        restored = TreeStore(self.log)
        restored.rebuild()
        before = self.log.latest_seq
        again = RiskScorer(restored, adapter=adapter).score_children(self.root)
        self.assertEqual(again["ordered_node_ids"], self.children)
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(adapter.resolutions, 1)
        self.assertEqual(self.log.latest_seq, before)
        self.assertTrue((self.log.run_dir / "scorer.json").is_file())

    def test_visits_and_completion_do_not_invalidate_but_facts_and_new_siblings_do(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter, max_calls_per_parent=10)
        scorer.score_children(self.root)
        self.tree.select_node(self.children[0])
        self.tree.set_node_state(self.children[0], "done")
        scorer.score_children(self.root)
        self.assertEqual(len(adapter.calls), 1)
        self.tree.set_observations(self.children[1], {"query_behavior": "error"})
        self.assertEqual(self.tree.state["nodes"][self.children[2]]["risk"]["status"], "pending")
        scorer.score_children(self.root)
        self.assertEqual(adapter.calls[-1], self.children[1:])
        self.tree.set_observations(self.children[1], {"query_behavior": "error", "body": "PRIVATE"})
        scorer.score_children(self.root)
        self.assertEqual(len(adapter.calls), 2)
        new_id = self.tree.discover(self.root, surface("https://example.test/upload"))
        scorer.score_children(self.root)
        self.assertEqual(adapter.calls[-1], [*self.children[1:], new_id])

    def test_node_errors_retry_only_failed_nodes_then_degrade_in_stable_order(self):
        def behavior(ids):
            result = good(ids)
            if self.children[0] in ids:
                result["scores"].pop(self.children[0])
                result["node_errors"][self.children[0]] = "invalid_answer"
            return result
        adapter = FakeAdapter(behavior)
        scorer = self.scorer(adapter)
        result = scorer.score_children(self.root)
        self.assertTrue(result["ready"])
        self.assertEqual(adapter.calls, [self.children, self.children[:1]])
        self.assertEqual(result["ordered_node_ids"], [*self.children[1:], self.children[0]])
        self.assertEqual(result["unscored"], {self.children[0]: "invalid_answer"})
        scorer.score_children(self.root)
        self.assertEqual(len(adapter.calls), 2)

    def test_cached_context_restores_risk_snapshot_after_facts_revert(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter)
        scorer.score_children(self.root)
        self.tree.set_observations(self.children[0], {"query_behavior": "error"})
        scorer.score_children(self.root)
        self.tree.set_observations(self.children[0], {})
        result = scorer.score_children(self.root)
        self.assertTrue(result["ready"])
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(self.tree.state["nodes"][self.children[0]]["risk"]["score"], 75)

    def test_all_node_errors_are_not_provider_outage(self):
        def behavior(ids):
            result = good(ids)
            result.update(scores={}, node_errors={key: "invalid_answer" for key in ids})
            return result
        scorer = self.scorer(FakeAdapter(behavior), max_retries=0)
        result = scorer.score_children(self.root)
        self.assertTrue(result["ready"])
        self.assertEqual(result["ordered_node_ids"], self.children)
        self.assertEqual(len(result["unscored"]), 3)

    def test_parent_limit_is_cumulative_across_context_changes(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter, batch_size=1, max_calls_per_parent=1)
        result = scorer.score_children(self.root)
        self.assertTrue(result["ready"])
        self.assertEqual(result["unscored"], {key: "parent_limit" for key in self.children[1:]})
        self.tree.set_observations(self.children[0], {"query_behavior": "error"})
        result = scorer.score_children(self.root)
        self.assertEqual(result["unscored"], {key: "parent_limit" for key in self.children})
        self.assertEqual(len(adapter.calls), 1)

    def test_global_budget_does_not_expose_partially_scored_batch_as_ready(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter, batch_size=1, max_calls=1, max_calls_per_parent=1)
        result = scorer.score_children(self.root)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stop_reason"], "call_budget")
        self.assertEqual(result["ordered_node_ids"], [])
        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(scorer.score_children(self.root)["ready"])
        self.assertEqual(len(adapter.calls), 1)

    def test_provider_failure_retries_bounded_and_unknown_cost_is_not_zero(self):
        def fail(ids):
            raise ProviderError("timeout", retryable=True)
        adapter = FakeAdapter(fail)
        scorer = self.scorer(adapter)
        result = scorer.score_children(self.root)
        self.assertEqual(result["stop_reason"], "scorer_unavailable")
        self.assertEqual(len(adapter.calls), 2)
        usage = scorer.usage()
        self.assertEqual(usage["unknown_cost_requests"], 2)
        self.assertIsNone(usage["cost_usd"])
        self.assertGreater(usage["reserved_cost_usd"], 0)
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "scorer_unavailable")
        self.assertEqual(len(adapter.calls), 2)

    def test_non_retryable_provider_error_and_cost_budget(self):
        def fail(ids):
            raise ProviderError("http_401")
        adapter = FakeAdapter(fail)
        scorer = self.scorer(adapter)
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "scorer_unavailable")
        self.assertEqual(len(adapter.calls), 1)

    def test_input_limit_never_sends_partial_or_unredacted_context(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter, max_input_bytes=100)
        result = scorer.score_children(self.root)
        self.assertTrue(result["ready"])
        self.assertEqual(result["unscored"], {key: "input_limit" for key in self.children})
        self.assertFalse(adapter.calls)

    def test_pending_request_replay_does_not_reissue_network_call(self):
        # Interrupt after durable reservation, before any finish event.
        def interrupted(ids):
            raise KeyboardInterrupt()
        adapter = FakeAdapter(interrupted)
        scorer = self.scorer(adapter)
        with self.assertRaises(KeyboardInterrupt):
            scorer.score_children(self.root)
        restored = TreeStore(self.log)
        restored.rebuild()
        result = RiskScorer(restored, adapter=adapter).score_children(self.root)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stop_reason"], "scorer_unavailable")
        self.assertEqual(len(adapter.calls), 1)

    def test_snapshot_failure_after_reservation_blocks_network_and_replay_retry(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter)
        scorer.configure()
        with patch("v4_tree.atomic_write_json", side_effect=OSError("disk unavailable")), self.assertRaises(LogFailure):
            scorer.score_children(self.root)
        self.assertFalse(adapter.calls)
        self.tree.rebuild()
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "scorer_unavailable")
        self.assertEqual(scorer.usage()["pending_requests"], 1)
        self.assertFalse(adapter.calls)

    def test_observation_change_while_request_inflight_never_applies_stale_score(self):
        def change(ids):
            self.tree.set_observations(self.children[0], {"data_exposure": "private"})
            return good(ids)
        scorer = self.scorer(FakeAdapter(change))
        result = scorer.score_children(self.root)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stop_reason"], "context_changed")
        self.assertIsNone(self.tree.state["nodes"][self.children[0]]["risk"]["score"])

    def test_config_is_pinned_and_replay_rejects_unknown_scorer_event(self):
        scorer = self.scorer()
        scorer.score_children(self.root)
        self.log.update(scorer_model="other/model")
        with self.assertRaises(ContractError):
            RiskScorer(self.tree, adapter=FakeAdapter()).score_children(self.root)
        event = {"kind": "scorer_unknown", "run_id": self.log.run_id, "seq": self.log.latest_seq + 1}
        with self.assertRaises(LogFailure):
            replay_tree([*self.log.read_events(), event], self.log.run_id)

    def test_cost_reservation_stops_before_request(self):
        adapter = FakeAdapter()
        scorer = self.scorer(adapter, max_cost_usd=.000001)
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "cost_budget")
        self.assertEqual(len(adapter.calls), 0)
        self.assertEqual(scorer.usage()["requests"], 0)

    def test_actual_cost_over_reservation_stops_and_is_not_capped_in_usage(self):
        def expensive(ids):
            result = good(ids)
            result["usage"]["cost"] = .02
            return result
        scorer = self.scorer(FakeAdapter(expensive))
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "cost_budget")
        self.assertEqual(scorer.usage()["cost_usd"], .02)

    def test_time_budget_includes_retry_time(self):
        now = [0.0]
        def slow(ids):
            now[0] += 3
            raise ProviderError("timeout", retryable=True)
        scorer = self.scorer(FakeAdapter(slow), time_budget_seconds=2)
        scorer.clock = lambda: now[0]
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "time_budget")
        self.assertEqual(scorer.usage()["elapsed_seconds"], 3)

    def test_failed_catalog_is_time_bounded_and_its_elapsed_time_survives_replay(self):
        now, observed = [0.0], []
        adapter = FakeAdapter()
        def fail_catalog(model, *, timeout_seconds):
            observed.append(timeout_seconds)
            now[0] += 1
            raise ProviderError("timeout", retryable=True)
        adapter.resolve = fail_catalog
        scorer = self.scorer(adapter, time_budget_seconds=1, timeout_seconds=20)
        scorer.clock = lambda: now[0]
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "scorer_unavailable")
        self.assertEqual(observed, [1])
        self.assertEqual(scorer.usage()["elapsed_seconds"], 1)
        self.assertFalse(adapter.calls)
        self.tree.rebuild()
        self.assertEqual(scorer.usage()["elapsed_seconds"], 1)

    def test_decoding_failure_finishes_reserved_request_and_retains_unknown_cost(self):
        import httpx2 as httpx
        from decision_provider import OpenRouterDecisions
        from test_scorer_provider import CATALOG
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"corrupt")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        scorer = self.scorer(OpenRouterDecisions(api_key="TEST_ONLY", client=client))
        self.assertEqual(scorer.score_children(self.root)["stop_reason"], "scorer_unavailable")
        accounting = scorer.usage()
        self.assertEqual(accounting["completed_requests"], 1)
        self.assertEqual(accounting["pending_requests"], 0)
        self.assertEqual(accounting["unknown_cost_requests"], 1)
        self.assertIsNone(accounting["cost_usd"])
        request = next(iter(self.tree.state["scorer"]["requests"].values()))
        self.assertEqual(request["finish"]["error"]["code"], "invalid_response_encoding")

    def test_replay_rejects_invalid_pin_as_log_failure(self):
        scorer = self.scorer()
        scorer.score_children(self.root)
        events = copy.deepcopy(self.log.read_events())
        event = next(item for item in events if item["kind"] == "scorer_configured")
        event["payload"]["model_pin"]["pricing"]["prompt"] = -1
        with self.assertRaises(LogFailure):
            replay_tree(events, self.log.run_id)

    def test_replay_rejects_forged_budget_reservation(self):
        scorer = self.scorer()
        scorer.score_children(self.root)
        events = copy.deepcopy(self.log.read_events())
        event = next(item for item in events if item["kind"] == "scorer_request_started")
        event["payload"]["estimated_cost_usd"] = 0
        with self.assertRaises(LogFailure):
            replay_tree(events, self.log.run_id)


if __name__ == "__main__":
    unittest.main()

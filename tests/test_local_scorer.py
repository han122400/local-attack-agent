"""Local ordinal scoring uses bounded loopback HTTP and categorical input."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx2 as httpx

import decision_provider
from scorer_session import RiskScorer
from scorer_state import empty_scorer, usage as scorer_usage
from experiment_config import require_execution_ready, validate_experiment_config
from session_log import SessionLog
from v4_tree import TreeStore
from test_scorer_contract import node
from test_v4_tree import surface


class LocalScorerTests(unittest.TestCase):
    def test_unknown_local_token_usage_does_not_invent_a_remote_cost(self):
        state = empty_scorer()
        state["configuration"] = {"provider": "local", "catalog_elapsed_seconds": 0}
        state["requests"]["S000001"] = {"node_ids": ["N0002"], "estimated_cost_usd": 0,
            "timeout_seconds": 5, "finish": {"attempted": True, "usage": None, "elapsed_seconds": 1}}
        totals = scorer_usage(state)
        self.assertIsNone(totals["input_tokens"])
        self.assertEqual(totals["cost_usd"], 0)

    def test_local_pin_rejects_boolean_pricing(self):
        pin = {"id": "qwen3.5:9b", "canonical_slug": "qwen3.5:9b",
               "output_modalities": ["ordinal_label"], "catalog_retrieved_at": "2026-10-29T00:00:00Z",
               "digest": "a" * 64, "pricing": {"prompt": False, "completion": 0, "request": 0}}
        with self.assertRaises(decision_provider.ProviderError):
            decision_provider.validate_local_pin(pin)

    def test_local_execution_gate_accepts_ollama_loopback_only(self):
        body = {"experiment_mode": "v4", "scorer_provider": "local",
                "scorer_model": "qwen3.5:9b", "scorer_settings": {"protocol": "ollama"}}
        require_execution_ready(validate_experiment_config(body))
        for settings in ({"protocol": "decisions"}, {"base_url": "http://example.test:11434/"},
                         {"base_url": 11434}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                require_execution_ready(validate_experiment_config({**body, "scorer_settings": settings}))

    def test_ollama_pin_and_ordinal_response_without_raw_evidence(self):
        self.assertTrue(hasattr(decision_provider, "LocalOllamaScorer"))
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b", "digest": "a" * 64}]})
            self.assertEqual(request.url.path, "/api/chat")
            body = json.loads(request.content)
            self.assertEqual(body["format"]["properties"]["scores"]["required"], ["N0002"])
            self.assertFalse(body["stream"])
            self.assertNotIn("PRIVATE", request.content.decode())
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True,
                "message": {"content": '{"scores":{"N0002":3}}'},
                "prompt_eval_count": 40, "eval_count": 5})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        adapter = decision_provider.LocalOllamaScorer(settings={"base_url": "http://127.0.0.1:11434/",
            "num_ctx": 4096, "num_predict": 128, "temperature": 0}, client=client)
        pin = adapter.resolve("qwen3.5:9b")
        self.assertEqual(pin["digest"], "a" * 64)
        result = adapter.evaluate(pin, node("N0001", kind="root"), [node()], ["N0002"],
                                  max_input_bytes=16000, timeout_seconds=15)
        self.assertEqual(result["scores"]["N0002"]["score"], 75.0)
        self.assertIsNone(result["scores"]["N0002"]["confidence"])
        self.assertEqual(result["usage"], {"input_tokens": 40, "output_tokens": 5, "cost": 0.0})
        self.assertEqual(len(requests), 3)

    def test_local_model_digest_drift_stops_before_chat(self):
        digests = iter(["a" * 64, "b" * 64])
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b", "digest": next(digests)}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        adapter = decision_provider.LocalOllamaScorer(client=client)
        pin = adapter.resolve("qwen3.5:9b")
        with self.assertRaises(decision_provider.ProviderError) as error:
            adapter.evaluate(pin, node("N0001", kind="root"), [node()], ["N0002"],
                             max_input_bytes=16000, timeout_seconds=15)
        self.assertEqual(error.exception.code, "model_digest_changed")
        self.assertEqual(paths, ["/api/tags", "/api/tags"])

    def test_local_ordinal_scoring_persists_and_replays_without_probabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"protocol": "ollama", "base_url": "http://127.0.0.1:11434/",
                        "num_ctx": 4096, "num_predict": 128, "temperature": 0}
            log = SessionLog(Path(tmp), "a" * 32, {"experiment_mode": "v4", "scorer_provider": "local",
                "scorer_model": "qwen3.5:9b", "scorer_settings": settings})
            self.addCleanup(log.close)
            tree = TreeStore(log)
            root = tree.initialize("https://example.test/")
            child = tree.discover(root, surface("https://example.test/search"))
            requests = []
            def handler(request):
                requests.append(request)
                if request.url.path == "/api/tags":
                    return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b", "digest": "a" * 64}]})
                return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True,
                    "message": {"content": json.dumps({"scores": {child: 3}})},
                    "prompt_eval_count": 40, "eval_count": 5})
            client = httpx.Client(transport=httpx.MockTransport(handler))
            self.addCleanup(client.close)
            adapter = decision_provider.LocalOllamaScorer(settings=settings, client=client)
            scorer = RiskScorer(tree, adapter=adapter)
            self.assertEqual(scorer.score_children(root)["ordered_node_ids"], [child])
            risk = tree.state["nodes"][child]["risk"]
            self.assertEqual((risk["score"], risk["score_method"]), (75.0, "ordinal_label"))
            self.assertIsNone(risk["probabilities"])
            self.assertIsNone(risk["confidence"])
            self.assertEqual(scorer.usage()["cost_usd"], 0.0)
            self.assertEqual(TreeStore(log).rebuild()["nodes"][child]["risk"], risk)
            self.assertEqual(len(requests), 3)

    def test_local_wire_input_limit_marks_node_unscored_before_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = SessionLog(Path(tmp), "b" * 32, {"experiment_mode": "v4", "scorer_provider": "local",
                "scorer_model": "qwen3.5:9b", "scorer_settings": {"max_input_bytes": 1900}})
            self.addCleanup(log.close)
            tree = TreeStore(log)
            root = tree.initialize("https://example.test/")
            child = tree.discover(root, surface("https://example.test/search"))
            paths = []
            def handler(request):
                paths.append(request.url.path)
                return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b", "digest": "a" * 64}]})
            client = httpx.Client(transport=httpx.MockTransport(handler))
            self.addCleanup(client.close)
            scorer = RiskScorer(tree, adapter=decision_provider.LocalOllamaScorer(client=client))
            result = scorer.score_children(root)
            self.assertTrue(result["ready"])
            self.assertEqual(result["unscored"], {child: "input_limit"})
            self.assertEqual(paths, ["/api/tags"])


if __name__ == "__main__":
    unittest.main()

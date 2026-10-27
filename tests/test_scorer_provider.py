import json
import unittest

import httpx2 as httpx

from decision_provider import OpenRouterDecisions, ProviderError
from risk_rubric import CRITERIA
from test_scorer_contract import node, answer


CATALOG = {"data": [{"id": "typesafe/jev-1.14", "canonical_slug": "typesafe/jev-1.14-20261014",
                     "architecture": {"output_modalities": ["decisions"]},
                     "pricing": {"prompt": "0.000000042", "completion": "0", "request": "0"}}]}


class ProviderTests(unittest.TestCase):
    def adapter(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return OpenRouterDecisions(api_key="TEST_SECRET", client=client)

    def test_catalog_pin_and_real_request_contract_and_egress(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            body = json.loads(request.content)
            self.assertEqual(str(request.url), "https://openrouter.ai/api/alpha/decisions")
            self.assertEqual(body["questions"]["risk_N0002"]["criteria"], list(CRITERIA))
            self.assertNotIn("PRIVATE", request.content.decode())
            self.assertNotIn("TEST_SECRET", request.content.decode())
            self.assertEqual(request.headers["Authorization"], "Bearer TEST_SECRET")
            return httpx.Response(200, json={"model": CATALOG["data"][0]["canonical_slug"],
                "answers": {"risk_N0002": answer()}, "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.00000042}})
        adapter = self.adapter(handler)
        pin = adapter.resolve("typesafe/jev-1.14")
        self.assertIn("catalog_retrieved_at", pin)
        result = adapter.evaluate(pin, node("N0001", kind="root"), [node()], ["N0002"], max_input_bytes=16000, timeout_seconds=20)
        self.assertEqual(result["scores"]["N0002"]["score"], 75)
        self.assertEqual(len(requests), 2)

    def test_catalog_does_not_trust_filter_or_floating_ids(self):
        bad = json.loads(json.dumps(CATALOG))
        bad["data"][0]["architecture"]["output_modalities"] = ["text"]
        adapter = self.adapter(lambda request: httpx.Response(200, json=bad))
        for model in ("typesafe/jev-1.14", "~typesafe/jev-latest", "missing/model"):
            with self.subTest(model=model), self.assertRaises(ProviderError):
                adapter.resolve(model)

    def test_malformed_catalog_fields_are_provider_failures(self):
        for change in ({"architecture": []}, {"architecture": {"output_modalities": "decisions"}},
                       {"pricing": None}, {"pricing": {"prompt": True, "completion": "0"}}):
            bad = json.loads(json.dumps(CATALOG))
            bad["data"][0].update(change)
            adapter = self.adapter(lambda request: httpx.Response(200, json=bad))
            with self.subTest(change=change), self.assertRaises(ProviderError):
                adapter.resolve("typesafe/jev-1.14")

    def test_error_is_bounded_redacted_and_does_not_follow_redirect(self):
        for status, retryable in ((401, False), (402, False), (429, True), (503, True), (302, False)):
            calls = []
            def handler(request):
                calls.append(request)
                if request.method == "GET":
                    return httpx.Response(200, json=CATALOG)
                return httpx.Response(status, headers={"Location": "https://evil.invalid/"}, text="TEST_SECRET PRIVATE_ERROR")
            adapter = self.adapter(handler)
            pin = adapter.resolve("typesafe/jev-1.14")
            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                adapter.evaluate(pin, node("N0001", kind="root"), [node()], ["N0002"], max_input_bytes=16000, timeout_seconds=20)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertIsNone(caught.exception.usage)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(len(calls), 2)

    def test_catalog_decoding_failure_is_a_safe_provider_error(self):
        adapter = self.adapter(lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"PRIVATE_CORRUPT_BODY"))
        with self.assertRaises(ProviderError) as caught:
            adapter.resolve("typesafe/jev-1.14")
        self.assertEqual(caught.exception.code, "invalid_response_encoding")
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_oversize_input_makes_no_post_and_invalid_contract_keeps_known_usage(self):
        posts = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            posts.append(request)
            return httpx.Response(200, json={"model": "wrong/model", "answers": {},
                 "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.1}})
        adapter = self.adapter(handler)
        pin = adapter.resolve("typesafe/jev-1.14")
        with self.assertRaises(ProviderError) as caught:
            adapter.evaluate(pin, node("N0001", kind="root"), [node()], ["N0002"], max_input_bytes=20, timeout_seconds=20)
        self.assertFalse(posts)
        self.assertFalse(caught.exception.attempted)
        with self.assertRaises(ProviderError) as caught:
            adapter.evaluate(pin, node("N0001", kind="root"), [node()], ["N0002"], max_input_bytes=16000, timeout_seconds=20)
        self.assertEqual(caught.exception.usage["cost"], 0.1)


if __name__ == "__main__":
    unittest.main()

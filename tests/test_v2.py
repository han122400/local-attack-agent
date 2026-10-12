import unittest

import v2


class V2GuardTests(unittest.TestCase):
    def test_target_and_single_query_change_are_scoped(self):
        original = "http://127.0.0.1:3000/rest/products/search?q=apple&page=1"
        changed = v2.changed_query_url(original, "q", "banana")
        self.assertEqual(changed, "http://127.0.0.1:3000/rest/products/search?q=banana&page=1")
        for url in ("http://example.com/", "http://127.0.0.1:3001/", "http://127.0.0.1:3000.evil.example/"):
            with self.subTest(url=url):
                self.assertFalse(v2.allowed_url(url))
                with self.assertRaises(ValueError):
                    v2.changed_query_url(url, "q", "banana")
        self.assertFalse(v2.allowed_url(123))
        with self.assertRaises(ValueError):
            v2.safe_target("http://127.0.0.1:3000/%64elete")
        with self.assertRaises(ValueError):
            v2.changed_query_url(original, "extra", "banana")
        with self.assertRaises(ValueError):
            v2.changed_query_url(original, "q", "apple")
        self.assertIn("sid=%5Bredacted%5D", v2.redact_url("http://127.0.0.1:3000/socket.io/?sid=abc&q=apple"))

    def test_unknown_evidence_and_unobserved_request_are_rejected(self):
        request = {"id": "REQ-0001", "url": "http://127.0.0.1:3000/rest/products/search?q=apple"}
        hypothesis = {"evidence_ids": ["REQ-0001", "RES-0001"], "claim": "결과 비교",
                      "baseline": {"request_id": "REQ-0001"}, "change": {"query_key": "q", "value": "banana"},
                      "expected_observation": "결과 변화", "required_state": "same_browser_session"}
        records = {"REQ-0001": request, "RES-0001": {"id": "RES-0001", "request_id": "REQ-0001"}}
        self.assertIsNone(v2.validate_hypothesis(hypothesis, records, {"REQ-0001"}))
        hypothesis["evidence_ids"].append("REQ-9999")
        self.assertIn("관찰되지 않은", v2.validate_hypothesis(hypothesis, records, {"REQ-0001"}))
        hypothesis["evidence_ids"].pop()
        self.assertIn("실제 UI", v2.validate_hypothesis(hypothesis, records, set()))
        hypothesis["evidence_ids"] = ["REQ-0001"]
        self.assertIn("연결된 실제 응답", v2.validate_hypothesis(hypothesis, records, {"REQ-0001"}))

    def test_comparison_uses_response_content_not_length_alone(self):
        original = {"status": 200, "sha256": "aaa", "bytes": 10, "list_length": 1}
        changed = {"status": 200, "sha256": "bbb", "bytes": 10, "list_length": 1}
        difference = v2.compare_responses(original, changed)
        self.assertTrue(difference["meaningful_change"])
        self.assertEqual([item["field"] for item in difference["changed_fields"]], ["sha256"])
        self.assertFalse(v2.compare_responses(original, dict(original, bytes=11))["meaningful_change"])

    def test_json_value_differences_are_linked_to_paths(self):
        before = {"status": 200, "content_type": "application/json", "raw": '{"data":[{"name":"Apple"}]}', "truncated": False}
        after = {"status": 200, "content_type": "application/json", "raw": '{"data":[{"name":"Banana"}]}', "truncated": False}
        difference = v2.compare_responses(before, after)
        self.assertEqual(difference["json_differences"][0]["path"], "$.data[0].name")
        whitespace_only = dict(before, raw='{ "data": [ { "name": "Apple" } ] }', sha256="different")
        self.assertFalse(v2.compare_responses(dict(before, sha256="original"), whitespace_only)["meaningful_change"])

    def test_reset_discounts_late_events(self):
        explorer = v2.V2Explorer()
        explorer.generation = 1
        explorer._record(1, "screens", {"url": v2.TARGET})
        explorer.reset()
        self.assertEqual(explorer.snapshot()["observations"]["screens"], [])
        self.assertIsNone(explorer._record(1, "screens", {"url": v2.TARGET}))
        self.assertEqual(explorer.snapshot()["step"], 0)


if __name__ == "__main__":
    unittest.main()

import unittest

import explorer as explorer_module


class V2GuardTests(unittest.TestCase):
    def test_target_and_single_query_change_are_scoped(self):
        original = "http://127.0.0.1:3000/rest/products/search?q=apple&page=1"
        changed = explorer_module.changed_query_url(original, "q", "banana")
        self.assertEqual(changed, "http://127.0.0.1:3000/rest/products/search?q=banana&page=1")
        for url in ("http://example.com/", "http://127.0.0.1:3001/", "http://127.0.0.1:3000.evil.example/"):
            with self.subTest(url=url):
                self.assertFalse(explorer_module.allowed_url(url))
                with self.assertRaises(ValueError):
                    explorer_module.changed_query_url(url, "q", "banana")
        self.assertFalse(explorer_module.allowed_url(123))
        with self.assertRaises(ValueError):
            explorer_module.safe_target("http://127.0.0.1:3000/%64elete")
        with self.assertRaises(ValueError):
            explorer_module.changed_query_url(original, "extra", "banana")
        with self.assertRaises(ValueError):
            explorer_module.changed_query_url(original, "q", "apple")
        self.assertIn("sid=%5Bredacted%5D", explorer_module.redact_url("http://127.0.0.1:3000/socket.io/?sid=abc&q=apple"))

    def test_unknown_evidence_and_unobserved_request_are_rejected(self):
        request = {"id": "REQ-0001", "url": "http://127.0.0.1:3000/rest/products/search?q=apple"}
        hypothesis = {"evidence_ids": ["REQ-0001", "RES-0001"], "claim": "결과 비교",
                      "baseline": {"request_id": "REQ-0001"}, "change": {"query_key": "q", "value": "banana"},
                      "expected_observation": "결과 변화", "required_state": "same_browser_session"}
        records = {"REQ-0001": request, "RES-0001": {"id": "RES-0001", "request_id": "REQ-0001"}}
        self.assertIsNone(explorer_module.validate_hypothesis(hypothesis, records, {"REQ-0001"}))
        hypothesis["evidence_ids"].append("REQ-9999")
        self.assertIn("관찰되지 않은", explorer_module.validate_hypothesis(hypothesis, records, {"REQ-0001"}))
        hypothesis["evidence_ids"].pop()
        self.assertIn("실제 UI", explorer_module.validate_hypothesis(hypothesis, records, set()))
        hypothesis["evidence_ids"] = ["REQ-0001"]
        self.assertIn("연결된 실제 응답", explorer_module.validate_hypothesis(hypothesis, records, {"REQ-0001"}))

    def test_comparison_uses_response_content_not_length_alone(self):
        original = {"status": 200, "sha256": "aaa", "bytes": 10, "list_length": 1}
        changed = {"status": 200, "sha256": "bbb", "bytes": 10, "list_length": 1}
        difference = explorer_module.compare_responses(original, changed)
        self.assertTrue(difference["meaningful_change"])
        self.assertEqual([item["field"] for item in difference["changed_fields"]], ["sha256"])
        self.assertFalse(explorer_module.compare_responses(original, dict(original, bytes=11))["meaningful_change"])

    def test_json_value_differences_are_linked_to_paths(self):
        before = {"status": 200, "content_type": "application/json", "raw": '{"data":[{"name":"Apple"}]}', "truncated": False}
        after = {"status": 200, "content_type": "application/json", "raw": '{"data":[{"name":"Banana"}]}', "truncated": False}
        difference = explorer_module.compare_responses(before, after)
        self.assertEqual(difference["json_differences"][0]["path"], "$.data[0].name")
        whitespace_only = dict(before, raw='{ "data": [ { "name": "Apple" } ] }', sha256="different")
        self.assertFalse(explorer_module.compare_responses(dict(before, sha256="original"), whitespace_only)["meaningful_change"])

    def test_reset_discounts_late_events(self):
        explorer = explorer_module.V2Explorer()
        explorer.generation = 1
        explorer._record(1, "screens", {"url": explorer_module.TARGET})
        explorer.reset()
        self.assertEqual(explorer.snapshot()["observations"]["screens"], [])
        self.assertIsNone(explorer._record(1, "screens", {"url": explorer_module.TARGET}))
        self.assertEqual(explorer.snapshot()["step"], 0)


class ComparisonRegressionTests(unittest.TestCase):
    def response(self, value):
        import json
        return {"status": 200, "content_type": "application/json",
                **explorer_module.summarize_body(json.dumps(value).encode(), "application/json")}

    def test_array_tail_and_missing_null_are_real_changes(self):
        before = list(range(11))
        after = before.copy()
        after[10] = 999
        for left, right, path in (
            ({"items": before}, {"items": after}, "$.items[10]"),
            ({"meta": {"x": None}}, {"meta": {}}, "$.meta.x"),
            ({"items": [None]}, {"items": []}, "$.items[0]"),
            ({"items": []}, {"items": [None]}, "$.items[0]"),
        ):
            with self.subTest(path=path):
                difference = explorer_module.compare_responses(self.response(left), self.response(right))
                self.assertTrue(difference["meaningful_change"])
                self.assertEqual(difference["json_differences"][0]["path"], path)

    def test_detail_limit_does_not_hide_changes(self):
        difference = explorer_module.compare_responses(self.response(list(range(30))), self.response([-1] * 30))
        self.assertTrue(difference["meaningful_change"])
        self.assertEqual(len(difference["json_differences"]), 20)


class CancellationRegressionTests(unittest.TestCase):
    def test_stalled_model_can_stop_reset_and_restart(self):
        import asyncio
        import threading
        import httpx2 as httpx
        from unittest.mock import patch

        entered = threading.Event()
        closed = threading.Event()
        class StalledStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""
            async def aclose(self):
                closed.set()
        class ModelOnlyExplorer(explorer_module.V2Explorer):
            async def _run(self, gen):
                self.loop = asyncio.get_running_loop()
                self.task = asyncio.current_task()
                await self._ask_json(gen, {}, "test", {})
        client_type = httpx.AsyncClient
        def client(**kwargs):
            return client_type(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=StalledStream())), **kwargs)
        explorer = ModelOnlyExplorer()
        with patch("explorer.httpx.AsyncClient", side_effect=client):
            try:
                self.assertTrue(explorer.start())
                self.assertTrue(entered.wait(2))
                explorer.stop()
                explorer.worker.join(2)
                self.assertFalse(explorer.worker.is_alive())
                self.assertTrue(closed.is_set())
                self.assertEqual(explorer.snapshot()["status"], "stopped")
                entered.clear()
                closed.clear()
                self.assertTrue(explorer.start())
                self.assertTrue(entered.wait(2))
                explorer.reset()
                self.assertFalse(explorer.worker.is_alive())
                self.assertTrue(closed.is_set())
                self.assertEqual(explorer.snapshot()["status"], "idle")
                self.assertEqual(explorer.snapshot()["console"], "")
            finally:
                explorer.stop()
                explorer.worker.join(2)

    def test_overall_budget_cancels_a_stalled_run(self):
        import asyncio
        from unittest.mock import patch
        class StalledExplorer(explorer_module.V2Explorer):
            async def _run(self, gen):
                await asyncio.Event().wait()
        explorer = StalledExplorer()
        with patch("explorer.MAX_SECONDS", 0.05):
            explorer.start()
            explorer.worker.join(2)
        self.assertFalse(explorer.worker.is_alive())
        self.assertEqual(explorer.snapshot()["status"], "error")
        self.assertIn("전체 실행 시간", explorer.snapshot()["message"])


class CaptureRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_body_is_recorded_as_error_and_not_offered_for_experiment(self):
        import asyncio
        import time
        from test_planner import observed_run
        explorer, _, request, _ = observed_run()
        explorer.state["observations"]["responses"].clear()
        explorer._deadline = time.monotonic() + 0.05
        response = type("Response", (), {"request": object(), "status": 200,
                        "headers": {"content-type": "application/json"}, "url": explorer_module.TARGET})()
        async def stalled_body():
            await asyncio.Event().wait()
        response.body = stalled_body
        tracker = explorer_module._Tracker(explorer, 0, None)
        tracker.request_ids[response.request] = request["id"]
        tracker.on_response(response)
        await asyncio.wait_for(tracker.flush(), 1)
        recorded = explorer.state["observations"]["responses"][0]
        self.assertIn("error", recorded)
        self.assertTrue(recorded["truncated"])
        self.assertEqual(explorer._eligible(), [])


if __name__ == "__main__":
    unittest.main()

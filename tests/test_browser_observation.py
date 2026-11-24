"""Event-protocol unit tests; no browser is launched."""

import asyncio
import base64
import json
import unittest

from engine.browser_observation import BrowserObservationBuffer


class EventContext:
    def __init__(self):
        self.handlers = {}
        self.non_event_accesses = []

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        self.handlers[name].remove(callback)

    def emit(self, name, value):
        for callback in tuple(self.handlers.get(name, ())):
            callback(value)

    def __getattr__(self, name):
        self.non_event_accesses.append(name)
        raise AssertionError("Collector attempted non-event API: " + name)


class Request:
    method = "GET"

    def __init__(self, url="https://example.test/items"):
        self.url = url
        self.network_accesses = []

    def __getattr__(self, name):
        self.network_accesses.append(name)
        raise AssertionError("Collector attempted request/network API: " + name)


class Response:
    status = 200

    def __init__(self, request, body=b'{"items":[1,2]}', *, length=True,
                 encoding=None, gate=None, error=None):
        self.request, self.data = request, body
        self.body_calls, self.cancelled = 0, False
        self.body_entered = asyncio.Event()
        self.gate, self.error = gate, error
        self.headers = [{"name": "Content-Type", "value": "application/json"}]
        if length:
            self.headers.append({"name": "Content-Length", "value": str(len(body))})
        if encoding:
            self.headers.append({"name": "Content-Encoding", "value": encoding})

    async def headers_array(self):
        return self.headers

    async def body(self):
        self.body_calls += 1
        self.body_entered.set()
        try:
            if self.gate:
                await self.gate.wait()
            if self.error:
                raise self.error
            return self.data
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class BrowserObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = EventContext()
        self.buffer = BrowserObservationBuffer(self.context, "https://example.test/")
        self.addAsyncCleanup(self.buffer.close)

    def emit(self, response):
        self.context.emit("request", response.request)
        self.context.emit("response", response)

    async def test_uses_only_response_events_and_marks_parsed_body_unverified(self):
        self.buffer.begin("EXEC-0001")
        response = Response(Request())
        self.emit(response)
        records = await self.buffer.finish("EXEC-0001")
        self.assertEqual(set(self.context.handlers), {"request", "response", "requestfailed"})
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "browser_response_audit")
        self.assertEqual(record["representation"], "playwright_decoded")
        self.assertIs(record["literal"], False)
        self.assertIs(record["verification_eligible"], False)
        self.assertEqual(base64.b64decode(record["response"]["body_base64"]), response.data)
        self.assertNotIn("facts", record)
        self.assertNotIn("request_id", record)
        self.assertEqual(response.body_calls, 1)

    async def test_post_response_audit_never_dispatches_a_second_request(self):
        self.buffer.begin("EXEC-POST")
        request = Request()
        request.method = "POST"
        response = Response(request, body=b'{"created":true}')
        self.emit(response)
        records = await self.buffer.finish("EXEC-POST")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['request']['method'], 'POST')
        self.assertEqual(response.body_calls, 1)
        self.assertEqual(self.context.non_event_accesses, [])
        self.assertEqual(request.network_accesses, [])

    async def test_unknown_large_and_compressed_bodies_are_not_requested(self):
        for options, reason in (({"length": False}, "unknown_length"),
                                ({"body": b'x' * 65537}, "size_limit"),
                                ({"encoding": "gzip"}, "content_encoded")):
            with self.subTest(reason=reason):
                self.buffer.begin("EXEC-0001")
                response = Response(Request(), **options)
                self.emit(response)
                records = await self.buffer.finish("EXEC-0001")
                self.assertEqual(response.body_calls, 0)
                self.assertEqual(records[0]["response"]["body_omitted"], reason)

    async def test_old_request_cannot_be_attributed_to_new_execution(self):
        self.buffer.begin("EXEC-0001")
        old = Request()
        self.context.emit("request", old)
        await self.buffer.finish("EXEC-0001")
        self.buffer.begin("EXEC-0002")
        late = Response(old)
        self.context.emit("response", late)
        current = Response(Request(), body=b'new')
        self.emit(current)
        records = await self.buffer.finish("EXEC-0002")
        self.assertEqual(late.body_calls, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(base64.b64decode(records[0]["response"]["body_base64"]), b'new')

    async def test_cross_origin_http_and_inactive_requests_are_ignored(self):
        responses = [Response(Request()), Response(Request("https://other.test/")),
                     Response(Request("http://example.test/"))]
        self.emit(responses[0])
        self.buffer.begin("EXEC-0001")
        for response in responses[1:]:
            self.emit(response)
        self.assertEqual(await self.buffer.finish("EXEC-0001"), [])
        self.assertEqual([response.body_calls for response in responses], [0, 0, 0])

    async def test_body_error_retains_metadata_without_controlling_request(self):
        self.buffer.begin("EXEC-0001")
        response = Response(Request(), error=RuntimeError("body unavailable"))
        self.emit(response)
        records = await self.buffer.finish("EXEC-0001")
        self.assertEqual(records[0]["response"]["status"], 200)
        self.assertEqual(records[0]["response"]["body_omitted"], "body_unavailable")

    async def test_finish_is_bounded_and_restarting_drops_old_collectors(self):
        self.buffer.begin("EXEC-0001")
        response = Response(Request(), gate=asyncio.Event())
        self.emit(response)
        await asyncio.wait_for(response.body_entered.wait(), 1)
        start = asyncio.get_running_loop().time()
        await self.buffer.finish("EXEC-0001")
        self.assertLess(asyncio.get_running_loop().time() - start, .4)
        self.buffer.begin("EXEC-0002")
        self.assertEqual(await self.buffer.finish("EXEC-0001"), [])
        fresh = Response(Request())
        self.emit(fresh)
        records = await self.buffer.finish("EXEC-0002")
        self.assertEqual(len(records), 1)
        self.assertTrue(response.cancelled)

    async def test_record_and_total_size_budgets_do_not_fetch_more_bodies(self):
        self.buffer.begin("EXEC-0001")
        responses = []
        for index in range(40):
            response = Response(Request(), body=b'x' * 65536)
            responses.append(response)
            self.emit(response)
            if index < 32:
                await asyncio.wait_for(response.body_entered.wait(), 1)
        records = await self.buffer.finish("EXEC-0001")
        self.assertGreater(len(records), 0)
        self.assertLessEqual(len(records), 32)
        self.assertLessEqual(sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
                                 for item in records), 524288)
        self.assertEqual(sum(response.body_calls for response in responses[32:]), 0)

    async def test_failed_request_and_close_release_event_bindings(self):
        self.buffer.begin("EXEC-0001")
        request = Request()
        self.context.emit("request", request)
        self.context.emit("requestfailed", request)
        response = Response(request)
        self.context.emit("response", response)
        self.assertEqual(await self.buffer.finish("EXEC-0001"), [])
        await self.buffer.close()
        self.assertTrue(all(not handlers for handlers in self.context.handlers.values()))
        self.assertEqual(response.body_calls, 0)

    async def test_at_most_four_body_collectors_wait_concurrently(self):
        self.buffer.begin("EXEC-0001")
        responses = [Response(Request(), gate=asyncio.Event()) for _ in range(12)]
        for response in responses:
            self.emit(response)
        await asyncio.wait_for(asyncio.gather(*(response.body_entered.wait()
                                               for response in responses[:4])), 1)
        self.assertEqual(sum(response.body_calls for response in responses), 4)
        await self.buffer.finish("EXEC-0001")

    async def test_large_header_metadata_is_omitted_without_fetching_body(self):
        self.buffer.begin("EXEC-0001")
        response = Response(Request())
        response.headers.append({"name": "X-Large", "value": "x" * 16385})
        self.emit(response)
        records = await self.buffer.finish("EXEC-0001")
        self.assertEqual(response.body_calls, 0)
        self.assertNotIn("headers", records[0]["response"])
        self.assertEqual(records[0]["response"]["body_omitted"], "headers_unavailable_or_large")


if __name__ == "__main__":
    unittest.main()

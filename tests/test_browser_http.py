"""Managed browser HTTP helpers, with fake requests and no browser or network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qsl, urlencode

from browser_http import BrowserHTTP
from command_runtime import WORKER_SOURCE


TARGET = "https://fixture.invalid/"


def response(body=b"ok", content_type="text/plain; charset=utf-8", status=200, headers=None):
    return SimpleNamespace(url=TARGET, status=status, ok=status < 400,
                           headers={"content-type": content_type, **(headers or {})}, body=AsyncMock(return_value=body))


def route(body=b"csrfToken=secret&q=normal&q=second", content_type="application/x-www-form-urlencoded", **kwargs):
    request = SimpleNamespace(url=kwargs.get("url", TARGET + "form"), method=kwargs.get("method", "POST"),
                              post_data_buffer=body, all_headers=AsyncMock(return_value={
                                  "content-type": content_type, "cookie": "session=private",
                                  "content-length": str(len(body)), "next-action": "observed-action"}))
    return SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock())


class BrowserHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response()),
                                                              fetch=AsyncMock(return_value=response())))
        self.http = BrowserHTTP(self.context, TARGET)
        self.http.begin_step("EXEC-1")

    async def test_existing_get_exposes_headers_and_truncation_without_saving_responses(self):
        self.context.request.get.return_value = response(b"x" * 2400, headers={
            "strict-transport-security": "max-age=300", "set-cookie": "session=secret; HttpOnly; Secure"})
        result = await self.http.http_get(TARGET)
        self.assertEqual(result["length"], 2400)
        self.assertEqual(len(result["body"]), 2000)
        self.assertTrue(result["body_truncated"])
        self.assertEqual(result["headers"]["strict-transport-security"], "max-age=300")
        self.assertNotIn("secret", result["headers"]["set-cookie"])
        self.assertIn("HttpOnly", result["headers"]["set-cookie"])
        self.context.request.get.assert_awaited_once_with(TARGET, max_redirects=0)
        self.assertEqual(self.http.captured_requests(), [])

    async def test_binary_response_does_not_raise_a_unicode_error(self):
        self.context.request.get.return_value = response(b"\xff\xd8\xff", "image/jpeg")
        result = await self.http.http_get(TARGET)
        self.assertTrue(result["binary"])
        self.assertEqual(result["byte_length"], 3)
        self.assertEqual(result["body"], "")

    async def test_text_charset_and_json_are_preserved(self):
        self.context.request.get.return_value = response("café".encode("latin-1"), "text/plain; charset=iso-8859-1")
        self.assertEqual((await self.http.http_get(TARGET))["body"], "café")
        self.context.request.get.return_value = response(b'{"ok":true}', "application/json")
        self.assertEqual((await self.http.http_get(TARGET))["json"], {"ok": True})

    async def test_same_origin_post_runs_and_is_available_for_explicit_replay(self):
        incoming = route()
        await self.http.guard(incoming)
        incoming.continue_.assert_awaited_once()
        incoming.abort.assert_not_awaited()
        card = self.http.captured_requests()[0]
        self.assertEqual(card["method"], "POST")
        self.assertEqual(card["fields"], ["csrfToken", "q"])
        self.assertEqual(card["step"], "EXEC-1")
        self.assertNotIn("secret", json.dumps(card))
        self.assertNotIn("private", json.dumps(card))

    async def test_blocked_request_has_a_reason_and_never_records_secret_query_values(self):
        incoming = route(url="https://outside.invalid/form?token=secret")
        await self.http.guard(incoming)
        incoming.abort.assert_awaited_once()
        incoming.continue_.assert_not_awaited()
        self.assertEqual(self.http.blocked_requests[0]["reason"], "outside_target_origin")
        self.assertNotIn("secret", json.dumps(self.http.blocked_requests))
        self.assertEqual(self.http.captured_requests(), [])
        with self.assertRaises(ValueError):
            await self.http.http_get("https://outside.invalid/")
        self.context.request.get.assert_not_awaited()

    async def test_unsupported_method_is_blocked(self):
        incoming = route(method="CONNECT")
        await self.http.guard(incoming)
        self.assertEqual(self.http.blocked_requests[0]["reason"], "unsupported_method")

    async def test_websockets_remain_explicitly_blocked(self):
        socket = SimpleNamespace(url="wss://fixture.invalid/ws", close=AsyncMock())
        await self.http.guard_web_socket(socket)
        socket.close.assert_awaited_once()
        self.assertEqual(self.http.blocked_requests[0]["reason"], "websocket_not_supported")

    async def test_credentials_invalid_urls_and_nondefault_ports_cannot_escape_scope(self):
        for url in (None, "https://fixture.invalid:0/", "https://fixture.invalid:444/",
                    "https://user:secret@fixture.invalid/", "https://[invalid/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.http.http_get(url)
        self.context.request.get.assert_not_awaited()
        await self.http.http_get("https://fixture.invalid:443/")
        self.context.request.get.assert_awaited_once()

    async def test_replay_changes_only_named_form_values_and_uses_current_context_cookies(self):
        incoming = route()
        await self.http.guard(incoming)
        request_id = self.http.captured_requests()[0]["id"]
        await self.http.replay_request(request_id, {"q": "changed"})
        args, options = self.context.request.fetch.call_args
        self.assertEqual(args, (TARGET + "form",))
        self.assertEqual(options["method"], "POST")
        self.assertEqual(parse_qsl(options["data"].decode()), [("csrfToken", "secret"), ("q", "changed"), ("q", "changed")])
        self.assertNotIn("cookie", options["headers"])
        self.assertNotIn("content-length", options["headers"])
        self.assertEqual(options["headers"]["next-action"], "observed-action")
        self.assertEqual(options["max_redirects"], 0)
        await self.http.replay_request(request_id)
        self.assertEqual(self.context.request.fetch.call_args.kwargs["data"], incoming.request.post_data_buffer)
        with self.assertRaisesRegex(ValueError, "unknown"):
            await self.http.replay_request(request_id, {"guessed_field": "value"})

    async def test_multipart_replay_preserves_hidden_fields_boundary_and_file_bytes(self):
        body = (b'--B0\r\nContent-Disposition: form-data; name="1_$ACTION_1:0"\r\n\r\n{"id":"action"}\r\n'
                b'--B0\r\nContent-Disposition: form-data; name="1_email"\r\n\r\nnormal@example.com\r\n'
                b'--B0\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n\xff\x00\r\n--B0--\r\n')
        await self.http.guard(route(body, 'multipart/form-data; boundary="B0"'))
        request_id = self.http.captured_requests()[0]["id"]
        await self.http.replay_request(request_id, {"1_email": "other@example.com"})
        options = self.context.request.fetch.call_args.kwargs
        self.assertEqual(options["data"], body.replace(b"normal@example.com", b"other@example.com"))
        self.assertEqual(options["headers"]["content-type"], 'multipart/form-data; boundary="B0"')
        with self.assertRaises(ValueError):
            await self.http.replay_request(request_id, {"file": "replacement"})
        with self.assertRaises(ValueError):
            await self.http.replay_request(request_id, {"1_email": "x\r\n--B0\r\n"})

    async def test_json_replay_preserves_other_fields(self):
        await self.http.guard(route(b'{"q":"normal","nested":{"key":7}}', "application/json"))
        await self.http.replay_request(self.http.captured_requests()[0]["id"], {"q": "changed"})
        self.assertEqual(json.loads(self.context.request.fetch.call_args.kwargs["data"]),
                         {"q": "changed", "nested": {"key": 7}})

    async def test_field_edits_preserve_untouched_raw_encoding_and_json_numbers(self):
        for body, content_type, expected in (
                (b"csrf=%FF&q=normal", "application/x-www-form-urlencoded", b"csrf=%FF&q=changed"),
                (b'{"q":"normal","nonce":1e400}', "application/json", b'{"q":"changed","nonce":1e400}')):
            with self.subTest(content_type=content_type):
                await self.http.guard(route(body, content_type))
                await self.http.replay_request(self.http.captured_requests()[-1]["id"], {"q": "changed"})
                self.assertEqual(self.context.request.fetch.call_args.kwargs["data"], expected)

    async def test_unreadable_schema_metadata_cannot_crash_the_worker(self):
        await self.http.guard(route())
        with patch.object(self.http, "_fields", side_effect=RecursionError("nested JSON")):
            self.assertIsNone(self.http.captured_requests()[0]["fields"])

    async def test_new_worker_request_ids_cannot_refer_to_an_old_workers_request(self):
        await self.http.guard(route())
        old_id = self.http.captured_requests()[0]["id"]
        restarted = BrowserHTTP(self.context, TARGET)
        restarted.begin_step("EXEC-2")
        await restarted.guard(route())
        self.assertNotEqual(old_id, restarted.captured_requests()[0]["id"])
        with self.assertRaises(ValueError):
            await restarted.replay_request(old_id)

    async def test_templates_are_bounded_and_block_reasons_are_per_step(self):
        for i in range(10):
            await self.http.guard(route(url=TARGET + str(i)))
        self.assertEqual(len(self.http.captured_requests()), 8)
        await self.http.guard(route(url="https://outside.invalid/"))
        self.http.begin_step("EXEC-2")
        self.assertEqual(self.http.blocked_requests, [])
        self.assertEqual(len(self.http.captured_requests()), 8)

    async def test_large_template_metadata_does_not_fill_model_context_or_guess_empty_fields(self):
        for index in range(8):
            body = urlencode({"field" + str(index) + "x" * 800: "private"}).encode()
            await self.http.guard(route(body, url=TARGET + "x" * 1000 + str(index)))
        cards = self.http.captured_requests()
        self.assertLessEqual(len(json.dumps(cards, ensure_ascii=False)), 2800)
        self.assertTrue(all(card["fields"] is None for card in cards))
        self.assertTrue(all(card["url_truncated"] for card in cards))
        self.assertNotIn("private", json.dumps(cards))

    async def test_worker_returns_request_metadata_even_when_code_fails_after_an_observation(self):
        # Exercise the real worker protocol with a fake Playwright driver; no browser is launched.
        context = self.context
        context.allowed_route = route()
        context.denied_route = route(url="https://outside.invalid/")
        context.new_page = AsyncMock(return_value=SimpleNamespace())
        context.close = AsyncMock()
        async def register(pattern, guard):
            context.guard = guard
        context.route = AsyncMock(side_effect=register)
        context.route_web_socket = AsyncMock()
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock(), is_connected=lambda: True)
        driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        class FakePlaywright:
            async def __aenter__(self):
                return driver
            async def __aexit__(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, stdout, stderr = (root / name for name in ("probe.py", "stdout", "stderr"))
            source.write_text("await context.guard(context.allowed_route)\n"
                              "await context.guard(context.denied_route)\n"
                              "print(await replay_request(captured_requests()[0]['id'], {'q':'changed'}))\n"
                              "raise ValueError('after observation')\n", encoding="utf-8")
            args = ["worker.py", "127.0.0.1", "12345", "token", TARGET, directory, ""]
            namespace, sent = {"__name__": "worker_unit_test"}, []
            messages = iter([{"type": "execute", "exec_id": "EXEC-7", "path": str(source),
                              "stdout_path": str(stdout), "stderr_path": str(stderr), "timeout": 2}, {"type": "stop"}])
            with patch.object(sys, "argv", args), patch.object(sys, "path", list(sys.path)):
                exec(compile(WORKER_SOURCE, "worker.py", "exec"), namespace)
                namespace.update(async_playwright=FakePlaywright,
                                 send=lambda sock, item: sent.append(item), recv=lambda sock, buffer: (next(messages), b""))
                with patch.object(namespace["socket"], "create_connection", return_value=Mock()):
                    await namespace["main"]()
            result = sent[-1]
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["exception_type"], "ValueError")
            self.assertEqual(result["blocked_requests"][0]["reason"], "outside_target_origin")
            self.assertEqual(result["request_templates"][0]["step"], "EXEC-7")
            self.assertIn("'status': 200", stdout.read_text())
            self.assertIn(b"q=changed", context.request.fetch.call_args.kwargs["data"])
            driver.chromium.launch.assert_awaited_once()
            context.close.assert_awaited_once()

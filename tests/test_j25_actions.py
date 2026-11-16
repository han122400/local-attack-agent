import json
import socket
import threading
import unittest
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from browser_http import BrowserHTTP
from j25_actions import J25Actions
from command_runtime import CommandRuntime
from session_log import SessionLog


ROOT = "https://fixture.invalid/"


class WorkerActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = SimpleNamespace(url=ROOT, content=AsyncMock(return_value=(
            '<a href="/#/search">Search</a><form action="/search" method="get">'
            '<input name="q"></form>')))
        self.context = SimpleNamespace(request=SimpleNamespace(fetch=AsyncMock()))
        self.http = BrowserHTTP(self.context, ROOT, capture_gets=True)
        async def goto(url):
            self.page.url = url
            return self.page
        self.actions = J25Actions(ROOT, self.page, self.http, goto, AsyncMock(return_value={"inputs": [], "buttons": []}))

    async def test_discovery_keeps_spa_route_and_single_get_declaration(self):
        result = await self.actions.perform({"action": "start"})
        self.assertTrue(any(item["url"].endswith("/#/search") for item in result["references"]["pages"]))
        self.assertEqual(result["references"]["inputs"][0]["field"], "q")
        self.assertEqual(result["references"]["inputs"][0]["basis"], "declared")

    async def test_observation_exposes_bounded_page_text_without_private_canary(self):
        secret = "J25-FIXTURE-PRIVATE-123456789"
        self.page.content.return_value = f"<html><body><h1>File preview</h1><div>{secret}</div></body></html>"
        actions = J25Actions(ROOT, self.page, self.http, self.actions.goto, self.actions.list_inputs,
            proof_canaries={"private-note": {"value": secret, "protected": True,
                "authorized_identities": ["owner"]}})
        result = await actions.perform({"action": "start"})
        self.assertIn("File preview", result["page_excerpt"])
        self.assertNotIn(secret, json.dumps(result))

    async def test_observed_query_is_private_and_other_parameters_are_preserved(self):
        request = SimpleNamespace(url=ROOT + "search?q=before&token=topsecret", method="GET",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={"authorization": "Bearer secret"}))
        route = SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock())
        self.http._ensure_browser_scope = AsyncMock()
        await self.http.guard(route)
        cards = self.http.captured_gets()
        self.assertNotIn("topsecret", json.dumps(cards))
        self.assertNotIn("Bearer secret", json.dumps(cards))
        response = SimpleNamespace(url=ROOT, status=200, ok=True, headers={"content-type": "text/plain"},
                                   body=AsyncMock(return_value=b"result"))
        self.context.request.fetch.return_value = response
        await self.http.replay_get(cards[0]["id"], "q", "new value")
        sent = self.context.request.fetch.await_args.args[0]
        self.assertIn("q=new+value", sent)
        self.assertIn("token=topsecret", sent)
        with self.assertRaises(ValueError):
            await self.http.replay_get("expired", "q", "new")

    async def test_capture_failure_does_not_block_the_page_request(self):
        request = SimpleNamespace(url=ROOT + "search?q=value", method="GET", frame=SimpleNamespace(page=object()),
                                  all_headers=AsyncMock(side_effect=RuntimeError("headers unavailable")))
        route = SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock())
        self.http._ensure_browser_scope = AsyncMock()
        self.http.begin_step("EXEC-1")
        await self.http.guard(route)
        route.continue_.assert_awaited_once()
        self.assertEqual(self.http.page_request_count, 1)
        self.assertEqual(self.http.captured_gets(), [])

    async def test_repeated_identical_get_keeps_one_reference(self):
        request = SimpleNamespace(url=ROOT + "search?q=value", method="GET", frame=SimpleNamespace(page=object()),
                                  all_headers=AsyncMock(return_value={"authorization": "Bearer secret"}))
        route = SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock())
        self.http._ensure_browser_scope = AsyncMock()
        self.http.begin_step("EXEC-1")
        await self.http.guard(route)
        first = self.http.captured_gets()[0]["id"]
        self.http.begin_step("EXEC-2")
        await self.http.guard(route)
        cards = self.http.captured_gets()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], first)
        self.assertEqual(cards[0]["step"], "EXEC-2")

    async def test_distinct_authentication_headers_remain_separate_requests(self):
        self.http._ensure_browser_scope = AsyncMock()
        for identity in ("user-a", "user-b"):
            request = SimpleNamespace(url=ROOT + "search?q=value", method="GET", frame=SimpleNamespace(page=object()),
                                      all_headers=AsyncMock(return_value={"x-user": identity}))
            await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        self.assertEqual(len(self.http.captured_gets()), 2)

    async def test_reflected_query_value_is_removed_from_visible_labels(self):
        self.http._ensure_browser_scope = AsyncMock()
        request = SimpleNamespace(url=ROOT + "search?token=secret-value", method="GET",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        self.actions.list_inputs = AsyncMock(return_value={"inputs": [{"name": "q", "label": "secret-value"}]})
        result = await self.actions.perform({"action": "start"})
        self.assertNotIn("secret-value", json.dumps(result))

    async def test_short_query_value_is_redacted_inside_a_larger_label(self):
        self.http._ensure_browser_scope = AsyncMock()
        request = SimpleNamespace(url=ROOT + "search?token=abc", method="GET",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        self.assertNotIn("abc", self.http.redact_get_secrets("prefixabcsuffix"))

    async def test_short_captured_value_does_not_corrupt_reference_origin(self):
        target = "http://127.0.0.1:3000/"
        http = BrowserHTTP(self.context, target, capture_gets=True)
        http._ensure_browser_scope = AsyncMock()
        page = SimpleNamespace(url=target)
        actions = J25Actions(target, page, http, AsyncMock(), AsyncMock())
        request = SimpleNamespace(url=target + "search?token=1", method="GET",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={}))
        await http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        refs = actions.references()
        self.assertTrue(all(urlsplit(item["url"]).netloc == "127.0.0.1:3000"
                            for items in refs.values() for item in items))
        self.assertNotIn("token=1", json.dumps(refs))

    async def test_initial_spa_fragment_query_is_redacted_before_navigation(self):
        target = ROOT + "#/reset?token=fragment-secret"
        self.page.url = target
        actions = J25Actions(target, self.page, self.http, self.actions.goto,
            AsyncMock(return_value={"inputs": [{"name": "q", "label": "fragment-secret"}]}))
        result = await actions.perform({"action": "start"})
        self.assertNotIn("fragment-secret", json.dumps(result))

    async def test_custom_identity_header_and_original_encoding_are_redacted(self):
        self.http._ensure_browser_scope = AsyncMock()
        request = SimpleNamespace(url=ROOT + "search?token=%2fprivate", method="GET",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={"x-user": "person-a"}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        text = self.http.redact_get_secrets("person-a %2fprivate")
        self.assertNotIn("person-a", text)
        self.assertNotIn("%2fprivate", text)

    async def test_evicted_get_reference_is_marked_expired(self):
        self.http._ensure_browser_scope = AsyncMock()
        for index in range(33):
            request = SimpleNamespace(url=ROOT + f"search?q={index}", method="GET", frame=SimpleNamespace(page=object()),
                                      all_headers=AsyncMock(return_value={}))
            await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
            if index == 0:
                refs = self.actions.references()
                old = next(item["id"] for item in refs["inputs"] if item["basis"] == "observed")
        refs = self.actions.references()
        expired = next(item for item in refs["inputs"] if item["id"] == old)
        self.assertFalse(expired["supported"])
        self.assertEqual(expired["limitation"], "request_expired")

    async def test_evicted_query_values_still_get_redacted(self):
        self.http._ensure_browser_scope = AsyncMock()
        for index in range(33):
            value = "old-private-token" if index == 0 else f"value-{index}"
            request = SimpleNamespace(url=ROOT + f"search?q={value}", method="GET", frame=SimpleNamespace(page=object()),
                                      all_headers=AsyncMock(return_value={}))
            await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        self.actions.list_inputs = AsyncMock(return_value={"inputs": [{"name": "q", "label": "old-private-token"}]})
        result = await self.actions.perform({"action": "start"})
        self.assertNotIn("old-private-token", json.dumps(result))

    async def test_comparison_preserves_partial_results_without_claiming_success(self):
        initial = await self.actions.perform({"action": "start"})
        ref = initial["references"]["inputs"][0]["id"]
        self.http.http_get = AsyncMock(side_effect=[{"status": 200, "body": "same", "length": 4,
            "body_truncated": False, "binary": False}, TimeoutError("timeout")])
        result = await self.actions.perform({"action": "compare", "ref": ref, "normal": "a", "test": "b"})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["comparison"]["samples"]), 1)
        self.assertEqual(result["comparison"]["failed_stage"], "test")

    async def test_verify_action_reports_private_canary_without_exposing_its_value(self):
        self.actions.proof_canaries = {"private-note": {"value": "CANARY-PRIVATE-12345",
            "protected": True, "authorized_identities": ["owner"]}}
        initial = await self.actions.perform({"action": "start"})
        ref = initial["references"]["inputs"][0]["id"]
        def reply(url):
            body = "CANARY-PRIVATE-12345" if "q=probe" in url else "ordinary"
            return {"status": 200, "body": body, "length": len(body),
                    "body_truncated": False, "binary": False}
        self.http.http_get = AsyncMock(side_effect=reply)
        result = await self.actions.perform({"action": "verify", "kind": "injection", "ref": ref,
                                             "normal": "plain", "test": "probe"})
        self.assertEqual(result["verification"]["status"], "independently_verified")
        self.assertEqual(result["verification"]["information"], ["private-note"])
        self.assertNotIn("CANARY-PRIVATE-12345", json.dumps(result))

    async def test_truncated_normal_samples_cannot_be_called_stable(self):
        initial = await self.actions.perform({"action": "start"})
        ref = initial["references"]["inputs"][0]["id"]
        sample = {"status": 200, "body": "prefix", "length": 10000, "body_truncated": True, "binary": False}
        self.http.http_get = AsyncMock(return_value=sample)
        result = await self.actions.perform({"action": "compare", "ref": ref, "normal": "a", "test": "b"})
        self.assertIsNone(result["comparison"]["baseline_stable"])

    async def test_multi_field_or_truncated_declaration_does_not_create_a_baseline(self):
        self.page.content.return_value = ('<form action="/search" method="get">'
            '<input name="q"><input name="csrf"></form>')
        result = await self.actions.perform({"action": "start"})
        self.assertTrue(result["references"]["inputs"])
        self.assertTrue(all(not item["supported"] for item in result["references"]["inputs"]))
        with patch("j25_actions.analyze_response", return_value={"assets": [], "hints": [],
                "surfaces": [{"locator": {"url": ROOT + "search?q=", "method": "GET", "input": "q"}}],
                "truncated": True}):
            result = await self.actions.perform({"action": "start"})
        self.assertTrue(all(not item["supported"] for item in result["references"]["inputs"]))

    async def test_post_declaration_is_visible_as_unsupported(self):
        self.page.content.return_value = '<form action="/login" method="post"><input name="email"></form>'
        result = await self.actions.perform({"action": "start"})
        entry = result["references"]["inputs"][0]
        self.assertFalse(entry["supported"])
        self.assertEqual(entry["method"], "POST")
        comparison = await self.actions.perform({"action": "compare", "ref": entry["id"], "normal": "a", "test": "b"})
        self.assertEqual(comparison["status"], "incomplete")

    async def test_visible_input_can_be_filled_but_not_submitted_without_scope(self):
        self.actions.list_inputs = AsyncMock(return_value={"inputs": [
            {"tag": "input", "type": "search", "name": "q", "label": "Search"}], "buttons": []})
        self.page.evaluate = AsyncMock(return_value=True)
        initial = await self.actions.perform({"action": "start"})
        ref = next(item["id"] for item in initial["references"]["inputs"] if item["basis"] == "visible")
        result = await self.actions.perform({"action": "interact", "ref": ref, "value": "apple"})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["interaction"]["filled"])
        self.assertEqual(self.page.evaluate.await_args.args[1][2], "q")
        self.context.request.fetch.assert_not_awaited()
        self.page.url = ROOT + "other"
        stale = await self.actions.perform({"action": "interact", "ref": ref, "value": "banana"})
        self.assertEqual(stale["status"], "incomplete")
        self.assertEqual(stale["error"], "ValueError")

    async def test_submitting_visible_form_requires_explicit_mutation_scope(self):
        self.actions.list_inputs = AsyncMock(return_value={"inputs": [
            {"tag": "input", "type": "text", "name": "email", "label": "Email"}], "buttons": []})
        self.page.evaluate = AsyncMock(return_value=True)
        self.page.keyboard = SimpleNamespace(press=AsyncMock())
        self.page.wait_for_timeout = AsyncMock()
        initial = await self.actions.perform({"action": "start"})
        ref = next(item["id"] for item in initial["references"]["inputs"] if item["basis"] == "visible")
        denied = await self.actions.perform({"action": "submit", "ref": ref, "value": "test@example.com"})
        self.assertEqual(denied["status"], "incomplete")
        self.page.keyboard.press.assert_not_awaited()
        self.http.allowed_mutation_paths = {"/login"}
        submitted = await self.actions.perform({"action": "submit", "ref": ref, "value": "test@example.com"})
        self.assertEqual(submitted["status"], "ok")
        self.assertTrue(submitted["interaction"]["submitted"])
        self.page.keyboard.press.assert_awaited_once_with("Enter")

    async def test_observed_get_can_be_replayed_once_with_bounded_value(self):
        self.http._ensure_browser_scope = AsyncMock()
        request = SimpleNamespace(url=ROOT + "search?q=before&token=private", method="GET",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        response = SimpleNamespace(url=ROOT, status=200, ok=True, headers={"content-type": "text/plain"},
                                   body=AsyncMock(return_value=b"result"))
        self.context.request.fetch.return_value = response
        ref = next(item["id"] for item in self.actions.references()["inputs"] if item["basis"] == "observed")
        result = await self.actions.perform({"action": "replay", "ref": ref, "value": "new"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"]["status"], 200)
        self.assertNotIn("private", json.dumps(result))
        self.assertIn("q=new", self.context.request.fetch.await_args.args[0])

    async def test_captured_post_is_a_reference_but_not_replayable_by_default(self):
        self.http._ensure_browser_scope = AsyncMock()
        request = SimpleNamespace(url=ROOT + "login", method="POST", post_data_buffer=b"email=a&password=secret",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={
                "content-type": "application/x-www-form-urlencoded", "authorization": "Bearer private"}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        refs = self.actions.references()["inputs"]
        self.assertTrue(any(item["method"] == "POST" for item in refs))
        self.assertNotIn("secret", json.dumps(refs))
        ref = next(item["id"] for item in refs if item["method"] == "POST")
        result = await self.actions.perform({"action": "replay", "ref": ref, "value": "new"})
        self.assertEqual(result["status"], "incomplete")
        self.context.request.fetch.assert_not_awaited()

    async def test_post_body_secret_reflected_in_page_label_is_redacted(self):
        self.http._ensure_browser_scope = AsyncMock()
        request = SimpleNamespace(url=ROOT + "login", method="POST", post_data_buffer=b"password=body-secret",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={
                "content-type": "application/x-www-form-urlencoded"}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        self.actions.list_inputs = AsyncMock(return_value={"inputs": [{"name": "q", "label": "body-secret"}]})
        result = await self.actions.perform({"action": "start"})
        self.assertNotIn("body-secret", json.dumps(result))

    async def test_scoped_post_replay_preserves_private_fields_and_blocks_other_paths(self):
        self.http = BrowserHTTP(self.context, ROOT,
            allowed_methods={"GET", "HEAD", "OPTIONS", "POST"}, allowed_mutation_paths={"/login"})
        self.http._ensure_browser_scope = AsyncMock()
        self.actions.http = self.http
        request = SimpleNamespace(url=ROOT + "login", method="POST",
            post_data_buffer=b"email=a&password=secret&csrfToken=private",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={
                "content-type": "application/x-www-form-urlencoded"}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        ref = next(item["id"] for item in self.actions.references()["inputs"] if item["field"] == "email")
        self.context.request.fetch.return_value = SimpleNamespace(url=ROOT + "login", status=200, ok=True,
            headers={"content-type": "application/json"}, body=AsyncMock(return_value=b'{"ok":true}'))
        result = await self.actions.perform({"action": "replay", "ref": ref, "value": "b"})
        self.assertEqual(result["status"], "ok")
        sent = self.context.request.fetch.await_args.kwargs
        self.assertEqual(sent["method"], "POST")
        self.assertIn(b"csrfToken=private", sent["data"])
        self.assertNotIn("private", json.dumps(result))

    async def test_scoped_post_verification_uses_normal_test_recheck_and_keeps_token_private(self):
        self.http = BrowserHTTP(self.context, ROOT,
            allowed_methods={"GET", "HEAD", "OPTIONS", "POST"}, allowed_mutation_paths={"/login"})
        self.http._ensure_browser_scope = AsyncMock()
        self.actions.http = self.http
        self.actions.proof_canaries = {"private-note": {"value": "CANARY-PRIVATE-12345",
            "protected": True, "authorized_identities": ["owner"]}}
        request = SimpleNamespace(url=ROOT + "login", method="POST",
            post_data_buffer=b"email=before&csrfToken=private",
            frame=SimpleNamespace(page=object()), all_headers=AsyncMock(return_value={
                "content-type": "application/x-www-form-urlencoded"}))
        await self.http.guard(SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
        ref = next(item["id"] for item in self.actions.references()["inputs"] if item["field"] == "email")
        self.assertTrue(next(item for item in self.actions.references()["inputs"] if item["id"] == ref)["supported"])
        def reply(_url, **kwargs):
            body = b"CANARY-PRIVATE-12345" if b"email=probe" in kwargs["data"] else b"ordinary"
            return SimpleNamespace(url=ROOT + "login", status=200, ok=True,
                headers={"content-type": "text/plain"}, body=AsyncMock(return_value=body))
        self.context.request.fetch.side_effect = reply
        result = await self.actions.perform({"action": "verify", "kind": "injection", "ref": ref,
                                             "normal": "plain", "test": "probe"})
        self.assertEqual(result["verification"]["status"], "independently_verified")
        self.assertEqual(self.context.request.fetch.await_count, 3)
        self.assertTrue(all(call.kwargs["method"] == "POST" and b"csrfToken=private" in call.kwargs["data"]
                            for call in self.context.request.fetch.await_args_list))
        self.assertNotIn("csrfToken=private", json.dumps(result))
        self.assertNotIn("CANARY-PRIVATE-12345", json.dumps(result))

    async def test_failed_navigation_and_unknown_visible_input_remain_incomplete(self):
        self.http.last_navigation_status = 403
        self.actions.list_inputs = AsyncMock(return_value={"inputs": [{"name": "search", "label": "Search"}]})
        self.page.content.return_value = "<html><body>Login required</body></html>"
        result = await self.actions.perform({"action": "start"})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["navigation"]["status"], 403)
        self.assertTrue(any(item["field"] == "search" and not item["supported"]
                            for item in result["references"]["inputs"]))

    async def test_blocked_subresources_do_not_invalidate_successful_navigation(self):
        self.http.last_navigation_status = 200
        self.http.blocked_requests = [{"url": "https://fonts.invalid/font.woff2", "method": "GET",
                                       "reason": "outside_target_origin"}]
        result = await self.actions.perform({"action": "start"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["navigation"], {"status": 200, "blocked_requests": 1})

    async def test_truncated_asset_cannot_register_a_supported_declaration(self):
        self.page.content.return_value = '<script src="/app.js"></script>'
        initial = await self.actions.perform({"action": "start"})
        asset = initial["references"]["assets"][0]["id"]
        self.http.http_get = AsyncMock(return_value={"status": 200, "body": "fetch('/search?q=x')",
            "length": 5000, "body_truncated": True, "binary": False, "content_type": "text/javascript"})
        result = await self.actions.perform({"action": "analyze", "ref": asset})
        self.assertTrue(result["truncated"])
        self.assertTrue(all(not item["supported"] for item in result["references"]["inputs"]))

    async def test_stop_after_normal_prevents_later_comparison_requests(self):
        initial = await self.actions.perform({"action": "start"})
        ref = initial["references"]["inputs"][0]["id"]
        stopped = [False]
        self.actions.stop_requested = lambda: stopped[0]
        async def first_request(url):
            stopped[0] = True
            return {"status": 200, "body": "normal", "length": 6, "body_truncated": False, "binary": False}
        self.http.http_get = AsyncMock(side_effect=first_request)
        result = await self.actions.perform({"action": "compare", "ref": ref, "normal": "a", "test": "b"})
        self.assertEqual(self.http.http_get.await_count, 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["comparison"]["samples"]), 1)


class WorkerTransportTests(unittest.TestCase):
    def test_private_initial_target_is_sent_over_control_channel_not_process_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            run_id = "8" * 32
            log = SessionLog(Path(temp), run_id, {"prompt_version": "J2.5"})
            runtime = CommandRuntime(Path(temp), log)
            secret_scope = {"isolation": "recoverable", "allowed_methods": ["POST"],
                "allowed_paths": ["/login"], "identities": [{"name": "test-user", "cookies": [
                    {"name": "sid", "value": "cookie-private", "url": ROOT}]}]}
            runtime.start_session(run_id, browser=False, j25_test_scope=secret_scope)
            runtime.target = ROOT + "?token=private-value"
            conn = SimpleNamespace(sendall=Mock(), close=Mock())
            server = SimpleNamespace(bind=Mock(), listen=Mock(), settimeout=Mock(), close=Mock(),
                getsockname=lambda: ("127.0.0.1", 1234), accept=lambda: (conn, None))
            process = SimpleNamespace(pid=123)
            try:
                with patch("command_runtime.socket.socket", return_value=server), \
                     patch("command_runtime.subprocess.Popen", return_value=process) as popen, \
                     patch("command_runtime.uuid.uuid4", return_value=SimpleNamespace(hex="fixture-token")), \
                     patch.object(runtime, "_register"), patch.object(runtime, "_start_output_readers"), \
                     patch.object(runtime, "_capture_browser_tree"), \
                     patch.object(runtime, "_wait_control", side_effect=[{"type": "hello", "token": "fixture-token"}, {"type": "ready"}]):
                    runtime._start_browser_worker()
                args = popen.call_args.args[0]
                self.assertNotIn("private-value", json.dumps(args))
                self.assertIn("private-value", conn.sendall.call_args.args[0].decode())
                self.assertNotIn("cookie-private", json.dumps(args))
                self.assertIn("cookie-private", conn.sendall.call_args.args[0].decode())
                self.assertNotIn("private-value", json.dumps(log.read_events()))
                self.assertNotIn("cookie-private", json.dumps(log.read_events()))
            finally:
                runtime._worker = None
                runtime._conn = None
                runtime.stop(run_id, 5)
                runtime.destroy(run_id, 5)
                log.close()
    def test_switching_test_identity_uses_isolated_cookie_jars(self):
        cookies = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cookies.append(self.headers.get("Cookie", ""))
                body = (b'<html><body>Fixture page<form action="/note" method="post">'
                        b'<input name="q"><button type="submit">Send</button></form></body></html>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                cookies.append("POST " + self.path + " " + self.headers.get("Cookie", ""))
                body = b"<html><body>Saved</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        target = f"http://127.0.0.1:{server.server_port}/"
        scope = {"isolation": "recoverable", "allowed_methods": ["POST"], "allowed_paths": ["/note"],
                 "identities": [{"name": name, "cookies": [{"name": "sid", "value": secret, "url": target}]}
                                for name, secret in (("alice", "alice-private"), ("bob", "bob-private"))]}
        try:
            with tempfile.TemporaryDirectory() as temp:
                run_id = "5" * 32
                log = SessionLog(Path(temp), run_id, {"prompt_version": "J2.5"})
                runtime = CommandRuntime(Path(temp), log)
                try:
                    runtime.start_session(run_id, target=target, j25_test_scope=scope)
                    result = runtime.run(run_id, {"action": "j25", "decision": {"action": "start"}})
                    self.assertEqual(result.j25_result["status"], "ok")
                    for name in ("alice", "bob"):
                        switched = runtime.run(run_id, {"action": "j25", "decision": {
                            "action": "identity", "value": name}})
                        self.assertEqual(switched.j25_result["identity"], name)
                        ref = switched.j25_result["references"]["pages"][0]["id"]
                        observed = runtime.run(run_id, {"action": "j25", "decision": {
                            "action": "observe", "ref": ref}})
                        self.assertEqual(observed.j25_result["status"], "ok")
                        if name == "alice":
                            input_ref = next(item["id"] for item in observed.j25_result["references"]["inputs"]
                                             if item.get("basis") == "visible")
                            submitted = runtime.run(run_id, {"action": "j25", "decision": {
                                "action": "submit", "ref": input_ref, "value": "test"}})
                            self.assertEqual(submitted.j25_result["status"], "ok")
                    self.assertTrue(any("sid=alice-private" in cookie for cookie in cookies))
                    self.assertTrue(any("sid=bob-private" in cookie for cookie in cookies))
                    self.assertTrue(any(cookie.startswith("POST /note sid=alice-private") for cookie in cookies))
                    self.assertTrue(any(not cookie for cookie in cookies))
                    self.assertNotIn("alice-private", json.dumps(log.read_events()))
                finally:
                    runtime.stop(run_id, 10)
                    runtime.destroy(run_id, 10)
                    log.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_scoped_post_does_not_follow_redirect_to_unapproved_path(self):
        posted = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<form action="/note" method="post"><input name="q"><button>Send</button></form>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                posted.append(self.path)
                self.send_response(307 if self.path == "/note" else 200)
                if self.path == "/note":
                    self.send_header("Location", "/delete")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                run_id = "6" * 32
                log = SessionLog(Path(temp), run_id, {"prompt_version": "J2.5"})
                runtime = CommandRuntime(Path(temp), log)
                target = f"http://127.0.0.1:{server.server_port}/"
                scope = {"isolation": "recoverable", "allowed_methods": ["POST"],
                         "allowed_paths": ["/note"], "identities": [{"name": "test", "cookies": []}]}
                try:
                    runtime.start_session(run_id, target=target, j25_test_scope=scope)
                    start = runtime.run(run_id, {"action": "j25", "decision": {"action": "start"}})
                    ref = next(item["id"] for item in start.j25_result["references"]["inputs"]
                               if item.get("basis") == "visible")
                    runtime.run(run_id, {"action": "j25", "decision": {"action": "submit",
                                                                          "ref": ref, "value": "x"}})
                    self.assertEqual(posted, ["/note"])
                finally:
                    runtime.stop(run_id, 10)
                    runtime.destroy(run_id, 10)
                    log.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_worker_verifies_private_canary_without_logging_its_value(self):
        canary = "CANARY-PRIVATE-12345"
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/search":
                    body = canary if parse_qs(parsed.query).get("q") == ["probe"] else "ordinary"
                else:
                    body = '<form action="/search" method="get"><input name="q"></form>Fixture'
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                run_id = "4" * 32
                log = SessionLog(Path(temp), run_id, {"prompt_version": "J2.5"})
                runtime = CommandRuntime(Path(temp), log)
                target = f"http://127.0.0.1:{server.server_port}/"
                scope = {"isolation": "recoverable", "allowed_methods": ["POST"],
                         "allowed_paths": ["/note"], "identities": [{"name": "owner", "cookies": []}],
                         "proof_canaries": [{"label": "private-note", "value": canary,
                             "protected": True, "authorized_identities": ["owner"]}]}
                try:
                    runtime.start_session(run_id, target=target, j25_test_scope=scope)
                    start = runtime.run(run_id, {"action": "j25", "decision": {"action": "start"}})
                    ref = next(item["id"] for item in start.j25_result["references"]["inputs"]
                               if item.get("basis") == "declared")
                    checked = runtime.run(run_id, {"action": "j25", "decision": {"action": "verify",
                        "kind": "injection", "ref": ref, "normal": "plain", "test": "probe"}})
                    self.assertEqual(checked.j25_result["verification"]["status"], "independently_verified")
                    self.assertNotIn(canary, json.dumps(checked.j25_result))
                    self.assertNotIn(canary, json.dumps(log.read_events()))
                finally:
                    runtime.stop(run_id, 10)
                    runtime.destroy(run_id, 10)
                    log.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_access_verification_compares_public_and_owner_sessions(self):
        canary = "CANARY-OWNER-PRIVATE-12345"
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/private":
                    seen.append(self.headers.get("Cookie", ""))
                    body = canary
                else:
                    body = '<html><body>Fixture<a href="/private">Private</a></body></html>'
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                run_id = "3" * 32
                log = SessionLog(Path(temp), run_id, {"prompt_version": "J2.5"})
                runtime = CommandRuntime(Path(temp), log)
                target = f"http://127.0.0.1:{server.server_port}/"
                scope = {"isolation": "recoverable", "allowed_methods": ["POST"],
                         "allowed_paths": ["/note"], "identities": [{"name": "owner", "cookies": [
                             {"name": "sid", "value": "owner-private", "url": target}]}],
                         "proof_canaries": [{"label": "private-note", "value": canary,
                             "protected": True, "authorized_identities": ["owner"]}]}
                try:
                    runtime.start_session(run_id, target=target, j25_test_scope=scope)
                    start = runtime.run(run_id, {"action": "j25", "decision": {"action": "start"}})
                    ref = next(item["id"] for item in start.j25_result["references"]["pages"]
                               if item["url"].endswith("/private"))
                    checked = runtime.run(run_id, {"action": "j25", "decision": {"action": "verify_access",
                        "ref": ref, "value": "owner"}})
                    self.assertEqual(checked.j25_result["verification"]["status"], "independently_verified")
                    self.assertEqual(seen, ["", "sid=owner-private", ""])
                    self.assertNotIn(canary, json.dumps(checked.j25_result))
                finally:
                    runtime.stop(run_id, 10)
                    runtime.destroy(run_id, 10)
                    log.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_browser_input_module_distinguishes_executed_dom_read_from_escaped_text(self):
        canary = "CANARY-DOM-PRIVATE-12345"
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                protected = canary if "sid=owner-private" in self.headers.get("Cookie", "") else "public"
                sink = "textContent" if self.path == "/safe" else "innerHTML"
                body = (f'<html><body><a href="/safe">Safe</a><div id="secret">{protected}</div>'
                        f'<input id="q"><div id="out"></div><script>document.getElementById("q")'
                        f'.addEventListener("input",e=>document.getElementById("out").{sink}=e.target.value)'
                        f'</script></body></html>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                run_id = "2" * 32
                log = SessionLog(Path(temp), run_id, {"prompt_version": "J2.5"})
                runtime = CommandRuntime(Path(temp), log)
                target = f"http://127.0.0.1:{server.server_port}/"
                scope = {"isolation": "recoverable", "allowed_methods": ["POST"],
                         "allowed_paths": ["/note"], "identities": [{"name": "owner", "cookies": [
                             {"name": "sid", "value": "owner-private", "url": target}]}],
                         "proof_canaries": [{"label": "dom-note", "value": canary,
                             "protected": True, "authorized_identities": ["owner"]}]}
                try:
                    runtime.start_session(run_id, target=target, j25_test_scope=scope)
                    switched = runtime.run(run_id, {"action": "j25", "decision": {"action": "identity",
                                                                                   "value": "owner"}})
                    root_ref = switched.j25_result["references"]["pages"][0]["id"]
                    observed = runtime.run(run_id, {"action": "j25", "decision": {"action": "observe",
                                                                                   "ref": root_ref}})
                    input_ref = next(item["id"] for item in observed.j25_result["references"]["inputs"]
                                     if item.get("basis") == "visible")
                    vulnerable = runtime.run(run_id, {"action": "j25", "decision": {
                        "action": "verify_browser", "ref": input_ref}})
                    self.assertEqual(vulnerable.j25_result["verification"]["status"], "independently_verified",
                                     vulnerable.j25_result)
                    safe_ref = next(item["id"] for item in observed.j25_result["references"]["pages"]
                                    if item["url"].endswith("/safe"))
                    safe_page = runtime.run(run_id, {"action": "j25", "decision": {"action": "observe",
                                                                                    "ref": safe_ref}})
                    safe_input = next(item["id"] for item in safe_page.j25_result["references"]["inputs"]
                                      if item.get("basis") == "visible" and item.get("url", "").endswith("/safe"))
                    protected = runtime.run(run_id, {"action": "j25", "decision": {
                        "action": "verify_browser", "ref": safe_input}})
                    self.assertNotEqual(protected.j25_result["verification"]["status"], "independently_verified")
                    self.assertNotIn(canary, json.dumps(vulnerable.j25_result))
                finally:
                    runtime.stop(run_id, 10)
                    runtime.destroy(run_id, 10)
                    log.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_cancel_drains_progress_already_waiting_in_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            run_id = "8" * 32
            log = SessionLog(Path(temp), run_id, {})
            runtime = CommandRuntime(Path(temp), log)
            reader, writer = socket.socketpair()
            runtime._conn = reader
            progress = {"type": "j25_progress", "exec_id": "EXEC-1", "j25_result": {"status": "incomplete"}}
            writer.sendall((json.dumps(progress) + "\n").encode())
            runtime._cancel.set()
            received = []
            try:
                with self.assertRaises(InterruptedError):
                    runtime._wait_control(1, on_progress=received.append)
                self.assertEqual(received, [progress])
            finally:
                reader.close()
                writer.close()
                log.close()

    def test_cancelled_worker_retains_partial_comparison(self):
        with tempfile.TemporaryDirectory() as temp:
            run_id = "8" * 32
            log = SessionLog(Path(temp), run_id, {})
            runtime = CommandRuntime(Path(temp), log)
            runtime.start_session(run_id, browser=False)
            runtime._browser_enabled = True
            worker = SimpleNamespace(pid=123, poll=lambda: None)
            runtime._owned[123] = {"pid": 123}
            progress = {"action": "compare", "ref": "I1", "status": "incomplete",
                        "comparison": {"samples": [{"stage": "normal", "status": 200}]}, "request_count": 1}
            def wait(timeout, on_poll=None, on_progress=None):
                on_progress({"type": "j25_progress", "exec_id": "EXEC-0001", "j25_result": progress})
                raise InterruptedError("stopped")
            try:
                with patch.object(runtime, "_worker", worker), patch.object(runtime, "_send_control"), \
                     patch.object(runtime, "_wait_control", side_effect=wait), \
                     patch.object(runtime, "_kill_tree") as kill, patch.object(runtime, "_unregister"):
                    result = runtime.run(run_id, {"action": "j25", "decision": {"action": "compare"}})
                    self.assertTrue(result.cancelled)
                    self.assertEqual(result.j25_result["comparison"]["samples"][0]["stage"], "normal")
                    kill.assert_called_once()
            finally:
                runtime._owned.clear()
                runtime._browser_enabled = False
                runtime.stop(run_id, 5)
                runtime.destroy(run_id, 5)
                log.close()

    def test_typed_action_crosses_worker_channel_without_generated_source(self):
        with tempfile.TemporaryDirectory() as temp:
            run_id = "8" * 32
            log = SessionLog(Path(temp), run_id, {})
            runtime = CommandRuntime(Path(temp), log)
            runtime.start_session(run_id, browser=False)
            runtime._browser_enabled = True
            reply = {"type": "result", "exec_id": "EXEC-0001", "exit_code": 0,
                     "j25_result": {"status": "ok", "references": {}}}
            try:
                with patch.object(runtime, "_worker", SimpleNamespace(poll=lambda: None)), \
                     patch.object(runtime, "_send_control") as send, \
                     patch.object(runtime, "_wait_control", return_value=reply), \
                     patch.object(runtime, "_capture_browser_tree"):
                    result = runtime.run(run_id, {"action": "j25", "decision": {"action": "start"}})
                self.assertEqual(result.j25_result["status"], "ok")
                self.assertEqual(send.call_args.args[0]["type"], "j25_execute")
                self.assertNotIn("code", send.call_args.args[0])
                self.assertFalse(list((runtime.runtime_dir / "tools").glob("EXEC-*.py")))
            finally:
                runtime._browser_enabled = False
                runtime.stop(run_id, 5)
                runtime.destroy(run_id, 5)
                log.close()

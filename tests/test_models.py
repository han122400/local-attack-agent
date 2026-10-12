import asyncio
import io
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

from fastapi import HTTPException
from starlette.requests import Request

import app
import explorer as explorer_module
from model_transport import mock_model


class ModelRouteTests(unittest.TestCase):
    def start_request(self, body, origin=None):
        headers = [(b"content-type", b"application/json")]
        if origin:
            headers.append((b"origin", origin.encode()))
        request = Request({"type": "http", "headers": headers})
        request._json = body
        # Exercise the route without Windows' socket-backed event loop.
        with patch("app.run_in_threadpool", AsyncMock(side_effect=lambda function: function()), create=True):
            coroutine = app.start(request)
            try:
                coroutine.send(None)
            except StopIteration as done:
                return done.value
            finally:
                coroutine.close()
        self.fail("Unexpected asynchronous wait")

    def test_installed_models_are_loaded_from_local_ollama(self):
        payload = {"models": [{"name": "qwen3.5:2b", "size": 2000}, {"name": "other:7b", "size": 7000}]}
        with patch("app.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as opened:
            response = app.ollama_models()
        self.assertEqual(json.loads(response.body)["models"], payload["models"])
        self.assertEqual(json.loads(response.body)["default_model"], app.MODEL)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(opened.call_args.args[0], "http://127.0.0.1:11434/api/tags")

    def test_empty_list_and_unavailable_ollama_are_distinct(self):
        with patch("app.urlopen", return_value=io.BytesIO(b'{"models": []}')):
            response = app.ollama_models()
            self.assertEqual(json.loads(response.body)["models"], [])
        with patch("app.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(HTTPException) as error:
                app.ollama_models()
            self.assertEqual(error.exception.status_code, 503)
            self.assertIn("Ollama", error.exception.detail)

    def test_malformed_model_list_is_reported(self):
        for payload in ([], {"models": None}, {"models": [None]}, {"models": [{"name": ""}]}):
            with self.subTest(payload=payload), patch("app.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                with self.assertRaises(HTTPException) as error:
                    app.ollama_models()
                self.assertEqual(error.exception.status_code, 503)

    def test_start_passes_installed_model_to_explorer(self):
        with patch("app.get_ollama_models", return_value=[{"name": "other:7b", "size": 7000}]), patch.object(app.EXPLORER, "start", return_value=True) as start:
            response = self.start_request({"target": explorer_module.TARGET, "model": "other:7b"})
        self.assertEqual(response, {"started": True})
        start.assert_called_once_with(explorer_module.TARGET, "other:7b")

    def test_start_rejects_unknown_or_invalid_model_before_running(self):
        for model in ("missing:7b", "", None, ["other:7b"]):
            with self.subTest(model=model), patch("app.get_ollama_models", return_value=[{"name": "other:7b"}]), patch.object(app.EXPLORER, "start") as start:
                with self.assertRaises(HTTPException) as error:
                    self.start_request({"model": model})
                self.assertEqual(error.exception.status_code, 400)
                start.assert_not_called()

    def test_missing_model_uses_default_and_running_run_is_rejected(self):
        with patch("app.get_ollama_models", return_value=[{"name": app.MODEL}]), patch.object(app.EXPLORER, "start", return_value=False) as start:
            with self.assertRaises(HTTPException) as error:
                self.start_request({})
        self.assertEqual(error.exception.status_code, 409)
        start.assert_called_once_with(explorer_module.TARGET, app.MODEL)

    def test_start_reports_unavailable_ollama_and_blocks_cross_site(self):
        with patch("app.get_ollama_models", side_effect=RuntimeError("Ollama 연결 실패")), patch.object(app.EXPLORER, "start") as start:
            with self.assertRaises(HTTPException) as error:
                self.start_request({"model": "other:7b"})
            self.assertEqual(error.exception.status_code, 503)
            with self.assertRaises(HTTPException) as error:
                self.start_request({}, origin="https://example.com")
            self.assertEqual(error.exception.status_code, 403)
            start.assert_not_called()


class RunModelTests(unittest.TestCase):
    def test_selected_model_is_saved_in_run_and_console(self):
        explorer = explorer_module.V2Explorer()
        with patch("explorer.threading.Thread"):
            self.assertTrue(explorer.start(explorer_module.TARGET, "other:7b"))
        self.assertEqual(explorer.snapshot()["model"], "other:7b")
        self.assertIn("other:7b", explorer.snapshot()["console"])
        with patch.object(app.EXPLORER, "snapshot", return_value=explorer.snapshot()):
            self.assertEqual(json.loads(app.export_state().body)["model"], "other:7b")

    def test_active_run_keeps_its_model_when_start_is_rejected(self):
        explorer = explorer_module.V2Explorer()
        with patch("explorer.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = True
            explorer.start(explorer_module.TARGET, "other:7b")
            self.assertFalse(explorer.start(explorer_module.TARGET, "third:8b"))
        self.assertEqual(explorer.snapshot()["model"], "other:7b")

    def test_ollama_request_uses_run_model_and_keeps_thinking_disabled(self):
        explorer = explorer_module.V2Explorer()
        with patch("explorer.threading.Thread"):
            explorer.start(explorer_module.TARGET, "other:7b")
        request = {"id": "REQ-0001", "action_id": "ACT-0001", "method": "GET", "url": explorer_module.TARGET + "rest/products/search?q=apple", "query": {"q": "apple"}}
        response = {"id": "RES-0001", "status": 200, "content_type": "application/json"}
        stream = io.BytesIO(b'{"message":{"content":"{}"}}\n')
        with mock_model(return_value=stream) as opened:
            self.assertEqual(asyncio.run(explorer._ask_ollama(explorer.generation, request, response)), ({}, ""))
        sent = json.loads(opened.call_args.args[0].content)
        self.assertEqual(sent["model"], "other:7b")
        self.assertIs(sent["think"], False)
        self.assertIs(sent["stream"], True)

    def test_previous_generation_cannot_use_next_runs_model(self):
        explorer = explorer_module.V2Explorer()
        explorer.generation = 2
        with mock_model() as opened:
            self.assertEqual(asyncio.run(explorer._ask_ollama(1, {}, {})), (None, "중지됨"))
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()

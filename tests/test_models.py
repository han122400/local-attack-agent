import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

from fastapi import HTTPException
from starlette.requests import Request

import app
import explorer as explorer_module
from harness import Harness
from session_log import SessionLog, SessionStore


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
        with patch("app.get_ollama_models", return_value=[{"name": "other:7b", "size": 7000}]), patch.object(app.EXPLORER, "start", return_value="a" * 32) as start:
            response = self.start_request({"target": explorer_module.TARGET, "model": "other:7b", "goal": "inspect"})
        self.assertEqual(response, {"started": True, "run_id": "a" * 32, "run_url": "/runs/" + "a" * 32})
        start.assert_called_once_with(explorer_module.TARGET, "other:7b", goal="inspect", model_settings=None,
                                      scenario_source="manual", chain_mode="none", budgets=None)

    def test_start_rejects_unknown_or_invalid_model_before_running(self):
        for model in ("missing:7b", "", None, ["other:7b"]):
            with self.subTest(model=model), patch("app.get_ollama_models", return_value=[{"name": "other:7b"}]), patch.object(app.EXPLORER, "start") as start:
                with self.assertRaises(HTTPException) as error:
                    self.start_request({"goal": "inspect", "model": model})
                self.assertEqual(error.exception.status_code, 400)
                start.assert_not_called()

    def test_missing_model_uses_default_and_running_run_is_rejected(self):
        with patch("app.get_ollama_models", return_value=[{"name": app.MODEL}]), patch.object(app.EXPLORER, "start", return_value=False) as start:
            with self.assertRaises(HTTPException) as error:
                self.start_request({"goal": "inspect"})
        self.assertEqual(error.exception.status_code, 409)
        start.assert_called_once_with(explorer_module.TARGET, app.MODEL, goal="inspect", model_settings=None,
                                      scenario_source="manual", chain_mode="none", budgets=None)

    def test_start_reports_unavailable_ollama_and_blocks_cross_site(self):
        with patch("app.get_ollama_models", side_effect=RuntimeError("Ollama 연결 실패")), patch.object(app.EXPLORER, "start") as start:
            with self.assertRaises(HTTPException) as error:
                self.start_request({"goal": "inspect", "model": "other:7b"})
            self.assertEqual(error.exception.status_code, 503)
            with self.assertRaises(HTTPException) as error:
                self.start_request({}, origin="https://example.com")
            self.assertEqual(error.exception.status_code, 403)
            start.assert_not_called()


class RunModelTests(unittest.TestCase):
    def test_selected_model_is_saved_in_run_and_sent_to_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            sent = []
            def transport(payload):
                sent.append(payload)
                yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                                                          "path": "", "content": "", "command": "", "code": ""})}}
            explorer = explorer_module.ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                                  lab_reset=lambda: {"verified": True})
            run_id = explorer.start(explorer_module.TARGET, "other:7b", goal="inspect")
            explorer.worker.join(10)
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["model"], "other:7b")
            self.assertEqual(sent[0]["model"], "other:7b")

    def test_active_run_keeps_its_model_when_start_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            entered = threading.Event()
            release = threading.Event()
            def transport(_payload):
                entered.set()
                release.wait(5)
                yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                                                          "path": "", "content": "", "command": "", "code": ""})}}
            explorer = explorer_module.ExplorerV3(Path(temp), transport=transport, browser_enabled=False,
                                                  lab_reset=lambda: {"verified": True})
            run_id = explorer.start(explorer_module.TARGET, "other:7b", goal="inspect")
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(explorer.start(explorer_module.TARGET, "third:8b", goal="inspect"))
                self.assertEqual(explorer.snapshot()["model"], "other:7b")
            finally:
                release.set()
                explorer.worker.join(10)
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["model"], "other:7b")

    def test_harness_rejects_other_run_id_before_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "f" * 32, {})
            opened = []
            harness = Harness(log, transport=lambda payload: opened.append(payload))
            with self.assertRaises(ValueError):
                harness.decide("a" * 32, {}, model="other:7b", settings={"think": False, "options": {}})
            self.assertEqual(opened, [])
            log.close()


if __name__ == "__main__":
    unittest.main()

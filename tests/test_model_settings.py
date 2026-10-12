import asyncio
import inspect
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import app
import explorer as explorer_module
from model_transport import mock_model


def start_request(body):
    request = Request({"type": "http", "headers": [(b"content-type", b"application/json")]})
    request._json = body
    with patch("app.run_in_threadpool", AsyncMock(side_effect=lambda function, *args: function(*args))):
        coroutine = app.start(request)
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        finally:
            coroutine.close()
    raise AssertionError("Unexpected asynchronous wait")


class ModelSettingsTests(unittest.TestCase):
    def test_custom_settings_reach_the_run_from_start_api(self):
        settings = {"think": True, "keep_alive": "10m", "options": {"temperature": 0.7, "num_ctx": 16384, "num_predict": 2048, "top_k": 20}}
        with patch("app.get_ollama_models", return_value=[{"name": "qwen3.5:9b"}]), patch.object(app.EXPLORER, "start", return_value=True) as start:
            self.assertEqual(start_request({"model": "qwen3.5:9b", "model_settings": settings}), {"started": True})
        self.assertEqual(start.call_args.kwargs.get("model_settings"), settings)

    def test_bad_settings_are_rejected_before_starting(self):
        invalid = [None, [], {"think": "invented"}, {"think": 1}, {"keep_alive": "forever"}, {"keep_alive": []}, {"keep_alive": True},
                   {"options": {"temperature": float("nan")}}, {"options": {"top_p": 1.2}},
                   {"options": {"num_ctx": True}}, {"options": {"num_predict": 0}},
                   {"options": {"num_ctx": 999999999}}, {"options": {"unknown": 1}}]
        for settings in invalid:
            with self.subTest(settings=settings), patch("app.get_ollama_models", return_value=[{"name": app.MODEL}]), patch.object(app.EXPLORER, "start") as start:
                with self.assertRaises(HTTPException) as error:
                    start_request({"model_settings": settings})
                self.assertEqual(error.exception.status_code, 400)
                start.assert_not_called()

    def test_run_records_its_own_copy_and_rejects_changes_while_active(self):
        explorer = explorer_module.V2Explorer()
        self.assertIn("model_settings", inspect.signature(explorer.start).parameters)
        settings = {"think": False, "options": {"temperature": 0.8, "num_predict": 1024}}
        with patch("explorer.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = True
            self.assertTrue(explorer.start(model_settings=settings))
            settings["options"]["temperature"] = 1.2
            self.assertFalse(explorer.start(model_settings={"think": True}))
        self.assertEqual(explorer.snapshot()["model_settings"]["options"]["temperature"], 0.8)
        self.assertIs(explorer.snapshot()["model_settings"]["think"], False)
        with patch.object(app.EXPLORER, "snapshot", return_value=explorer.snapshot()):
            self.assertEqual(json.loads(app.export_state().body)["model_settings"], explorer.snapshot()["model_settings"])

    def test_shared_model_transport_uses_settings_and_separates_thinking_from_json(self):
        explorer = explorer_module.V2Explorer()
        settings = {"think": True, "keep_alive": "10m", "options": {"temperature": 0.7, "num_ctx": 16384, "num_predict": 2048, "seed": 42}}
        explorer.state["model_settings"] = settings
        stream = io.BytesIO(b'{"message":{"thinking":"checking observations"}}\n{"message":{"content":"{\\"ok\\":true}"}}\n{"done":true,"done_reason":"stop"}\n')
        with mock_model(return_value=stream) as opened:
            result = asyncio.run(explorer._ask_json(0, {}, "system", {}))
        self.assertEqual(result, ({"ok": True}, ""))
        payload = json.loads(opened.call_args.args[0].content)
        self.assertIs(payload["think"], True)
        self.assertEqual(payload["keep_alive"], "10m")
        self.assertEqual(payload["options"], settings["options"])
        self.assertIn("checking observations", explorer.snapshot()["console"])

    def test_model_default_thinking_omits_the_override(self):
        explorer = explorer_module.V2Explorer()
        explorer.state["model_settings"] = {"think": None, "options": {"temperature": 0.3}}
        with mock_model(return_value=io.BytesIO(b'{"message":{"content":"{}"}}\n')) as opened:
            asyncio.run(explorer._ask_json(0, {}, "system", {}))
        self.assertNotIn("think", json.loads(opened.call_args.args[0].content))

    def test_model_details_include_actual_capabilities_without_loading_weights(self):
        self.assertTrue(hasattr(app, "ollama_model_details"), "Model details endpoint is missing")
        show = {"capabilities": ["completion", "thinking"], "thinking": {"values": [False, True], "default": True},
                "model_info": {"qwen35.context_length": 262144}, "parameters": "temperature 1\ntop_k 20", "details": {"family": "qwen35"}}
        with patch("app.get_ollama_models", return_value=[{"name": "qwen3.5:9b"}]), patch("app.urlopen", return_value=io.BytesIO(json.dumps(show).encode())) as opened:
            response = app.ollama_model_details("qwen3.5:9b")
        body = json.loads(response.body)
        self.assertEqual(body["thinking"], show["thinking"])
        self.assertEqual(body["context_length"], 262144)
        self.assertEqual(body["parameters"], show["parameters"])
        self.assertEqual(opened.call_args.args[0].full_url, "http://127.0.0.1:11434/api/show")
        self.assertEqual(body["presets"][0]["settings"]["options"]["temperature"], 0.7)

    def test_empty_generation_reports_output_limit_instead_of_json_position(self):
        explorer = explorer_module.V2Explorer()
        stream = io.BytesIO(b'{"message":{"thinking":"working"}}\n{"done":true,"done_reason":"length"}\n')
        with mock_model(return_value=stream):
            answer, error = asyncio.run(explorer._ask_json(0, {}, "system", {}))
        self.assertIsNone(answer)
        self.assertIn("num_predict", error)


if __name__ == "__main__":
    unittest.main()

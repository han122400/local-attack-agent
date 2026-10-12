import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import app
from explorer import ExplorerV3
from harness import Harness, ModelFailure
from session_log import SessionLog, SessionStore


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

    def test_run_records_its_own_settings_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = {"think": False, "options": {"temperature": 0.8, "num_predict": 1024}}
            def finish(_payload):
                yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                                                          "path": "", "content": "", "command": "", "code": ""})}}
            explorer = ExplorerV3(Path(temp), transport=finish, browser_enabled=False,
                                  lab_reset=lambda: {"verified": True})
            run_id = explorer.start(goal="inspect", model_settings=settings)
            settings["options"]["temperature"] = 1.2
            explorer.worker.join(10)
            self.assertEqual(SessionStore(Path(temp)).read(run_id)["model_settings"]["options"]["temperature"], 0.8)

    def test_harness_uses_thinking_settings_but_decodes_content_only(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "e" * 32, {})
            sent = []
            def transport(payload):
                sent.append(payload)
                yield {"message": {"thinking": "unparsed reasoning"}}
                yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                                                          "path": "", "content": "", "command": "", "code": ""})}}
            settings = {"think": True, "keep_alive": "10m", "options": {"temperature": 0.7, "num_predict": 2048}}
            self.assertEqual(Harness(log, transport=transport).decide(log.run_id, {}, model="qwen3.5:9b", settings=settings).action, "finish")
            self.assertIs(sent[0]["think"], True)
            self.assertEqual(sent[0]["keep_alive"], "10m")
            self.assertEqual(sent[0]["options"], settings["options"])
            log.close()

    def test_model_default_thinking_omits_override(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "e" * 32, {})
            sent = []
            def transport(payload):
                sent.append(payload)
                yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                                                          "path": "", "content": "", "command": "", "code": ""})}}
            Harness(log, transport=transport).decide(log.run_id, {}, model="mock:model", settings={"think": None, "options": {"temperature": 0.3}})
            self.assertNotIn("think", sent[0])
            log.close()

    def test_model_details_include_actual_capabilities_without_loading_weights(self):
        show = {"capabilities": ["completion", "thinking"], "thinking": {"values": [False, True], "default": True},
                "model_info": {"qwen35.context_length": 262144}, "parameters": "temperature 1\ntop_k 20", "details": {"family": "qwen35"}}
        with patch("app.get_ollama_models", return_value=[{"name": "qwen3.5:9b"}]), patch("app.urlopen", return_value=io.BytesIO(json.dumps(show).encode())) as opened:
            response = app.ollama_model_details("qwen3.5:9b")
        body = json.loads(response.body)
        self.assertEqual(body["thinking"], show["thinking"])
        self.assertEqual(body["context_length"], 262144)
        self.assertEqual(body["parameters"], show["parameters"])
        self.assertEqual(opened.call_args.args[0].full_url, "http://127.0.0.1:11434/api/show")

    def test_empty_generation_reports_output_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), "e" * 32, {})
            def transport(_payload):
                yield {"message": {"thinking": "working"}}
                yield {"done": True, "done_reason": "length"}
            with self.assertRaises(ModelFailure) as raised:
                Harness(log, transport=transport, max_calls=1).decide(log.run_id, {}, model="mock:model",
                                                                       settings={"think": True, "options": {"num_predict": 700}})
            self.assertIn("num_predict", str(raised.exception))
            log.close()


if __name__ == "__main__":
    unittest.main()

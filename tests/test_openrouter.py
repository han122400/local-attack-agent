import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
from fastapi import HTTPException

import app
import lab
import model_providers as providers
from command_runtime import CommandRuntime
from harness import Harness, ModelFailure
from session_log import SessionLog
from test_model_settings import start_request


MODEL = "openrouter:vendor/test-model"
CATALOG = {"data": [{"id": "vendor/test-model", "name": "Test model", "context_length": 32768,
    "architecture": {"output_modalities": ["text"]},
    "top_provider": {"max_completion_tokens": 8192},
    "supported_parameters": ["structured_outputs", "temperature", "max_tokens", "reasoning"],
    "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
    {"id": "vendor/image", "architecture": {"output_modalities": ["image"]}}]}
COMMAND = {"action": "finish", "plan": "done", "reason": "observed", "path": "", "content": "", "command": "", "code": ""}


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text('OPENROUTER_API_KEY="test-secret"\n', encoding="utf-8")
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(providers, "ENV_PATH", self.env_path))
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.client_class = httpx.Client

    def http(self, handler):
        # Exercise real request construction and response parsing without external calls.
        return patch.object(providers.httpx, "Client", side_effect=lambda **kwargs:
            self.client_class(transport=httpx.MockTransport(handler), **kwargs))

    def test_dotenv_key_is_not_added_to_process_environment_and_env_takes_precedence(self):
        self.assertEqual(providers.openrouter_key(), "test-secret")
        self.assertNotIn("OPENROUTER_API_KEY", os.environ)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-secret"}):
            self.assertEqual(providers.openrouter_key(), "env-secret")

    def test_catalog_loads_text_models_with_namespaced_ids_and_no_key(self):
        def respond(request):
            self.assertEqual(request.url, "https://openrouter.ai/api/v1/models")
            self.assertEqual(request.headers["authorization"], "Bearer test-secret")
            return httpx.Response(200, json=CATALOG)
        with self.http(respond):
            response = app.ollama_models(provider="openrouter")
        body = json.loads(response.body)
        self.assertEqual([item["name"] for item in body["models"]], [MODEL])
        self.assertEqual(body["models"][0]["context_length"], 32768)
        self.assertNotIn("test-secret", response.body.decode())

    def test_missing_key_and_http_errors_are_actionable_and_redacted(self):
        self.env_path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY"):
            providers.get_openrouter_models()
        self.env_path.write_text("OPENROUTER_API_KEY=test-secret", encoding="utf-8")
        for status in (401, 402, 429, 500):
            with self.subTest(status=status), self.http(lambda _: httpx.Response(status,
                    json={"error": {"message": "rejected test-secret"}})):
                with self.assertRaises(RuntimeError) as error:
                    providers.get_openrouter_models()
                self.assertIn(str(status), str(error.exception))
                self.assertNotIn("test-secret", str(error.exception))

    def test_start_validates_remote_model_without_contacting_ollama(self):
        with self.http(lambda _: httpx.Response(200, json=CATALOG)), \
                patch.object(app, "get_ollama_models", side_effect=AssertionError("Ollama must not be used")), \
                patch.object(app.EXPLORER, "start", return_value="a" * 32) as start:
            self.assertTrue(start_request({"goal": "inspect", "model": MODEL})["started"])
            self.assertEqual(start.call_args.args[1], MODEL)
            with self.assertRaises(HTTPException) as error:
                start_request({"goal": "inspect", "model": "openrouter:vendor/missing"})
            self.assertEqual(error.exception.status_code, 400)

    def test_model_details_use_remote_catalog(self):
        with self.http(lambda _: httpx.Response(200, json=CATALOG)):
            body = json.loads(app.ollama_model_details(MODEL).body)
        self.assertEqual(body["provider"], "openrouter")
        self.assertEqual(body["context_length"], 32768)

    def test_small_context_reserves_input_space_and_caps_output_before_request(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["data"][0]["context_length"] = 4096
        seen = []
        def respond(request):
            if request.method == "GET": return httpx.Response(200, json=catalog)
            seen.append(json.loads(request.content))
            chunk = {"choices": [{"delta": {"content": json.dumps(COMMAND)}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        log = SessionLog(self.root, "b" * 32, {})
        try:
            with self.http(respond):
                Harness(log).decide(log.run_id, {}, model=MODEL,
                    settings={"think": None, "options": {"num_predict": 8192}})
            self.assertGreater(seen[0]["max_tokens"], 0)
            self.assertLess(seen[0]["max_tokens"], 1500)
        finally:
            log.close()

    def test_prompt_too_large_is_rejected_without_sending_chat_request(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["data"][0]["context_length"] = 1024
        def respond(request):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json=catalog)
        log = SessionLog(self.root, "c" * 32, {})
        try:
            with self.http(respond), self.assertRaisesRegex(ModelFailure, "컨텍스트"):
                Harness(log, max_calls=1).decide(log.run_id, {}, model=MODEL, settings={"think": None, "options": {}})
        finally:
            log.close()

    def test_harness_routes_sse_and_preserves_reasoning_usage_and_safe_logs(self):
        seen = []
        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            self.assertEqual(request.url, "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer test-secret")
            seen.append(json.loads(request.content))
            chunks = [{"choices": [{"delta": {"reasoning": "checking"}, "finish_reason": None}]},
                      {"choices": [{"delta": {"content": json.dumps(COMMAND)}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 90, "completion_tokens": 40}}]
            data = ': heartbeat\n\n' + ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})
        log = SessionLog(self.root, "d" * 32, {})
        self.addCleanup(log.close)
        with self.http(respond):
            command = Harness(log).decide(log.run_id, {}, model=MODEL,
                settings={"think": True, "options": {"num_ctx": 8192, "num_predict": 2000, "temperature": 0.3}})
        self.assertEqual(command.action, "finish")
        self.assertEqual(seen[0]["model"], "vendor/test-model")
        self.assertEqual(seen[0]["max_tokens"], 2000)
        self.assertEqual(seen[0]["temperature"], 0.3)
        self.assertEqual(seen[0]["response_format"]["type"], "json_schema")
        self.assertNotIn("oneOf", seen[0]["response_format"]["json_schema"]["schema"])
        # Route to the cheapest provider that still honours the parameters we send.
        self.assertEqual(seen[0]["provider"], {"sort": "price", "require_parameters": True})
        self.assertNotIn("options", seen[0])
        self.assertNotIn("num_ctx", seen[0])
        events = log.read_events()
        self.assertNotIn("test-secret", json.dumps(events))
        self.assertTrue(any(e["kind"] == "model_delta" and e.get("channel") == "thinking" for e in events))
        response = next(e for e in events if e["kind"] == "model_response")
        self.assertEqual(response["prompt_eval_count"], 90)
        self.assertEqual(response["eval_count"], 40)

    def test_stream_errors_and_truncation_never_execute_a_command(self):
        for chunk, kind in [({"error": {"code": 402, "message": "test-secret"}}, "transport"),
                            ({"choices": [{"delta": {"content": json.dumps(COMMAND)}, "finish_reason": "length"}]}, "output_limit")]:
            with self.subTest(kind=kind):
                def respond(request):
                    if request.method == "GET": return httpx.Response(200, json=CATALOG)
                    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
                log = SessionLog(self.root / kind, "e" * 32, {})
                try:
                    with self.http(respond), self.assertRaises(ModelFailure) as error:
                        Harness(log, max_calls=1).decide(log.run_id, {}, model=MODEL, settings={"think": None, "options": {}})
                    self.assertEqual(error.exception.kind, kind)
                    self.assertNotIn("test-secret", json.dumps(log.read_events()))
                    self.assertFalse(any(e["kind"] == "command_decoded" for e in log.read_events()))
                finally:
                    log.close()

    def test_runtime_child_environment_does_not_inherit_api_key(self):
        log = SessionLog(self.root, "f" * 32, {})
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-secret", "PATH": "test-path"}):
                env = CommandRuntime(self.root, log)._environment()
            self.assertNotIn("OPENROUTER_API_KEY", env)
            self.assertEqual(env["PATH"], "test-path")
        finally:
            log.close()

    def test_docker_child_environment_does_not_inherit_api_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-secret", "PATH": "test-path"}), \
                patch.object(lab.subprocess, "run") as run:
            lab._docker("inspect", "juice-lab")
        self.assertNotIn("OPENROUTER_API_KEY", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "test-path")

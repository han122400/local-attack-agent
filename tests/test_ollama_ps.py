import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from fastapi import HTTPException

import app


class OllamaPsTests(unittest.TestCase):
    def test_cpu_gpu_and_split_match_ollama_ps(self):
        for size, vram, label in (
            (100, 0, "100% CPU"),
            (100, 100, "100% GPU"),
            (100, 52, "48%/52% CPU/GPU"),
            (200, 101, "50%/50% CPU/GPU"),
            (100, 101, "Unknown"),
            (100, None, "Unknown"),
            (None, 0, "Unknown"),
        ):
            with self.subTest(size=size, vram=vram):
                self.assertEqual(app.ollama_processor(size, vram), label)

    def test_running_models_route_reads_local_ps_without_loading_models(self):
        payload = {"models": [{"name": "foundation-sec-web:latest", "digest": "abcdef1234567890", "size": 100,
                               "size_vram": 52, "context_length": 8192, "expires_at": "2026-10-12T03:00:00Z"}]}
        with patch("app.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as opened:
            response = app.ollama_ps()
        self.assertEqual(response.headers["cache-control"], "no-store")
        result = json.loads(response.body)
        self.assertIsInstance(result["checked_at"], (int, float))
        self.assertEqual(result["models"][0], {**payload["models"][0], "id": "abcdef123456", "processor": "48%/52% CPU/GPU"})
        opened.assert_called_once_with("http://127.0.0.1:11434/api/ps", timeout=4)

    def test_empty_list_is_success_and_missing_vram_is_unknown(self):
        with patch("app.urlopen", return_value=io.BytesIO(b'{"models": []}')):
            self.assertEqual(json.loads(app.ollama_ps().body)["models"], [])
        with patch("app.urlopen", return_value=io.BytesIO(b'{"models": [{"name": "test:8b", "size": 100}]}')):
            model = json.loads(app.ollama_ps().body)["models"][0]
            self.assertEqual(model["processor"], "Unknown")
            self.assertIsNone(model["size_vram"])

    def test_offline_and_invalid_responses_return_503(self):
        with patch("app.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(HTTPException) as error:
                app.ollama_ps()
            self.assertEqual(error.exception.status_code, 503)
        for payload in ([], {}, {"models": None}, {"models": [None]}, {"models": [{"name": ""}]}):
            with self.subTest(payload=payload), patch("app.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                with self.assertRaises(HTTPException) as error:
                    app.ollama_ps()
                self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()

"""The second dashboard model catalog is independent of execution models."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


class ScorerModelsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8765")

    def test_provider_specific_catalog_and_invalid_local_endpoint(self):
        with patch("engine.decision_provider.OpenRouterDecisions.list_models", return_value=[
                {"id": "typesafe/jev-1.14", "name": "Jev", "pricing": {"prompt": "0.1"}}]):
            response = self.client.get("/api/scorer/models?provider=openrouter")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"][0]["id"], "typesafe/jev-1.14")
        with patch("engine.decision_provider.LocalOllamaScorer.list_models", return_value=[
                {"id": "qwen3.5:9b", "name": "qwen3.5:9b", "digest": "a" * 64}]):
            response = self.client.get("/api/scorer/models?provider=local")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"][0]["digest"], "a" * 64)
        self.assertEqual(self.client.get("/api/scorer/models?provider=local&base_url=http://example.test:11434/").status_code, 400)


if __name__ == "__main__":
    unittest.main()

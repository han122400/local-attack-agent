"""Run-specific start, state, stop and recovery API contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from explorer import ExplorerV3
from session_log import SessionStore


def finished(_payload):
    yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "enough",
                                              "path": "", "content": "", "command": "", "code": ""})}}


class RunApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.explorer = ExplorerV3(self.root, transport=finished, browser_enabled=False,
                                   lab_reset=lambda: {"verified": True})
        self.addCleanup(self._stop_explorer)
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8765")

    def _stop_explorer(self):
        if self.explorer.worker and self.explorer.worker.is_alive():
            self.explorer.stop()
            self.explorer.worker.join(10)
        self.explorer.lease.release()

    def test_start_returns_run_id_and_run_scoped_state(self):
        with patch.object(app, "EXPLORER", self.explorer), patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            response = self.client.post("/api/start", json={"goal": "inspect catalog", "model": "mock:model",
                                                            "scenario_source": "manual", "chain_mode": "none"})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body["started"])
            self.assertEqual(body["run_url"], "/runs/" + body["run_id"])
            self.explorer.worker.join(10)
            state = self.client.get("/api/runs/" + body["run_id"])
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json()["goal"], "inspect catalog")
            self.assertEqual(state.json()["run_id"], body["run_id"])
            self.assertEqual(self.client.get("/api/runs/" + "f" * 32).status_code, 404)

    def test_unsupported_modes_and_wrong_run_controls_are_rejected(self):
        with patch.object(app, "EXPLORER", self.explorer), patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            for mode in ({"scenario_source": "auto"}, {"chain_mode": "campaign"}):
                response = self.client.post("/api/start", json={"goal": "inspect", "model": "mock:model", **mode})
                self.assertEqual(response.status_code, 400)
            self.assertEqual(SessionStore(self.root).list_runs(), [])
            wrong = self.client.post("/api/runs/" + "f" * 32 + "/stop")
            self.assertIn(wrong.status_code, (404, 409))

    def test_reset_recovers_named_incomplete_run_without_erasing_logs(self):
        run_id = "a" * 32
        log = SessionStore(self.root).create(run_id, {"goal": "inspect", "run_result": "interrupted"})
        log.append("original", {"text": "kept"})
        log.close()
        with patch.object(app, "EXPLORER", self.explorer):
            response = self.client.post(f"/api/runs/{run_id}/reset")
            self.assertEqual(response.status_code, 200, response.text)
            self.explorer.worker.join(10)
        self.assertEqual(SessionStore(self.root).read(run_id)["phase"], "finished")
        self.assertIn('"kind":"original"', (self.root / run_id / "events.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

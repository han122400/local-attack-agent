"""Run-specific start, state, stop and recovery API contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from explorer import ExplorerV3
from planning import load_prompt
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
                                                            "target": "https://my-site.example/catalog",
                                                            "scenario_source": "manual", "chain_mode": "none"})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body["started"])
            self.assertEqual(body["run_url"], "/runs/" + body["run_id"])
            self.explorer.worker.join(10)
            state = self.client.get("/api/runs/" + body["run_id"])
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json()["goal"], "inspect catalog")
            self.assertEqual(state.json()["target"], "https://my-site.example/catalog")
            self.assertEqual(state.json()["lab_reset_status"], "not_applicable")
            self.assertEqual(state.json()["run_result"], "completed")
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

    def test_custom_call_budget_reaches_run_and_model_context(self):
        for limit in (50, 60):
            with self.subTest(limit=limit):
                sent = []
                def report(payload):
                    sent.append(json.loads(payload["messages"][1]["content"]))
                    yield from finished(payload)
                self.explorer.transport = report
                with patch.object(app, "EXPLORER", self.explorer), patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
                    response = self.client.post("/api/start", json={"goal": "inspect", "model": "mock:model",
                        "target": "https://fixture.invalid/", "budgets": {"model_calls": limit}})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.explorer.worker.join(10)
                    state = self.client.get(f"/api/runs/{response.json()['run_id']}").json()
                self.assertEqual(state["budgets"]["model_calls"], limit)
                self.assertEqual(sent[0]["remaining"]["model_calls"], limit)
                self.assertEqual(sent[0]["remaining"]["report_reserve"], 1)

    def test_j1_selection_reaches_model_log_and_final_coverage_report(self):
        sent = []
        def report(payload):
            sent.append(payload)
            yield {"message": {"content": json.dumps({"action": "finish", "plan": "REPORT/report: 결과",
                   "reason": "실행된 조사가 없어 취약점 여부는 판단할 수 없음", "path": "", "content": "", "command": "", "code": ""})}}
        self.explorer.transport = report
        with patch.object(app, "EXPLORER", self.explorer), patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            response = self.client.post("/api/start", json={"goal": "검증 후 보고", "model": "mock:model",
                "target": "https://fixture.invalid/", "prompt_version": "J1", "budgets": {"model_calls": 1}})
            self.assertEqual(response.status_code, 200, response.text)
            run_id = response.json()["run_id"]
            self.explorer.worker.join(10)
            state = self.client.get(f"/api/runs/{run_id}").json()
        prompt, digest = load_prompt("J1")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["messages"][0]["content"], prompt)
        self.assertEqual(state["prompt_version"], "J1")
        self.assertEqual(state["run_result"], "completed")
        self.assertEqual(set(state["j1_state"]["coverage"]), {"SQLi", "IDOR", "XSS", "OTHER"})
        for check in ("SQLi", "IDOR", "XSS"):
            self.assertNotIn(check, state["report"])
        events = [json.loads(line) for line in (self.root / run_id / "events.jsonl").read_text().splitlines()]
        request = next(event for event in events if event["kind"] == "model_request")
        self.assertEqual(request["system_prompt_version"], "J1")
        self.assertEqual(request["system_prompt_sha256"], digest)

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

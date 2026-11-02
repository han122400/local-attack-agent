import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from explorer import ExplorerV3
from session_log import SessionStore
from test_scorer_session import FakeAdapter


def finish(payload):
    yield {"message": {"content": json.dumps({"action": "finish", "plan": "done", "reason": "no further node work",
                                               "path": "", "content": "", "command": "", "code": ""})}}


class ExecutionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.explorer = ExplorerV3(self.root, browser_enabled=False, transport=finish,
                                  scorer_adapter=FakeAdapter(), lab_reset=lambda: self.fail("No lab on generic target"))
        self.addCleanup(self.explorer.lease.release)
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8765")
        patcher = patch.object(app, "EXPLORER", self.explorer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.body = {"target": "https://example.test/", "goal": "inspect", "model": "mock:model", "experiment_mode": "v4",
                     "scorer_provider": "openrouter", "scorer_model": "typesafe/jev-1.14", "scorer_settings": {}}

    def test_api_persists_incomplete_result_when_model_finishes_without_observations(self):
        with patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            response = self.client.post("/api/start", json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        self.explorer.worker.join(10)
        self.assertFalse(self.explorer.worker.is_alive())
        meta = SessionStore(self.root).read(run_id)
        self.assertEqual(meta["run_result"], "incomplete")
        self.assertEqual(meta["finish_reason"], "investigation_incomplete")
        self.assertEqual(meta["prompt_version"], "v4")
        self.assertEqual(meta["v4_stage"], "execution")
        self.assertEqual(meta["report_status"], "complete")
        self.assertEqual(meta["phase"], "finished")
        self.assertFalse((self.root / run_id / "runtime").exists())
        self.assertTrue((self.root / run_id / "report.json").is_file())
        report = json.loads((self.root / run_id / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["run_result"], meta["run_result"])
        self.assertEqual(report["incomplete_nodes"][0]["reason"], "no_successful_observation")

    def test_api_accepts_local_ollama_scorer_settings(self):
        body = {**self.body, "scorer_provider": "local", "scorer_model": "qwen3.5:9b",
                "scorer_settings": {"protocol": "ollama", "base_url": "http://127.0.0.1:11434/"}}
        with patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            response = self.client.post("/api/start", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.explorer.worker.join(10)
        self.assertFalse(self.explorer.worker.is_alive())
        state = SessionStore(self.root).read(response.json()["run_id"])
        self.assertEqual((state["experiment_mode"], state["scorer_provider"], state["scorer_model"]),
                         ("v4", "local", "qwen3.5:9b"))
        self.assertEqual(state["report_status"], "complete")

    def test_mode_prompt_mismatch_and_bad_settings_reject_before_model_lookup(self):
        for changes in ({"prompt_version": "v3"}, {"v4_settings": {"node_attempts": 0}}, {"experiment_mode": "v3", "prompt_version": "v4"}):
            with patch("app.get_ollama_models", side_effect=AssertionError("No lookup for invalid config")):
                response = self.client.post("/api/start", json={**self.body, **changes})
            self.assertEqual(response.status_code, 400, response.text)

    def test_api_executes_a_temporary_python_result_and_exports_it(self):
        import base64
        import subprocess
        def result_transport(payload):
            context = json.loads(payload["messages"][1]["content"])
            result = {"schema_version": 1, "node_id": context["node"]["id"], "attempt_id": context["attempt"]["attempt_id"],
                      "discovered": [], "proposed_verdict": "inconclusive", "claim": "temporary local fixture", "evidence_refs": [], "next_observation": ""}
            encoded = base64.b64encode(json.dumps(result).encode()).decode()
            script = f"import base64,pathlib;p=pathlib.Path('{context['attempt']['result_path']}');p.write_bytes(base64.b64decode('{encoded}'))"
            command = subprocess.list2cmdline([context["python"], "-c", script])
            yield {"message": {"content": json.dumps({"action": "run_command", "plan": "local fixture", "reason": "write result",
                "path": "", "content": "", "code": "", "command": command})}}
        self.explorer.transport = result_transport
        with patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            response = self.client.post("/api/start", json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        self.explorer.worker.join(10)
        meta = SessionStore(self.root).read(run_id)
        self.assertEqual(meta["phase"], "finished", meta.get("cleanup_error"))
        self.assertEqual(meta["run_result"], "incomplete")
        events = [json.loads(line) for line in (self.root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        imported = [event for event in events if event["kind"] == "node_result_imported"]
        diagnostics = [{key: event.get(key) for key in ("kind", "stderr", "exception", "exit_code", "error_kind")}
                       for event in events if event["kind"] in ("exec_finished", "model_error", "run_error")]
        self.assertEqual(len(imported), 1, {"reason": meta.get("finish_reason"), "diagnostics": diagnostics})
        self.assertEqual(imported[0]["payload"]["execution_status"], "succeeded")
        self.assertTrue((self.root / run_id / imported[0]["payload"]["result_ref"]["path"]).is_file())

    def test_interrupted_attempt_is_preserved_without_confirming_or_resuming(self):
        from command_runtime import CommandRuntime
        from v4_tree import TreeStore
        log = SessionStore(self.root).create("c" * 32, {**self.body, "v4_stage": "execution", "managed_processes": [],
            "phase": "running", "budgets": {"finalize_seconds": 10}, "lab_reset_status": "not_applicable"})
        tree = TreeStore(log)
        tree.initialize(self.body["target"])
        runtime = CommandRuntime(self.root, log)
        runtime.start_session(log.run_id, browser=False)
        attempt = tree.begin_attempt("N0001", call_id="CALL-0001")
        log.append("exec_started", {"exec_id": "EXEC-0001", "call_id": "CALL-0001", "action": "run_command"})
        path = runtime.runtime_dir / attempt["result_path"]
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "node_id": "N0001", "attempt_id": "A0001", "discovered": [],
            "proposed_verdict": "confirmed", "claim": "untrusted", "evidence_refs": [], "next_observation": ""}), encoding="utf-8")
        log.close()
        self.explorer.transport = lambda payload: self.fail("No model during recovery")
        self.assertTrue(self.explorer.recover(log.run_id))
        self.explorer.worker.join(10)
        node = json.loads((self.root / log.run_id / "nodes" / "N0001.json").read_text(encoding="utf-8"))
        self.assertEqual(node["verification_state"], "inconclusive")
        self.assertEqual(node["attempts"][0]["execution_status"], "interrupted")
        self.assertFalse(runtime.runtime_dir.exists())

    def test_v3_prompt_list_and_v4_prompt_list_are_compatible(self):
        self.assertNotIn("v4", self.client.get("/api/prompts").json()["versions"])
        self.assertEqual(self.client.get("/api/prompts?experiment_mode=v4").json()["versions"], ["v4"])

    def test_lost_execution_metadata_recovers_without_resuming_models(self):
        with patch("app.get_ollama_models", return_value=[{"name": "mock:model"}]):
            run_id = self.client.post("/api/start", json=self.body).json()["run_id"]
        self.explorer.worker.join(10)
        (self.root / run_id / "session.json").unlink()
        self.explorer.transport = lambda payload: self.fail("Recovery must not resume a model")
        self.assertTrue(self.explorer.recover(run_id))
        self.explorer.worker.join(10)
        metadata = SessionStore(self.root).read(run_id)
        self.assertEqual(metadata["experiment_mode"], "v4")
        self.assertEqual(metadata["v4_stage"], "execution")
        self.assertEqual(metadata["phase"], "finished")
        self.assertEqual(metadata["report_status"], "complete")
        self.assertEqual(metadata["v4_usage"]["execution_calls"], 1)
        self.assertEqual(metadata["v4_usage"]["cost_usd"], 0)

    def test_corrupt_execution_log_yields_degraded_report_and_preserves_runtime(self):
        from command_runtime import CommandRuntime
        from v4_tree import TreeStore
        log = SessionStore(self.root).create("b" * 32, {**self.body, "v4_stage": "execution", "managed_processes": [],
            "phase": "running", "budgets": {"finalize_seconds": 10}, "lab_reset_status": "not_applicable"})
        TreeStore(log).initialize(self.body["target"])
        runtime = CommandRuntime(self.root, log)
        runtime.start_session(log.run_id, browser=False)
        log.append("v4_test_marker", {})
        log.close()
        path = log.events_path
        rows = path.read_text(encoding="utf-8").splitlines()
        rows[1] = "{corrupt"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertTrue(self.explorer.recover(log.run_id))
        self.explorer.worker.join(10)
        metadata = SessionStore(self.root).read(log.run_id)
        self.assertEqual(metadata["report_status"], "degraded")
        self.assertEqual(metadata["phase"], "recovery_required")
        self.assertTrue(runtime.runtime_dir.exists())
        self.assertFalse(self.explorer.can_start()[0])

    def test_bad_recovery_ownership_still_produces_a_report_and_retains_runtime(self):
        from command_runtime import CommandRuntime
        from v4_tree import TreeStore
        log = SessionStore(self.root).create("d" * 32, {**self.body, "v4_stage": "execution", "phase": "running",
            "managed_processes": [{"pid": "invalid", "created_at": 1.0, "run_id": "d" * 32}],
            "budgets": {"finalize_seconds": 10}, "lab_reset_status": "not_applicable"})
        TreeStore(log).initialize(self.body["target"])
        runtime = CommandRuntime(self.root, log)
        runtime.start_session(log.run_id, browser=False)
        log.close()
        self.assertTrue(self.explorer.recover(log.run_id))
        self.explorer.worker.join(10)
        metadata = SessionStore(self.root).read(log.run_id)
        self.assertEqual(metadata.get("report_status"), "degraded")
        self.assertEqual(metadata["phase"], "recovery_required")
        self.assertTrue(runtime.runtime_dir.exists())


if __name__ == "__main__":
    unittest.main()

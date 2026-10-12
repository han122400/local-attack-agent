"""Opt-in local integration gate; run with V3_REAL_OLLAMA=1."""

import json
import os
import unittest

import psutil

from fastapi.testclient import TestClient

import app as dashboard


@unittest.skipUnless(os.environ.get("V3_REAL_OLLAMA") == "1", "requires local Ollama and juice-lab")
class RealOllamaGate(unittest.TestCase):
    def test_two_sequential_sessions_write_execute_observe_and_clean(self):
        prior_run = None
        with TestClient(dashboard.app, base_url="http://127.0.0.1") as client:
            for index in range(2):
                response = client.post("/api/start", json={
                    "target": "http://127.0.0.1:3000/", "model": "qwen3.5:9b",
                    "goal": "Explore the local Juice Shop homepage. Use browser code to observe the page title "
                            "and report only what that output proves.",
                    "scenario_source": "manual", "chain_mode": "none",
                    "model_settings": {"think": False, "options": {"temperature": 0, "num_ctx": 8192,
                                                                  "num_predict": 1200}},
                    "budgets": {"seconds": 180, "model_calls": 8, "tool_calls": 8,
                                "command_seconds": 30, "finalize_seconds": 120},
                })
                self.assertEqual(response.status_code, 200, response.text)
                run_id = response.json()["run_id"]
                print(f"REAL_GATE run={index + 1} id={run_id}", flush=True)
                dashboard.EXPLORER.worker.join(430)
                self.assertFalse(dashboard.EXPLORER.worker.is_alive(), f"run {run_id} did not finish")
                state = dashboard.EXPLORER.get_run(run_id)
                events = [json.loads(line) for line in
                          (dashboard.EXPLORER.store.root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
                kinds = [event["kind"] for event in events]
                actions = [event.get("action") for event in events if event["kind"] == "command_decoded"]
                print(f"REAL_GATE result={state['run_result']} phase={state['phase']} "
                      f"cleanup={state['cleanup_status']} reset={state['lab_reset_status']} "
                      f"actions={actions} reason={state.get('finish_reason', '')[:160]}", flush=True)
                self.assertIn(state["run_result"], {"completed", "timed_out", "error"})
                self.assertNotIn("Executor or lifecycle failure", state.get("finish_reason", ""))
                self.assertEqual(state["phase"], "finished")
                self.assertEqual(state["cleanup_status"], "succeeded")
                self.assertEqual(state["lab_reset_status"], "succeeded")
                self.assertFalse((dashboard.EXPLORER.store.root / run_id / "runtime").exists())
                self.assertEqual(state["managed_processes"], [])
                self.assertTrue((dashboard.EXPLORER.store.root / run_id / "events.jsonl").is_file())
                self.assertIn("run_python", actions)
                completed_execs = {item.get("exec_id") for item in events
                                   if item["kind"] == "exec_finished" and item.get("action") == "run_python"
                                   and item.get("exit_code") == 0}
                self.assertTrue(completed_execs)
                self.assertTrue(any(item["kind"] == "exec_output" and item.get("exec_id") in completed_execs
                                    and item.get("text", "").strip() for item in events))
                model_requests = [item for item in events if item["kind"] == "model_request"]
                self.assertGreaterEqual(len(model_requests), 2)
                following_context = json.loads(model_requests[-1]["input"]["messages"][1]["content"])
                self.assertTrue(any(item["result"].get("exec_id") in completed_execs
                                    and "OWASP Juice Shop" in item["result"].get("stdout", "")
                                    for item in following_context["recent"]))
                for item in events:
                    if item["kind"] != "process_started":
                        continue
                    record = item["process"]
                    try:
                        current = psutil.Process(record["pid"])
                    except psutil.NoSuchProcess:
                        continue
                    self.assertNotAlmostEqual(current.create_time(), record["created_at"], delta=0.01)
                self.assertIn("run_finalized", kinds)
                reset_events = [item for item in events if item["kind"] == "lab_reset"]
                self.assertTrue(reset_events)
                reset_probe = reset_events[-1]["probe"]["probe"]
                self.assertEqual((reset_probe["products"], reset_probe["challenges"], reset_probe["solved"]),
                                 (46, 116, 0))
                self.assertTrue(reset_probe["apple_juice_present"])
                if prior_run:
                    self.assertTrue((dashboard.EXPLORER.store.root / prior_run / "events.jsonl").is_file())
                prior_run = run_id
                self.assertTrue(dashboard.EXPLORER.can_start()[0])


if __name__ == "__main__":
    unittest.main()

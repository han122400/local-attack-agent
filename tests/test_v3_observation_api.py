import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from explorer import ExplorerV3
from session_log import SessionStore


RUN_ID = "6" * 32


class ObservationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.explorer = ExplorerV3(self.root, browser_enabled=False,
                                   lab_reset=lambda: {"verified": True})
        patcher = patch.object(app, "EXPLORER", self.explorer)
        patcher.start()
        self.addCleanup(patcher.stop)
        log = SessionStore(self.root).create(RUN_ID, {
            "goal": "<script>window.__xss=1</script>", "model": "mock:model",
            "started_at": 1, "phase": "finished", "run_result": "completed",
            "cleanup_status": "succeeded", "log_status": "finalized", "lab_reset_status": "succeeded"})
        log.append("run_created", {"goal": "test"})
        log.append("exec_output", {"call_id": "CALL-1", "exec_id": "EXEC-1",
                                   "text": "<img src=x onerror=window.__xss=1>"})
        artifact = log.run_dir / "artifacts" / "code" / "a.py"
        artifact.parent.mkdir()
        artifact.write_text("print('<script>window.__xss=1</script>')", encoding="utf-8")
        (log.run_dir / "artifacts" / "data.bin").write_bytes(b"\x00\x01")
        log.close()
        self.client = TestClient(app.app, base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)

    def test_list_state_saved_events_and_run_page(self):
        listing = self.client.get("/api/runs").json()
        self.assertTrue(listing["can_start"])
        self.assertEqual(listing["runs"][0]["run_id"], RUN_ID)
        state = self.client.get(f"/api/runs/{RUN_ID}").json()
        self.assertFalse(state["live_available"])
        self.assertFalse(state["runtime_exists"])
        self.assertEqual(state["goal"], "<script>window.__xss=1</script>")
        first = self.client.get(f"/api/runs/{RUN_ID}/events?limit=1").json()
        self.assertEqual(first["next_seq"], 1)
        second = self.client.get(f"/api/runs/{RUN_ID}/events?after_seq=1").json()
        self.assertEqual([item["seq"] for item in second["events"]], [2])
        self.assertEqual(self.client.get(f"/runs/{RUN_ID}").status_code, 200)
        self.assertEqual(self.client.get("/runs/" + "f" * 32).status_code, 404)
        self.assertEqual(self.client.get("/api/runs/bad/events").status_code, 400)
        self.assertEqual(self.client.get(f"/api/runs/{RUN_ID}/events?after_seq=-1").status_code, 400)

    def test_file_boundaries_binary_preview_and_export(self):
        deleted = self.client.get(f"/api/runs/{RUN_ID}/files?source=runtime").json()
        self.assertFalse(deleted["available"])
        self.assertEqual(self.client.get(f"/api/runs/{RUN_ID}/file?source=runtime&path=x").status_code, 410)
        files = self.client.get(f"/api/runs/{RUN_ID}/files?source=artifacts").json()["files"]
        self.assertIn("code/a.py", [item["path"] for item in files])
        preview = self.client.get(f"/api/runs/{RUN_ID}/file?source=artifacts&path=code/a.py").json()
        self.assertEqual(preview["kind"], "text")
        self.assertIn("<script>", preview["content"])
        binary = self.client.get(f"/api/runs/{RUN_ID}/file?source=artifacts&path=data.bin").json()
        self.assertEqual(binary["kind"], "binary")
        self.assertIsNone(binary["content"])
        self.assertEqual(self.client.get(f"/api/runs/{RUN_ID}/file?source=artifacts&path=../session.json").status_code, 400)
        self.assertEqual(self.client.get(f"/api/runs/{RUN_ID}/file?source=other&path=x").status_code, 400)
        archive = self.client.get(f"/api/runs/{RUN_ID}/export")
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as packaged:
            self.assertIn("events.jsonl", packaged.namelist())
            self.assertIn("artifacts/code/a.py", packaged.namelist())
            self.assertIn("<script>", packaged.read("artifacts/code/a.py").decode())

    def test_archived_live_stream_ends_without_attaching_to_another_run(self):
        response = self.client.get(f"/api/runs/{RUN_ID}/live")
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: stream_end", response.text)
        self.assertIn(RUN_ID, response.text)


if __name__ == "__main__":
    unittest.main()

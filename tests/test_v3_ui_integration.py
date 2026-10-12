"""Opt-in Chromium coverage for the four v3plan 10.3 UI scenarios."""

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import uvicorn
from playwright.sync_api import sync_playwright

import app
from command_runtime import installed_chromium
from explorer import ExplorerV3


ATTACK_TEXT = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'


def answer(action, **fields):
    command = {"action": action, "plan": f"Inspect {ATTACK_TEXT}", "reason": "Use actual output",
               "path": "", "content": "", "command": "", "code": ""}
    command.update(fields)
    return {"message": {"content": json.dumps(command)}}


@unittest.skipUnless(os.environ.get("V3_UI_INTEGRATION") == "1", "requires local Chromium")
class BrowserUiScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.release_start = threading.Event()
        cls.release_b = threading.Event()
        cls.release_more = threading.Event()
        cls.release_file_edit = threading.Event()
        cls.release_file_delete = threading.Event()
        cls.release_file_finish = threading.Event()
        cls.original_explorer = app.EXPLORER
        def transport(payload):
            context = json.loads(payload["messages"][1]["content"])
            goal = context["goal"]
            count = len(context["recent"])
            if goal == "UI files":
                if count == 0:
                    response = answer("write_file", path="work/live.txt", content="first version")
                elif count == 1:
                    cls.release_file_edit.wait(15)
                    response = answer("write_file", path="work/live.txt", content="second version is longer")
                elif count == 2:
                    cls.release_file_delete.wait(15)
                    response = answer("run_command", command="del /Q work\\live.txt")
                else:
                    cls.release_file_finish.wait(15)
                    response = answer("finish", reason="file lifecycle observed")
            elif count == 0:
                response = answer("write_file", path="work/note.txt", content=f"{goal} {ATTACK_TEXT}")
            elif count == 1:
                if goal == "UI start": cls.release_start.wait(15)
                if goal == "UI B": cls.release_b.wait(15)
                command = "for /L %i in (1,1,300) do @echo LINE-%i" if goal == "UI D" else "type work\\note.txt"
                response = answer("run_command", command=command)
            elif count == 2 and goal == "UI D":
                cls.release_more.wait(15)
                response = answer("run_command", command="echo LATE-OUTPUT")
            else:
                response = answer("finish", reason="observed")
            yield response
            yield {"done": True, "done_reason": "stop"}
        cls.transport = transport
        cls.models_patch = patch.object(app, "get_ollama_models", return_value=[{"name": "mock:model", "size": 1024}])
        cls.lab_patch = patch.object(app, "get_lab_status", return_value={
            "status": "ready", "message": "Mock Juice Shop ready", "ready": True, "can_start": False})
        cls.ollama_patch = patch.object(app, "urlopen", return_value=io.BytesIO(b'{"models":[]}'))
        cls.models_patch.start(); cls.lab_patch.start(); cls.ollama_patch.start()
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(5)
        cls.port = cls.sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=cls.port,
                                               log_level="error", lifespan="off"))
        cls.server = server
        cls.thread = threading.Thread(target=lambda: server.run(sockets=[cls.sock]), daemon=True)
        cls.thread.start()
        for _ in range(100):
            if server.started: break
            time.sleep(0.05)
        if not server.started: raise RuntimeError("UI test server did not start")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=installed_chromium())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.sock.close()
        cls.ollama_patch.stop(); cls.lab_patch.stop(); cls.models_patch.stop()
        app.EXPLORER = cls.original_explorer
        cls.temp.cleanup()

    def setUp(self):
        for event in (self.release_start, self.release_b, self.release_more,
                      self.release_file_edit, self.release_file_delete, self.release_file_finish): event.clear()
        self.explorer = ExplorerV3(Path(self.temp.name) / self._testMethodName,
                                   transport=type(self).transport, browser_enabled=False,
                                   lab_reset=lambda: {"verified": True, "probe": "mock"})
        app.EXPLORER = self.explorer
        self.context = self.browser.new_context(viewport={"width": 1300, "height": 850})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def tearDown(self):
        for event in (self.release_start, self.release_b, self.release_more,
                      self.release_file_edit, self.release_file_delete, self.release_file_finish): event.set()
        if self.explorer.worker and self.explorer.worker.is_alive(): self.explorer.worker.join(20)

    def wait_run(self, run_id):
        self.explorer.worker.join(20)
        self.assertFalse(self.explorer.worker.is_alive())
        metadata = self.explorer.get_run(run_id)
        self.assertEqual(metadata["phase"], "finished", f"{metadata!r}; snapshot={self.explorer.snapshot()!r}")

    def test_01_start_navigation_reload_and_tab_close_do_not_stop_run(self):
        page = self.page
        page.goto(self.base)
        page.locator("#startButton:not([disabled])").wait_for(timeout=10000)
        self.assertTrue(page.locator('#scenarioSelect option[value="auto"]').evaluate("item => item.disabled"))
        self.assertTrue(page.locator('#chainSelect option[value="campaign"]').evaluate("item => item.disabled"))
        page.locator("#goalInput").fill("UI start")
        starts = []
        def capture_start(route):
            response = route.fetch()
            starts.append(response.json())
            route.fulfill(response=response)
        page.route("**/api/start", capture_start)
        page.locator("#startButton").click()
        page.wait_for_url(f"{self.base}/runs/*")
        payload = starts[0]
        run_id = payload["run_id"]
        page.wait_for_url(f"{self.base}/runs/{run_id}")
        self.assertEqual(page.locator("#runId").inner_text(), run_id)
        self.assertTrue(self.explorer.worker.is_alive())
        duplicate = page.request.post(f"{self.base}/api/start", data=json.dumps({"goal":"duplicate","model":"mock:model"}),
                                      headers={"Content-Type":"application/json"})
        self.assertEqual(duplicate.status, 409)
        page.reload()
        self.assertEqual(page.locator("#runId").inner_text(), run_id)
        page.goto(self.base)
        self.assertTrue(page.locator("#startButton").is_disabled())
        self.assertEqual(page.locator("#currentRunLink").get_attribute("href"), f"/runs/{run_id}")
        page.go_back()
        self.assertEqual(page.locator("#runId").inner_text(), run_id)
        page.locator("#settingsTab").click()
        self.assertIn(run_id, page.locator("#settingsValue").inner_text())
        page.locator("#observeTab").click()
        page.close()
        self.assertTrue(self.explorer.worker.is_alive())
        self.release_start.set()
        self.wait_run(run_id)

        # A response can disappear after the server accepts the request.
        # The main page must recover the run_id from the run listing.
        self.release_start.clear()
        lost_page = self.context.new_page()
        lost_page.goto(self.base)
        lost_page.locator("#startButton:not([disabled])").wait_for(timeout=10000)
        lost_page.locator("#goalInput").fill("UI start")
        lost_ids = []
        def lose_response(route):
            lost_ids.append(route.fetch().json()["run_id"])
            route.abort("failed")
        lost_page.route("**/api/start", lose_response)
        lost_page.locator("#startButton").click()
        lost_page.wait_for_url(f"{self.base}/runs/*")
        self.assertEqual(lost_page.locator("#runId").inner_text(), lost_ids[0])
        self.assertEqual(len(self.explorer.store.list_runs()), 2)
        self.release_start.set()
        self.wait_run(lost_ids[0])
        self.assertFalse(self.page_errors)

    def test_02_archived_a_and_live_b_reconnect_and_stop_scope(self):
        a = self.explorer.start(goal="UI A", model="mock:model")
        self.wait_run(a)
        b = self.explorer.start(goal="UI B", model="mock:model")
        a_page = self.page
        b_page = self.context.new_page()
        a_page.goto(f"{self.base}/runs/{a}")
        b_page.goto(f"{self.base}/runs/{b}")
        b_page.locator("#connectionStatus", has_text="실시간 연결됨").wait_for(timeout=10000)
        self.assertEqual(a_page.locator("#connectionStatus").inner_text(), "저장 기록")
        self.assertEqual(a_page.locator("#currentRunLink").get_attribute("href"), f"/runs/{b}")
        self.assertEqual(a_page.request.post(f"{self.base}/api/runs/{a}/stop").status, 409)
        self.assertEqual(a_page.request.post(f"{self.base}/api/runs/{a}/reset").status, 409)
        self.assertTrue(self.explorer.worker.is_alive())
        b_page.reload()
        b_page.locator("#connectionStatus", has_text="실시간 연결됨").wait_for(timeout=10000)
        self.release_b.set()
        self.wait_run(b)
        b_page.locator("#consoleOutput", has_text="UI B").wait_for(timeout=10000)
        b_page.locator("#connectionStatus", has_text="저장 기록").wait_for(timeout=10000)
        self.assertNotIn("UI B", a_page.locator("#consoleOutput").inner_text())
        before = b_page.locator("#timeline .timeline-row").count()
        saved_events = self.explorer.store.read_events(b, limit=1000)
        b_page.reload()
        b_page.locator("#timeline .timeline-row").first.wait_for()
        self.assertEqual(b_page.locator("#timeline .timeline-row").count(), before)
        self.assertEqual(sum(item["kind"] not in {"model_delta", "exec_output", "worker_output"}
                             for item in saved_events), before)

        # Recreate the controller from disk to exercise the app restart path.
        restarted = ExplorerV3(self.explorer.store.root, transport=type(self).transport,
                               browser_enabled=False, lab_reset=lambda: {"verified": True, "probe": "mock"})
        app.EXPLORER = restarted
        self.explorer = restarted
        b_page.reload()
        b_page.locator("#timeline .timeline-row").first.wait_for()
        self.assertEqual(b_page.locator("#timeline .timeline-row").count(), before)
        self.assertEqual(b_page.locator("#connectionStatus").inner_text(), "저장 기록")
        exported = b_page.request.get(f"{self.base}/api/runs/{b}/export")
        self.assertEqual(exported.status, 200)
        self.assertIn("application/zip", exported.headers["content-type"])
        with zipfile.ZipFile(io.BytesIO(exported.body())) as archive:
            self.assertIn("events.jsonl", archive.namelist())
            self.assertIn("session.json", archive.namelist())
        self.assertEqual(b_page.request.get(f"{self.base}/api/runs/bad/events").status, 400)
        self.assertFalse(self.page_errors)

    def test_03_file_preview_console_search_and_xss_text(self):
        run_id = self.explorer.start(goal="UI XSS", model="mock:model")
        self.wait_run(run_id)
        artifacts = self.explorer.store.root / run_id / "artifacts"
        (artifacts / "binary.bin").write_bytes(b"\x00\x01")
        (artifacts / "big.txt").write_text("B" * 140000, encoding="utf-8")
        page = self.page
        page.goto(f"{self.base}/runs/{run_id}")
        page.locator("#fileList .file-item", has_text="note-").click()
        page.locator("#filePreview", has_text="<script>").wait_for()
        self.assertEqual(page.evaluate("window.__xss"), None)
        self.assertEqual(page.locator("#filePreview script, #filePreview img").count(), 0)
        page.locator("#consoleSearch").fill("window.__xss")
        self.assertGreater(page.locator("#consoleOutput mark").count(), 0)
        self.assertEqual(page.evaluate("window.__xss"), None)
        self.assertEqual(page.locator("#timeline script, #timeline img").count(), 0)
        page.locator("#timeline .timeline-row", has_text="exec_finished").last.click()
        self.assertIn("EXEC-0002", page.locator("#executionText").inner_text())
        self.assertIn("CALL-0002", page.locator("#executionText").inner_text())
        page.locator("#openCodeButton").click()
        page.locator("#filePreview", has_text="type work").wait_for()
        page.locator("#evidenceTab").click()
        page.locator("#evidenceFiles .file-item", has_text="note-").click()
        page.locator("#evidencePreview", has_text="<img").wait_for()
        self.assertEqual(page.evaluate("window.__xss"), None)
        page.locator("#evidenceFiles .file-item", has_text="binary.bin").click()
        page.locator("#evidencePreview", has_text="바이너리 파일").wait_for()
        page.locator("#evidenceFiles .file-item", has_text="big.txt").click()
        page.locator("#evidenceMeta", has_text="미리보기 잘림").wait_for()
        self.assertLessEqual(len(page.locator("#evidencePreview").inner_text()), 131072)
        malicious = f'evil<img src=x onerror="window.__xss=1">.txt'
        def fake_names(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "run_id":run_id,"source":"artifacts","available":True,
                "files":[{"path":malicious,"kind":"file","size":4,"modified_at":1}]}))
        page.route(f"**/api/runs/{run_id}/files?source=artifacts", fake_names)
        page.reload()
        page.locator("#fileList .file-item", has_text="evil<img").wait_for()
        self.assertEqual(page.locator("#fileList img").count(), 0)
        self.assertEqual(page.evaluate("window.__xss"), None)

        # Live file writes refresh the selected preview; a CMD deletion is
        # picked up by the bounded runtime file poll while the run stays live.
        live_id = self.explorer.start(goal="UI files", model="mock:model")
        page.goto(f"{self.base}/runs/{live_id}")
        page.locator("#fileList .file-item", has_text="live.txt").click(timeout=10000)
        page.locator("#filePreview", has_text="first version").wait_for()
        self.release_file_edit.set()
        page.locator("#filePreview", has_text="second version is longer").wait_for(timeout=10000)
        self.release_file_delete.set()
        page.locator("#fileMeta", has_text="삭제됨 또는 미보존").wait_for(timeout=10000)
        self.assertEqual(page.locator("#fileList .file-item", has_text="live.txt").count(), 0)
        self.release_file_finish.set()
        self.wait_run(live_id)
        page.locator("#fileHeading", has_text="보존된 코드·증거").wait_for(timeout=10000)
        self.assertFalse(self.page_errors)

    def test_04_narrow_layout_keyboard_and_console_scroll(self):
        run_id = self.explorer.start(goal="UI D", model="mock:model")
        page = self.page
        page.set_viewport_size({"width":390,"height":780})
        page.goto(f"{self.base}/runs/{run_id}")
        page.locator("#consoleOutput", has_text="LINE-200").wait_for(timeout=10000)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        page.locator("#menuToggle").click()
        self.assertTrue(page.locator("#sidebar").evaluate("item => item.classList.contains('open')"))
        page.locator("#historyLink").focus()
        self.assertEqual(page.evaluate("document.activeElement.id"), "historyLink")
        page.locator("#menuToggle").click()
        page.locator("#settingsTab").click()
        self.assertTrue(page.locator("#settingsPanel").is_visible())
        page.locator("#observeTab").click()
        page.locator("#wrapToggle").uncheck()
        self.assertFalse(page.locator("#consoleOutput").evaluate("item => item.classList.contains('wrap')"))
        page.locator("#wrapToggle").check()
        console = page.locator("#consoleOutput")
        console.evaluate("item => item.scrollTop=0")
        page.locator("#autoScrollToggle").uncheck()
        before = console.evaluate("item => item.scrollTop")
        self.release_more.set()
        self.wait_run(run_id)
        console.get_by_text("LATE-OUTPUT").wait_for(timeout=10000)
        self.assertEqual(console.evaluate("item => item.scrollTop"), before)
        self.assertTrue(page.locator("#newOutputButton").is_visible())
        page.locator("#newOutputButton").click()
        self.assertTrue(page.locator("#autoScrollToggle").is_checked())
        self.assertFalse(self.page_errors)


if __name__ == "__main__":
    unittest.main()

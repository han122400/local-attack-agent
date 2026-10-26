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
                    command = "del /Q work\\live.txt" if os.name == "nt" else "rm -f work/live.txt"
                    response = answer("run_command", command=command)
                else:
                    cls.release_file_finish.wait(15)
                    response = answer("finish", reason="file lifecycle observed")
            elif count == 0:
                response = answer("write_file", path="work/note.txt", content=f"{goal} {ATTACK_TEXT}")
            elif count == 1:
                if goal == "UI start": cls.release_start.wait(15)
                if goal == "UI B": cls.release_b.wait(15)
                if goal == "UI D":
                    command = "for /L %i in (1,1,300) do @echo LINE-%i" if os.name == "nt" else "seq 1 300"
                else:
                    command = "type work\\note.txt" if os.name == "nt" else "cat work/note.txt"
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
        cls.ollama_patch = patch.object(app, "urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b'{"models":[]}'))
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

    def test_openrouter_selection_and_start_work_without_ollama(self):
        models = [{"name": "openrouter:vendor/test-model", "id": "vendor/test-model", "label": "Remote test",
                   "provider": "openrouter", "context_length": 4096, "max_completion_tokens": 2048,
                   "supported_parameters": ["structured_outputs", "max_tokens"], "pricing": {}}]
        with patch.object(app, "get_ollama_models", side_effect=RuntimeError("Ollama offline")), \
                patch.object(app, "get_openrouter_models", return_value=models):
            page = self.page
            page.goto(self.base)
            page.locator("#providerSelect").select_option("openrouter")
            page.locator("#startButton:not([disabled])").wait_for(timeout=10000)
            self.assertEqual(page.locator("#modelSelect").input_value(), "openrouter:vendor/test-model")
            self.assertIn("외부", page.locator("#remoteModelNotice").inner_text())
            self.assertTrue(page.locator("#remoteModelNotice").is_visible())
            self.assertEqual(page.locator("#thinkSelect").input_value(), "null")
            self.assertTrue(page.locator("#thinkSelect").is_disabled())
            self.assertEqual(page.locator("#contextInput").input_value(), "4096")
            self.assertEqual(page.locator("#predictInput").input_value(), "2048")
            page.locator("#goalInput").fill("UI remote")
            page.locator("#startButton").click()
            page.wait_for_url("**/runs/*")
            run_id = page.url.rsplit("/", 1)[1]
            self.wait_run(run_id)
            self.assertEqual(self.explorer.get_run(run_id)["model"], "openrouter:vendor/test-model")
            page.wait_for_function("document.getElementById('runModel').textContent === 'openrouter:vendor/test-model'")
            self.assertEqual(page.locator("#sideProviderLabel").inner_text(), "OpenRouter")
            self.assertEqual(self.page_errors, [])

    def test_missing_openrouter_key_does_not_block_switching_back_to_ollama(self):
        with patch.object(app, "get_openrouter_models", side_effect=RuntimeError("OPENROUTER_API_KEY 설정 필요")):
            page = self.page
            page.goto(self.base)
            page.locator("#startButton:not([disabled])").wait_for(timeout=10000)
            page.locator("#providerSelect").select_option("openrouter")
            page.wait_for_function("document.getElementById('modelStatus').textContent.includes('OPENROUTER_API_KEY')")
            self.assertTrue(page.locator("#startButton").is_disabled())
            page.locator("#providerSelect").select_option("ollama")
            page.locator("#startButton:not([disabled])").wait_for(timeout=10000)
            self.assertEqual(page.locator("#modelSelect").input_value(), "mock:model")
            self.assertTrue(page.locator("#remoteModelNotice").is_hidden())

    def test_model_search_preserves_selection_and_starts_the_explicitly_chosen_model(self):
        # This scenario verifies selection and request routing, without unrelated command execution.
        self.explorer.transport = lambda _payload: iter([answer("finish")])
        models = [{"name": f"openrouter:{model_id}", "id": model_id, "label": label,
                   "context_length": 32768, "max_completion_tokens": 8192,
                   "supported_parameters": ["temperature"]}
                  for model_id, label in [("anthropic/sonnet", "Sonnet"), ("openai/pro", "Pro"),
                                          ("openai/mini", "Mini"), ("google/flash", "Flash")]]
        with patch.object(app, "get_openrouter_models", return_value=models):
            page = self.page
            page.goto(self.base)
            page.locator("#providerSelect").select_option("openrouter")
            page.locator("#startButton:not([disabled])").wait_for(timeout=10000)
            search = page.locator("#modelSearch")
            search.fill("OpEnAi pro")
            self.assertEqual(page.locator("#modelSelect option").count(), 1)
            self.assertIn("1 / 4", page.locator("#modelSearchCount").inner_text())
            # Filtering must not silently replace the model already selected for the run.
            self.assertIn("anthropic/sonnet", page.locator("#selectedModelId").inner_text())
            search.press("Enter")
            self.assertIn("openai/pro", page.locator("#selectedModelId").inner_text())
            self.assertTrue(page.url.endswith("/"))
            page.locator("#refreshModelsButton").click()
            page.locator("#modelSelect:not([disabled])").wait_for()
            self.assertEqual(search.input_value(), "OpEnAi pro")
            self.assertEqual(page.locator("#modelSelect").input_value(), "openrouter:openai/pro")
            search.fill("does-not-exist")
            self.assertTrue(page.locator("#modelSearchEmpty").is_visible())
            self.assertIn("openai/pro", page.locator("#selectedModelId").inner_text())
            self.assertTrue(page.locator("#startButton").is_enabled())
            search.press("Escape")
            self.assertEqual(page.locator("#modelSelect option").count(), 4)
            page.set_viewport_size({"width": 390, "height": 780})
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            page.locator("#goalInput").fill("UI remote search")
            page.locator("#startButton").click()
            page.wait_for_url("**/runs/*")
            run_id = page.url.rsplit("/", 1)[1]
            self.wait_run(run_id)
            self.assertEqual(self.explorer.get_run(run_id)["model"], "openrouter:openai/pro")
            self.assertEqual(self.page_errors, [])

    def test_model_prices_and_independent_ollama_sidebar_status(self):
        models = [{"name": "openrouter:vendor/paid", "id": "vendor/paid", "label": "Paid model",
                   "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
                  {"name": "openrouter:vendor/free", "id": "vendor/free", "label": "Free model",
                   "pricing": {"prompt": "0", "completion": "0"}},
                  {"name": "openrouter:vendor/unknown", "id": "vendor/unknown", "label": "Unknown model",
                   "pricing": {"prompt": None, "completion": "-1"}}]
        with patch.object(app, "get_openrouter_models", return_value=models):
            page = self.page
            page.goto(self.base)
            page.locator("#providerSelect").select_option("openrouter")
            page.locator("#modelSelect:not([disabled])").wait_for()
            self.assertEqual(page.locator("#selectedInputPrice").inner_text(), "$2.50")
            self.assertEqual(page.locator("#selectedOutputPrice").inner_text(), "$10.00")
            self.assertIn("100만 토큰", page.locator("#modelPriceUnit").inner_text())
            paid = page.locator('#modelSelect option[value="openrouter:vendor/paid"]').inner_text()
            self.assertIn("입력 $2.50", paid)
            self.assertIn("출력 $10.00", paid)
            self.assertEqual(page.locator("#sideProviderStatus").inner_text(), "연결됨")
            self.assertEqual(page.locator("#sideOllamaLabel").inner_text(), "Ollama")
            page.wait_for_function("document.getElementById('sideOllama').textContent === '연결됨'")
            self.assertEqual(page.locator("#sideOllama").inner_text(), "연결됨")
            page.locator("#modelSelect").select_option("openrouter:vendor/free")
            self.assertEqual(page.locator("#selectedInputPrice").inner_text(), "$0.00")
            self.assertEqual(page.locator("#selectedOutputPrice").inner_text(), "$0.00")
            page.locator("#modelSelect").select_option("openrouter:vendor/unknown")
            self.assertEqual(page.locator("#selectedInputPrice").inner_text(), "미제공")
            self.assertEqual(page.locator("#selectedOutputPrice").inner_text(), "변동 요금")
            # Route interception isolates the connection states without an actual Ollama instance.
            pending = []
            page.route("**/api/ollama/ps", lambda route: pending.append(route))
            page.locator("#refreshModelsButton").click()
            page.locator("#modelSelect:not([disabled])").wait_for()
            self.assertTrue(page.locator("#startButton").is_enabled())
            self.assertTrue(pending)
            for route in pending:
                route.fulfill(status=503, content_type="application/json", body='{"detail":"Ollama offline"}')
            page.wait_for_function("document.getElementById('sideOllama').textContent === '연결 실패'")
            page.locator("#modelSelect:not([disabled])").wait_for()
            self.assertEqual(page.locator("#sideProviderStatus").inner_text(), "연결됨")
            self.assertTrue(page.locator("#startButton").is_enabled())
            page.unroute("**/api/ollama/ps")
            page.locator("#providerSelect").select_option("ollama")
            page.locator("#modelSelect:not([disabled])").wait_for()
            self.assertTrue(page.locator("#selectedModelPricing").is_hidden())
            self.assertIn("API 토큰 과금 없음", page.locator("#localModelPriceNote").inner_text())
            self.assertEqual(self.page_errors, [])

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
        page.locator("#runId", has_text=run_id).wait_for()
        self.assertEqual(page.locator("#runId").inner_text(), run_id)
        self.assertTrue(self.explorer.worker.is_alive())
        duplicate = page.request.post(f"{self.base}/api/start", data=json.dumps({"goal":"duplicate","model":"mock:model"}),
                                      headers={"Content-Type":"application/json"})
        self.assertEqual(duplicate.status, 409)
        page.reload()
        page.locator("#runId", has_text=run_id).wait_for()
        self.assertEqual(page.locator("#runId").inner_text(), run_id)
        page.goto(self.base)
        page.locator(f'#currentRunLink[href="/runs/{run_id}"]').wait_for()
        self.assertTrue(page.locator("#startButton").is_disabled())
        self.assertEqual(page.locator("#currentRunLink").get_attribute("href"), f"/runs/{run_id}")
        page.go_back()
        page.locator("#runId", has_text=run_id).wait_for()
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
        lost_page.locator("#runId", has_text=lost_ids[0]).wait_for()
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
        self.assertEqual(sum(item["kind"] != "model_delta"
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
        page.locator('#timeline .timeline-row[data-kind="exec_finished"]').last.click()
        self.assertIn("EXEC-0002", page.locator("#executionText").inner_text())
        self.assertIn("CALL-0002", page.locator("#executionText").inner_text())
        page.locator("#openCodeButton").click()
        page.locator("#filePreview", has_text="type work" if os.name == "nt" else "cat work").wait_for()
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
        # Scrolling up disables auto-follow asynchronously; wait for that event
        # instead of clicking a checkbox while its handler is changing state.
        page.wait_for_function("!document.getElementById('autoScrollToggle').checked")
        before = console.evaluate("item => item.scrollTop")
        self.release_more.set()
        self.wait_run(run_id)
        console.get_by_text("LATE-OUTPUT").wait_for(timeout=10000)
        self.assertEqual(console.evaluate("item => item.scrollTop"), before)
        self.assertTrue(page.locator("#newOutputButton").is_visible())
        page.locator("#newOutputButton").click()
        self.assertTrue(page.locator("#autoScrollToggle").is_checked())
        self.assertFalse(self.page_errors)

    def test_05_model_output_is_readable_and_failed_generation_is_not_execution(self):
        run_id = "e" * 32
        log = self.explorer.store.create(run_id, {
            "goal": "출력 가독성 확인", "model": "mock:model", "phase": "finished",
            "run_result": "error", "cleanup_status": "succeeded", "log_status": "finalized",
            "lab_reset_status": "succeeded", "finish_reason": "Model output_limit",
            "budgets": {"model_calls": 40, "tool_calls": 80}, "started_at": 1, "finished_at": 2})
        payload = {"action":"run_python", "plan":"페이지 제목을 관찰합니다", "reason":"실제 출력으로 확인합니다",
                   "code":"print('first')\nprint('second')", "path":"", "content":"", "command":""}
        raw = json.dumps(payload, ensure_ascii=False)
        log.append("model_request", {"call_id":"CALL-0001", "settings":{"options":{"num_predict":1024}}})
        for part in (raw[:35], raw[35:90], raw[90:]):
            log.append("model_delta", {"call_id":"CALL-0001", "channel":"content", "text":part})
        log.append("model_response", {"call_id":"CALL-0001", "output":raw, "done_reason":"stop", "eval_count":80})
        log.append("command_decoded", {"call_id":"CALL-0001", "action":"run_python", "plan":payload["plan"], "reason":payload["reason"]})
        log.append("exec_started", {"call_id":"CALL-0001", "exec_id":"EXEC-0001", "action":"run_python"})
        log.append("exec_output", {"call_id":"CALL-0001", "exec_id":"EXEC-0001", "channel":"stdout", "text":"실제 관찰 결과\n"})
        log.append("exec_finished", {"call_id":"CALL-0001", "exec_id":"EXEC-0001", "action":"run_python", "exit_code":0, "elapsed":0.5, "stdout":"실제 관찰 결과\n"})
        log.append("model_request", {"call_id":"CALL-0002", "settings":{"options":{"num_predict":1024}}})
        partial = '{"action":"run_python","plan":"다음 관찰","code":"print(\\"unfinished'
        log.append("model_delta", {"call_id":"CALL-0002", "channel":"content", "text":partial})
        log.append("model_response", {"call_id":"CALL-0002", "output":partial, "done_reason":"length", "eval_count":1024})
        log.append("model_error", {"call_id":"CALL-0002", "error_kind":"output_limit", "message":"Output limit reached (1024 tokens)"})
        file_reply = json.dumps({"action":"read_file", "plan":"저장 파일 확인", "reason":"내용 관찰",
                                 "path":"work/note.txt", "content":"", "command":"", "code":""})
        log.append("model_request", {"call_id":"CALL-0003"})
        log.append("model_response", {"call_id":"CALL-0003", "output":file_reply, "done_reason":"stop", "eval_count":25})
        log.append("command_decoded", {"call_id":"CALL-0003", "action":"read_file", "plan":"저장 파일 확인", "reason":"내용 관찰"})
        log.append("exec_started", {"call_id":"CALL-0003", "exec_id":"EXEC-0002", "action":"read_file"})
        log.append("exec_finished", {"call_id":"CALL-0003", "exec_id":"EXEC-0002", "action":"read_file",
                                     "exit_code":0, "elapsed":0.1, "stdout":"파일 읽기 실제 출력"})
        log.close()
        page = self.page
        page.goto(f"{self.base}/runs/{run_id}")
        page.locator("#decisionList", has_text="페이지 제목을 관찰합니다").wait_for(timeout=5000)
        readable = page.locator("#consoleOutput").inner_text()
        self.assertIn("print('first')\nprint('second')", readable)
        self.assertEqual(readable.count("실제 관찰 결과"), 1)
        self.assertIn("파일 읽기 실제 출력", readable)
        self.assertIn('print("unfinished', readable)
        self.assertNotIn('"action":', readable)
        self.assertIn("실행되지 않음", readable)
        self.assertIn("출력 한도", page.locator("#decisionList").inner_text())
        self.assertEqual("3", page.locator("#callMetric").inner_text())
        self.assertEqual("2", page.locator("#execMetric").inner_text())
        page.locator("#rawOutputToggle").check()
        page.locator("#consoleOutput", has_text='"action":').wait_for()
        self.assertIn('"action":', page.locator("#consoleOutput").inner_text())
        page.locator("#rawOutputToggle").uncheck()
        page.locator("#consoleOutput", has_text="Python 코드").wait_for()
        self.assertNotIn('"action":', page.locator("#consoleOutput").inner_text())
        self.assertFalse(self.page_errors)

    def test_06_failure_categories_metrics_filters_and_replay(self):
        run_id = "f" * 32
        log = self.explorer.store.create(run_id, {
            "goal": "분류 검증", "model": "mock:model", "phase": "finished", "run_result": "completed",
            "cleanup_status": "succeeded", "log_status": "finalized", "lab_reset_status": "succeeded",
            "budgets": {"model_calls": 40, "tool_calls": 80}, "started_at": 1, "finished_at": 2})
        results = [
            {"exit_code": 0, "stdout": "HTTP 404 and 403 are observations"},
            {"exit_code": 1, "failure_source": "generated_code", "cause_code": "python_exception",
             "exception_type": "SyntaxError", "exception": "SyntaxError: invalid code", "retryable": True},
            {"exit_code": None, "timed_out": True},
            {"exit_code": None, "cancelled": True},
            {"exit_code": 1, "failure_source": "system", "cause_code": "browser_worker_exited",
             "exception": "Worker connection closed", "retryable": True},
        ]
        for index, result in enumerate(results, 1):
            ids = {"call_id": f"CALL-{index:04d}", "exec_id": f"EXEC-{index:04d}", "action": "run_python"}
            log.append("model_request", {"call_id": ids["call_id"]})
            log.append("exec_started", ids)
            if result.get("stdout"):
                log.append("exec_output", {**ids, "channel": "stdout", "text": result["stdout"]})
            log.append("exec_finished", {**ids, **result})
        for index, error in enumerate(("format", "transport", "cancelled"), 6):
            log.append("model_request", {"call_id": f"CALL-{index:04d}"})
            log.append("model_error", {"call_id": f"CALL-{index:04d}", "error_kind": error, "message": error})
        log.append("run_finalized", {"run_result": "completed"})
        log.close()
        page = self.page
        page.goto(f"{self.base}/runs/{run_id}")
        page.locator('#timeline [data-kind="run_finalized"]').wait_for()
        for selector, expected in (("#execMetric", "5"), ("#modelFailureMetric", "1"), ("#systemErrorMetric", "2")):
            self.assertEqual(page.locator(selector).inner_text(), expected)
        self.assertIn("성공 1 · 실패 1 · 시간 초과 1 · 중단 1 · 시스템 문제 1", page.locator("#workBreakdown").inner_text())
        page.locator("#eventCategoryFilter").select_option("execution")
        self.assertEqual(page.locator('#timeline [data-category="system"]').count(), 0)
        page.locator('#timeline [data-kind="exec_finished"][data-outcome="failed"]').click()
        self.assertIn("python_exception", page.locator("#executionText").inner_text())
        self.assertIn("SyntaxError", page.locator("#executionText").inner_text())
        page.locator("#eventCategoryFilter").select_option("system")
        self.assertEqual(page.locator("#timeline .timeline-row").count(), 2)
        page.locator('#timeline [data-kind="exec_finished"]').click()
        self.assertIn("browser_worker_exited", page.locator("#executionText").inner_text())
        page.locator("#eventCategoryFilter").select_option("observation")
        self.assertIn("HTTP 404 and 403", page.locator("#timeline").inner_text())
        page.reload()
        page.locator('#timeline [data-kind="run_finalized"]').wait_for()
        self.assertEqual(page.locator("#systemErrorMetric").inner_text(), "2")
        screenshots = os.environ.get("V3_UI_SCREENSHOTS")
        for name, size in (("desktop", {"width":1440, "height":1000}), ("mobile", {"width":390, "height":844})):
            page.set_viewport_size(size)
            page.locator(".run-metrics").scroll_into_view_if_needed()
            if screenshots:
                page.screenshot(path=str(Path(screenshots) / f"event-categories-{name}.png"), animations="disabled")
            self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), size["width"])
        self.assertFalse(self.page_errors)


if __name__ == "__main__":
    unittest.main()

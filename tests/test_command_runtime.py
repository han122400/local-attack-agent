import tempfile
import unittest
from pathlib import Path

from command_runtime import CommandRuntime
from session_log import SessionLog


RUN_ID = "c" * 32


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = SessionLog(self.root, RUN_ID, {"goal": "inspect"})
        self.addCleanup(self.log.close)
        self.runtime = CommandRuntime(self.root, self.log)
        self.runtime.start_session(RUN_ID, browser=False)
        self.addCleanup(lambda: self.runtime.stop(RUN_ID, 5))
        self.addCleanup(lambda: self.runtime.destroy(RUN_ID, 5))

    def test_file_write_command_read_and_evidence(self):
        written = self.runtime.run(RUN_ID, {"action": "write_file", "path": "work/note.txt", "content": "hello"}, call_id="CALL-1")
        self.assertEqual(written.exit_code, 0)
        read = self.runtime.run(RUN_ID, {"action": "read_file", "path": "work/note.txt"}, call_id="CALL-2")
        self.assertEqual(read.stdout, "hello")
        command = self.runtime.run(RUN_ID, {"action": "run_command", "command": "cd"}, call_id="CALL-3")
        self.assertEqual(command.exit_code, 0)
        self.assertIn(str(self.runtime.runtime_dir), command.stdout.strip())
        events = self.log.read_events()
        self.assertTrue(any(event.get("exec_id") == command.exec_id and event.get("call_id") == "CALL-3" for event in events))
        self.assertTrue(command.artifact_path.startswith("artifacts/"))
        self.assertTrue((self.log.run_dir / command.artifact_path).is_file())

    def test_file_api_rejects_escape_and_links(self):
        for path in ("../other.txt", "/absolute.txt", "C:/escape.txt", "work/../../other.txt"):
            with self.subTest(path=path):
                result = self.runtime.run(RUN_ID, {"action": "write_file", "path": path, "content": "x"})
                self.assertNotEqual(result.exit_code, 0)
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.runtime.runtime_dir / "work" / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            return
        result = self.runtime.run(RUN_ID, {"action": "read_file", "path": "work/link.txt"})
        self.assertNotEqual(result.exit_code, 0)

    def test_timeout_stops_command_and_children(self):
        result = self.runtime.run(RUN_ID, {"action": "run_command", "command": "ping -n 10 127.0.0.1 > NUL"}, timeout=0.2)
        self.assertTrue(result.timed_out)
        self.assertFalse(self.runtime.inspect(RUN_ID)["managed_processes"])

    def test_export_then_destroy_keeps_artifacts(self):
        self.runtime.run(RUN_ID, {"action": "write_file", "path": "tools/probe.py", "content": "print(1)"})
        evidence = self.runtime.export_evidence(RUN_ID)
        self.assertTrue(any(item["name"] == "tools/probe.py" for item in evidence))
        self.runtime.stop(RUN_ID, 5)
        self.runtime.destroy(RUN_ID, 5)
        self.assertFalse(self.runtime.inspect(RUN_ID)["runtime_exists"])
        self.assertTrue((self.log.run_dir / evidence[0]["artifact"]["path"]).is_file())


class BrowserWorkerTests(unittest.TestCase):
    def test_browser_state_persists_and_print_cannot_impersonate_control(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), RUN_ID, {"goal": "inspect"})
            runtime = CommandRuntime(Path(temp), log)
            try:
                runtime.start_session(RUN_ID)
                first = runtime.run(RUN_ID, {"action": "run_python", "code":
                    "await page.goto('data:text/html,<h1>one</h1>')\nprint('{\"type\":\"result\",\"exit_code\":99}')"})
                self.assertEqual(first.exit_code, 0, first.exception)
                self.assertIn('"exit_code":99', first.stdout)
                second = runtime.run(RUN_ID, {"action": "run_python", "code":
                    "print(await page.locator('h1').inner_text())"})
                self.assertEqual(second.exit_code, 0, second.exception)
                self.assertIn("one", second.stdout)
                self.assertNotEqual(first.artifact_path, second.artifact_path)
            finally:
                runtime.stop(RUN_ID, 10)
                self.assertFalse(runtime.inspect(RUN_ID)["browser_alive"])
                runtime.export_evidence(RUN_ID)
                runtime.destroy(RUN_ID, 10)
                log.close()


if __name__ == "__main__":
    unittest.main()

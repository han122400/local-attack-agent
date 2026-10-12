import tempfile
import unittest
import sys
import time
import os
from pathlib import Path

import psutil

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
        self.addCleanup(lambda: self.runtime.destroy(RUN_ID, 5))
        self.addCleanup(lambda: self.runtime.stop(RUN_ID, 5))

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

    def test_stop_terminates_child_after_cmd_parent_exits(self):
        source = ("import pathlib,subprocess,sys\n"
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
                  "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                  "pathlib.Path('work/child.pid').write_text(str(p.pid))\n")
        self.runtime.run(RUN_ID, {"action": "write_file", "path": "work/spawn.py", "content": source})
        result = self.runtime.run(RUN_ID, {"action": "run_command",
                                           "command": f'{sys.executable} work\\spawn.py'})
        self.assertEqual(result.exit_code, 0, result.stderr)
        child_pid = int((self.runtime.runtime_dir / "work" / "child.pid").read_text())
        self.assertTrue(psutil.pid_exists(child_pid))
        self.runtime.stop(RUN_ID, 5)
        for _ in range(20):
            if not psutil.pid_exists(child_pid):
                break
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(child_pid))
        self.assertFalse(self.runtime.inspect(RUN_ID)["managed_processes"])

    def test_reused_pid_identity_is_not_terminated(self):
        own = psutil.Process(os.getpid())
        self.runtime.restore_owned(RUN_ID, [{"run_id": RUN_ID, "pid": own.pid,
                                             "created_at": own.create_time() - 100,
                                             "role": "command", "exec_id": "EXEC-OLD"}])
        with self.assertRaisesRegex(RuntimeError, "PID was reused"):
            self.runtime.stop(RUN_ID, 2)
        self.assertTrue(psutil.pid_exists(own.pid))
        self.assertTrue(self.runtime.inspect(RUN_ID)["managed_processes"])
        self.runtime.restore_owned(RUN_ID, [])

    def test_export_then_destroy_keeps_artifacts(self):
        self.runtime.run(RUN_ID, {"action": "write_file", "path": "tools/probe.py", "content": "print(1)"})
        evidence = self.runtime.export_evidence(RUN_ID)
        self.assertTrue(any(item["name"] == "tools/probe.py" for item in evidence))
        self.runtime.stop(RUN_ID, 5)
        self.runtime.destroy(RUN_ID, 5)
        self.assertFalse(self.runtime.inspect(RUN_ID)["runtime_exists"])
        self.assertTrue((self.log.run_dir / evidence[0]["artifact"]["path"]).is_file())


class BrowserWorkerTests(unittest.TestCase):
    def test_new_session_has_no_prior_cookie_or_runtime_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            first_id, second_id = "1" * 32, "2" * 32
            first_log = SessionLog(Path(temp), first_id, {"goal": "first"})
            first = CommandRuntime(Path(temp), first_log)
            first.start_session(first_id)
            try:
                cookie = first.run(first_id, {"action": "run_python", "code":
                    "await context.add_cookies([{'name':'v3','value':'first',"
                    "'url':'http://127.0.0.1:3000/'}])\nprint(len(await context.cookies()))"})
                self.assertIn("1", cookie.stdout)
                first.run(first_id, {"action": "write_file", "path": "tools/private.py", "content": "secret"})
            finally:
                first.stop(first_id, 10)
                first.export_evidence(first_id)
                first.destroy(first_id, 10)
                first_log.close()
            second_log = SessionLog(Path(temp), second_id, {"goal": "second"})
            second = CommandRuntime(Path(temp), second_log)
            second.start_session(second_id)
            try:
                no_cookie = second.run(second_id, {"action": "run_python", "code":
                    "print(len(await context.cookies()))"})
                self.assertEqual(no_cookie.stdout.strip(), "0")
                self.assertFalse((second.runtime_dir / "tools" / "private.py").exists())
            finally:
                second.stop(second_id, 10)
                second.export_evidence(second_id)
                second.destroy(second_id, 10)
                second_log.close()

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
                large = runtime.run(RUN_ID, {"action": "run_python", "code":
                    "import sys\nprint('O'*120000)\nprint('E'*120000,file=sys.stderr)"}, call_id="CALL-LARGE")
                self.assertEqual(large.exit_code, 0, large.exception)
                self.assertTrue(large.output_truncated)
                chunks = [item for item in log.read_events(limit=1000)
                          if item["kind"] == "exec_output" and item["exec_id"] == large.exec_id]
                self.assertTrue(chunks)
                self.assertTrue(all(item["call_id"] == "CALL-LARGE" for item in chunks))
                self.assertGreaterEqual(sum(len(item["text"]) for item in chunks if item["channel"] == "stdout"), 120000)
                self.assertGreaterEqual(sum(len(item["text"]) for item in chunks if item["channel"] == "stderr"), 120000)
            finally:
                runtime.stop(RUN_ID, 10)
                self.assertFalse(runtime.inspect(RUN_ID)["browser_alive"])
                runtime.export_evidence(RUN_ID)
                runtime.destroy(RUN_ID, 10)
                log.close()


if __name__ == "__main__":
    unittest.main()

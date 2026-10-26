"""Local execution contracts; no browser, Docker, or live model is launched."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from command_runtime import CommandRuntime
from session_log import SessionLog


RUN_ID = "d" * 32


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log = SessionLog(Path(temporary.name), RUN_ID, {})
        self.addCleanup(self.log.close)
        self.runtime = CommandRuntime(Path(temporary.name), self.log)
        self.runtime.start_session(RUN_ID, browser=False)
        self.addCleanup(lambda: self.runtime.destroy(RUN_ID, 5))
        self.addCleanup(lambda: self.runtime.stop(RUN_ID, 5))

    def test_local_command_runs_without_docker_and_uses_session_directory(self):
        probe = self.runtime.runtime_dir / "work" / "probe.py"
        probe.write_text("import os\nprint('LOCAL_RUNTIME_OK')\nprint(os.getcwd())\n", encoding="utf-8")
        arguments = [sys.executable, "work/probe.py"]
        command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
        with patch("shutil.which", return_value=None):
            result = self.runtime.run(RUN_ID, {"action": "run_command", "command": command})
        self.assertEqual(result.exit_code, 0, result.exception or result.stderr)
        self.assertIn("LOCAL_RUNTIME_OK", result.stdout)
        self.assertIn(str(self.runtime.runtime_dir), result.stdout)
        self.runtime.stop(RUN_ID, 5)
        self.assertEqual(self.runtime.inspect(RUN_ID)["managed_processes"], [])

    def test_runtime_files_work_without_posix_directory_handles(self):
        with patch.object(os, "supports_dir_fd", set()):
            written = self.runtime.run(RUN_ID, {
                "action": "write_file", "path": "work/result.txt", "content": "local observation"})
            self.assertEqual(written.exit_code, 0, written.exception)
            read = self.runtime.run(RUN_ID, {"action": "read_file", "path": "work/result.txt"})
        self.assertEqual(read.exit_code, 0, read.exception)
        self.assertEqual(read.stdout, "local observation")


if __name__ == "__main__":
    unittest.main()

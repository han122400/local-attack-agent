import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import a5_benchmark
import command_runtime
import model_providers
import planning
from command_runtime import CommandRuntime
from session_log import SessionLog


class ProjectPathTests(unittest.TestCase):
    def test_paths_and_prompt_are_independent_of_working_directory(self):
        from engine.paths import PROJECT_ROOT, RUNS_ROOT, PROMPTS_DIR, ENV_PATH, DEPS_DIR, BENCHMARKS_DIR
        root = Path(__file__).resolve().parent.parent
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                self.assertEqual(root, PROJECT_ROOT)
                self.assertEqual(root / 'runs', RUNS_ROOT)
                self.assertEqual(root / 'prompts' / 'system', PROMPTS_DIR)
                self.assertEqual(root / '.env', ENV_PATH)
                self.assertEqual(root / '.deps', DEPS_DIR)
                self.assertEqual(root / 'benchmarks', BENCHMARKS_DIR)
                self.assertEqual(ENV_PATH, model_providers.ENV_PATH)
                self.assertEqual(BENCHMARKS_DIR / 'a5-pair-v2.json', a5_benchmark.CONFIG_PATH)
                self.assertTrue(planning.load_prompt('v4')[0])
            finally:
                os.chdir(original)

    def test_worker_receives_project_root_without_launching_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            log = SessionLog(Path(temp), 'c' * 32, {})
            self.addCleanup(log.close)
            runtime = CommandRuntime(Path(temp), log)
            (runtime.runtime_dir / 'tools').mkdir(parents=True)
            runtime.target = 'https://example.test/'
            server = MagicMock()
            server.getsockname.return_value = ('127.0.0.1', 12345)
            server.accept.return_value = (MagicMock(), None)
            process = MagicMock(pid=123)
            with patch.object(command_runtime.socket, 'socket', return_value=server), \
                 patch.object(command_runtime.subprocess, 'Popen', return_value=process) as popen, \
                 patch.object(command_runtime, 'installed_chromium', return_value=None), \
                 patch.object(command_runtime.uuid, 'uuid4') as uuid, \
                 patch.object(runtime, '_register'), patch.object(runtime, '_start_output_readers'), \
                 patch.object(runtime, '_capture_browser_tree'), \
                 patch.object(runtime, '_wait_control', side_effect=[{'type': 'hello', 'token': 'token'}, {'type': 'ready'}]):
                uuid.return_value.hex = 'token'
                runtime._start_browser_worker()
            self.assertEqual(str(Path(__file__).resolve().parent.parent), popen.call_args.args[0][7])


if __name__ == '__main__':
    unittest.main()

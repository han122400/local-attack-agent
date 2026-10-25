import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from process_sandbox import sandbox_command, remove_container
from command_runtime import CommandRuntime, WORKER_SOURCE
from session_log import SessionLog


RUN_ID = 'b' * 32
NAME = 'agent-' + RUN_ID + '-exec-0001'


class SandboxTests(unittest.TestCase):
    def test_no_docker_or_missing_image_never_falls_back_to_host(self):
        with patch('process_sandbox.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'host execution is disabled'):
                sandbox_command(NAME, RUN_ID, Path('/tmp/run'), ['sh', '-c', 'echo test'])
        with patch('process_sandbox.shutil.which', return_value='/docker'), \
                patch('process_sandbox.subprocess.run', side_effect=OSError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'Docker sandbox unavailable'):
                sandbox_command(NAME, RUN_ID, Path('/tmp/run'), ['sh', '-c', 'echo test'])

    def test_generated_code_gets_only_runtime_mount_and_no_network_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('process_sandbox.shutil.which', return_value='/docker'), \
                patch('process_sandbox.subprocess.run'):
            command = sandbox_command(NAME, RUN_ID, Path(directory), ['python', '-c', 'print(1)'])
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertIn('--read-only', command)
        self.assertEqual(command[command.index('--cap-drop') + 1], 'ALL')
        self.assertIn('no-new-privileges', command)
        self.assertEqual(command.count('--mount'), 1)
        self.assertTrue(command[command.index('--mount') + 1].endswith(',dst=/work'))
        self.assertNotIn('docker.sock', ' '.join(command))
        self.assertNotIn('OPENROUTER_API_KEY', command)

    def test_cleanup_checks_owned_label(self):
        with patch('process_sandbox.shutil.which', return_value='/docker'), \
                patch('process_sandbox.subprocess.run') as run:
            run.return_value.stdout = ''
            remove_container(NAME, RUN_ID)
            self.assertEqual(run.call_count, 1)
            self.assertIn('label=local-attack-agent.run=' + RUN_ID, run.call_args.args[0])
        with self.assertRaises(ValueError):
            remove_container('juice-lab', RUN_ID)

    def test_missing_sandbox_never_starts_a_host_process(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), RUN_ID, {})
            self.addCleanup(log.close)
            runtime = CommandRuntime(Path(directory), log)
            runtime.start_session(RUN_ID, browser=False)
            with patch('command_runtime.sandbox_command', side_effect=RuntimeError('sandbox unavailable')), \
                    patch('command_runtime.subprocess.Popen') as popen:
                result = runtime.run(RUN_ID, {'action': 'run_command', 'command': 'echo should-not-run'})
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn('sandbox unavailable', result.exception)
                with self.assertRaisesRegex(RuntimeError, 'sandbox unavailable'):
                    runtime._start_browser_worker()
                popen.assert_not_called()
            runtime.stop(RUN_ID)
            runtime.destroy(RUN_ID)

    def test_browser_worker_source_compiles(self):
        compile(WORKER_SOURCE, '_browser_worker.py', 'exec')

    def test_host_file_write_rejects_a_swapped_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = SessionLog(root, RUN_ID, {})
            self.addCleanup(log.close)
            runtime = CommandRuntime(root, log)
            runtime.start_session(RUN_ID, browser=False)
            outside = root / 'outside'
            outside.mkdir()
            canary = outside / 'canary.txt'
            canary.write_text('unchanged')
            original = runtime._file_path
            def swap(path, *, writing):
                result = original(path, writing=writing)
                (runtime.runtime_dir / 'work').rmdir()
                (runtime.runtime_dir / 'work').symlink_to(outside, target_is_directory=True)
                return result
            with patch.object(runtime, '_file_path', side_effect=swap):
                result = runtime.run(RUN_ID, {'action': 'write_file', 'path': 'work/canary.txt', 'content': 'changed'})
            self.assertNotEqual(result.exit_code, 0)
            self.assertEqual(canary.read_text(), 'unchanged')
            (runtime.runtime_dir / 'work').unlink()
            runtime.stop(RUN_ID)
            runtime.destroy(RUN_ID)

    def test_container_cleanup_is_required_even_without_host_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), RUN_ID, {'sandbox_containers': [NAME]})
            self.addCleanup(log.close)
            runtime = CommandRuntime(Path(directory), log)
            with patch('command_runtime.remove_container', side_effect=RuntimeError('Docker offline')):
                with self.assertRaisesRegex(RuntimeError, 'Docker offline'):
                    runtime.stop(RUN_ID)
            self.assertEqual(runtime.inspect(RUN_ID)['sandbox_containers'], [NAME])
            with self.assertRaisesRegex(RuntimeError, 'Managed processes remain'):
                runtime.destroy(RUN_ID)

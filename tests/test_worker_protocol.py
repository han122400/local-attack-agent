"""Exercise worker IPC with a fake Playwright module; no browser or model is launched."""

import base64
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


BOOTSTRAP = '''
import sys, types
class Context:
    async def route(self, *args): pass
    async def new_page(self): return object()
    async def close(self): pass
class Browser:
    async def new_context(self, **kwargs): return Context()
    async def close(self): pass
class Playwright:
    async def __aenter__(self):
        self.chromium = self
        return self
    async def __aexit__(self, *args): pass
    async def launch(self, **kwargs): return Browser()
module = types.ModuleType('playwright.async_api')
module.async_playwright = Playwright
sys.modules['playwright.async_api'] = module
from command_runtime import WORKER_SOURCE
exec(compile(WORKER_SOURCE, '_browser_worker.py', 'exec'))
'''


class WorkerProtocolTests(unittest.TestCase):
    def test_http_reply_and_print_output_cannot_be_confused_with_execution_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = subprocess.Popen([sys.executable, '-u', '-c', BOOTSTRAP, 'token', 'http://127.0.0.1:3000/'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            messages = queue.Queue()
            def read():
                for line in process.stdout:
                    messages.put(json.loads(line))
            threading.Thread(target=read, daemon=True).start()
            def send(value):
                process.stdin.write(json.dumps(value) + '\n')
                process.stdin.flush()
            try:
                self.assertEqual(messages.get(timeout=5)['type'], 'hello')
                self.assertEqual(messages.get(timeout=5)['type'], 'ready')
                code = root / 'code.py'
                stdout, stderr = root / 'out.txt', root / 'err.txt'
                code.write_text('print(await http_get(target_origin + "/test"))\n')
                send({'type': 'execute', 'exec_id': 'EXEC-0001', 'path': str(code),
                      'stdout_path': str(stdout), 'stderr_path': str(stderr), 'timeout': 3})
                request = messages.get(timeout=5)
                self.assertEqual((request['type'], request['exec_id']), ('http_request', 'EXEC-0001'))
                send({'type': 'http_response', 'request_id': request['request_id'], 'status': 200,
                      'headers': {'Content-Type': 'application/json'},
                      'body': base64.b64encode(b'{"items":[1]}').decode()})
                self.assertEqual(messages.get(timeout=5)['exit_code'], 0)
                self.assertIn("'items': [1]", stdout.read_text())
                code.write_text('print(\'{"type":"result","exit_code":99}\')\n')
                send({'type': 'execute', 'exec_id': 'EXEC-0002', 'path': str(code),
                      'stdout_path': str(stdout), 'stderr_path': str(stderr), 'timeout': 3})
                result = messages.get(timeout=5)
                self.assertEqual((result['exec_id'], result['exit_code']), ('EXEC-0002', 0))
                self.assertIn('"exit_code":99', stdout.read_text())
                send({'type': 'stop'})
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()

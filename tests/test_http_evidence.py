import hashlib
import base64
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from http_evidence import EvidenceBroker
from command_runtime import CommandRuntime
from explorer import compose_report
from scorecard import score_run
from session_log import SessionLog


class ProxyTests(unittest.TestCase):
    def setUp(self):
        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302 if self.path == '/redirect' else 200)
                self.send_header('Location', 'http://outside.invalid/' if self.path == '/redirect' else '/')
                self.end_headers()
                self.wfile.write(b'actual response')
            def log_message(self, *args): pass
        self.site = ThreadingHTTPServer(('127.0.0.1', 0), Site)
        self.site.daemon_threads = True
        thread = threading.Thread(target=self.site.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), 'a' * 32, {})
        self.addCleanup(self.log.close)
        self.target = f'http://127.0.0.1:{self.site.server_port}/'
        self.proxy = EvidenceBroker(self.target, self.log)

    def request(self, method, url):
        response = self.proxy.fetch(method, url)
        return response['status'], response['body']

    def test_only_actual_responses_produce_linked_artifacts(self):
        self.proxy.begin('EXEC-0001', 'CALL-0001')
        self.assertEqual(self.request('GET', self.target), (200, b'actual response'))
        records = self.proxy.end('EXEC-0001')
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record['exec_id'], record['call_id'], record['status']), ('EXEC-0001', 'CALL-0001', 200))
        artifact = record['response_artifact']
        saved = (self.log.run_dir / artifact['path']).read_bytes()
        self.assertEqual(saved, b'actual response')
        self.assertEqual(hashlib.sha256(saved).hexdigest(), artifact['sha256'])
        self.assertEqual(self.proxy.end('EXEC-0002'), [])

    def test_wrong_origin_credentials_writes_and_connect_are_blocked(self):
        self.proxy.begin('EXEC-0001', 'CALL-0001')
        for method, url in [('GET', 'http://outside.invalid/'), ('POST', self.target),
                            ('CONNECT', 'outside.invalid:443'),
                            ('GET', self.target.replace('://', '://user:pass@'))]:
            with self.subTest(method=method, url=url), self.assertRaises(PermissionError):
                self.request(method, url)
        self.assertEqual(self.proxy.end('EXEC-0001'), [])

    def test_redirect_is_observed_without_following_it(self):
        self.proxy.begin('EXEC-0001', 'CALL-0001')
        self.assertEqual(self.request('GET', self.target + 'redirect')[0], 302)
        self.assertEqual(len(self.proxy.end('EXEC-0001')), 1)

    def test_controller_handles_worker_http_but_rejects_stale_execution(self):
        runtime = CommandRuntime(Path(self.temp.name), self.log)
        runtime._evidence = self.proxy
        runtime._active_exec_id = 'EXEC-0001'
        runtime._conn = io.BytesIO()
        self.proxy.begin('EXEC-0001', 'CALL-0001')
        for exec_id, request_id in [('EXEC-OLD', 1), ('EXEC-0001', 2)]:
            runtime._control_queue.put({'type': 'http_request', 'exec_id': exec_id,
                                        'request_id': request_id, 'method': 'GET', 'url': self.target})
        runtime._control_queue.put({'type': 'result', 'exec_id': 'EXEC-0001', 'exit_code': 0})
        self.assertEqual(runtime._wait_control(3)['type'], 'result')
        replies = [json.loads(line) for line in runtime._conn.getvalue().splitlines()]
        self.assertIn('error', replies[0])
        self.assertEqual(base64.b64decode(replies[1]['body']), b'actual response')
        records = self.proxy.end('EXEC-0001')
        self.assertEqual(len(records), 1)
        finding = {'step': 1, 'http_evidence': records, 'evidence': True, 'status': 'ok',
                   'observed': 'FAKE CONFIRMED SQLi', 'claim': 'FAKE CONFIRMED SQLi'}
        report = compose_report([finding], 'completed', '')
        self.assertIn(records[0]['id'], report)
        self.assertNotIn('FAKE CONFIRMED SQLi', report)
        self.assertEqual(score_run({'findings': [finding], 'run_result': 'completed'}, self.log.run_dir)['evidence'], 1)


if __name__ == '__main__':
    unittest.main()

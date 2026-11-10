import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class EntrypointTests(unittest.TestCase):
    def test_scorecard_cli_forwards_arguments_and_exit_status(self):
        with patch('sys.argv', ['scorecard', '--json']), patch('engine.scorecard.main', return_value=7) as main:
            with self.assertRaises(SystemExit) as result:
                runpy.run_module('tools.scorecard', run_name='__main__')
        main.assert_called_once_with(['--json'])
        self.assertEqual(result.exception.code, 7)

    def test_root_entrypoint_exports_same_app_and_delegates_main(self):
        import app
        from web import app as web_app
        self.assertIs(app.app, web_app.app)
        with patch.object(web_app, 'main') as main:
            runpy.run_path('app.py', run_name='__main__')
        main.assert_called_once_with()

    def test_public_dashboard_resources_and_run_route(self):
        from web import app as web_app
        from engine.session_log import SessionStore
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            log = store.create('a' * 32, {'experiment_mode': 'v3'})
            log.close()
            with patch.object(web_app.EXPLORER, 'store', store):
                client = TestClient(web_app.app, base_url='http://127.0.0.1:8765')
                for path, content_type in [('/', 'text/html'), ('/app.js?v=6-separate-model-pickers', 'javascript'),
                                           ('/style.css?v=5-separate-model-pickers', 'text/css'),
                                           ('/model-settings-config.js?v=1-provider-settings', 'javascript'),
                                           ('/runs/' + 'a' * 32, 'text/html')]:
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(content_type, response.headers['content-type'])
                        self.assertTrue(response.content)

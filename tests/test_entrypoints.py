import runpy
import unittest
from unittest.mock import patch


class EntrypointTests(unittest.TestCase):
    def test_scorecard_cli_forwards_arguments_and_exit_status(self):
        with patch('sys.argv', ['scorecard', '--json']), patch('engine.scorecard.main', return_value=7) as main:
            with self.assertRaises(SystemExit) as result:
                runpy.run_module('tools.scorecard', run_name='__main__')
        main.assert_called_once_with(['--json'])
        self.assertEqual(result.exception.code, 7)

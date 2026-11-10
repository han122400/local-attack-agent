import builtins
import hashlib
import subprocess
import unittest
from unittest.mock import patch

from tools import check_v4_discovery as checker
import test_v4_loop as fixtures
from test_scorer_session import FakeAdapter


class BaselineImportTests(unittest.TestCase):
    def test_legacy_imports_are_scoped_to_baseline_module(self):
        from tools._legacy_imports import baseline_builtins
        original = builtins.__import__
        namespace = {'__builtins__': baseline_builtins()}
        exec('import harness\nfrom v4_tree import TreeStore\ndef get_budget():\n from explorer import context_char_budget\n return context_char_budget', namespace)
        from engine import harness, tree
        from orchestrator import context_char_budget
        self.assertIs(namespace['harness'], harness)
        self.assertIs(namespace['TreeStore'], tree.TreeStore)
        self.assertIs(namespace['get_budget'](), context_char_budget)
        self.assertIs(builtins.__import__, original)

    def test_historical_loop_loads_and_constructs_without_model_calls(self):
        ref = '306783e827d0c9007362d1a3a13ef6ce77581067'
        source = checker.git_bytes('show', ref + ':v4_loop.py')
        prompt = checker.git_bytes('show', ref + ':prompts/system/v4.txt').decode('utf-8')
        fixture = fixtures.LoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = checker.runner_type(source, ref, prompt)(fixture.log, fixtures.Runtime(fixture.log), scorer_adapter=FakeAdapter())
        self.addCleanup(runner.scorer.close)
        self.assertGreater(runner.context_budget, 0)

    def test_source_info_uses_available_commit_path_and_hashes_original_bytes(self):
        loop = b'original loop bytes'
        prompt = b'prompt bytes'
        def fake_git(*args):
            if args[:2] == ('rev-parse', 'HEAD'):
                return b'head\n'
            if args[0] == 'diff' or args[0] == 'ls-files':
                return b''
            if args[:2] == ('rev-parse', '--verify'):
                return b'abc123\n'
            if args == ('show', 'abc123:v4_loop.py'):
                raise subprocess.CalledProcessError(1, args)
            if args == ('show', 'abc123:strategies/greedy.py'):
                return loop
            if args == ('show', 'abc123:prompts/system/v4.txt'):
                return prompt
            raise AssertionError(args)
        with patch.object(checker, 'git_bytes', side_effect=fake_git):
            info, got_loop, got_prompt = checker.source_info('ref')
        self.assertEqual(got_loop, loop)
        self.assertEqual(got_prompt, 'prompt bytes')
        self.assertEqual(info['loop_sha256'], hashlib.sha256(loop).hexdigest())
        self.assertEqual(info['base_prompt_sha256'], hashlib.sha256(prompt).hexdigest())

    def test_source_info_reports_missing_loop(self):
        def fake_git(*args):
            if args[0] == 'show':
                raise subprocess.CalledProcessError(1, args)
            if args[0] == 'rev-parse':
                return b'abc123\n'
            return b''
        with patch.object(checker, 'git_bytes', side_effect=fake_git):
            with self.assertRaisesRegex(ValueError, 'no supported loop source'):
                checker.source_info('ref')


if __name__ == '__main__':
    unittest.main()

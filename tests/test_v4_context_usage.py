"""The run state exposes the v4 execution model's current context budget usage."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.session_log import SessionLog, SessionStore
from strategies.greedy import V4Runner
from test_scorer_session import FakeAdapter
from test_v4_loop import Runtime
from test_v4_tree import surface


class V4ContextUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = SessionLog(self.root, 'a' * 32, {
            'experiment_mode': 'v4', 'target': 'https://example.test/', 'goal': '문맥 검사',
            'model': 'mock:model', 'model_settings': {'think': False, 'options': {'num_ctx': 32768, 'num_predict': 4096}},
            'budgets': {'seconds': 120, 'model_calls': 30, 'tool_calls': 30, 'command_seconds': 10, 'finalize_seconds': 10},
            'scorer_provider': 'openrouter', 'scorer_model': 'typesafe/jev-1.14', 'scorer_settings': {}})
        self.addCleanup(self.log.close)
        self.seen = []

    def state_at_request(self, payload):
        state = SessionStore(self.root).read(self.log.run_id)
        count = sum(len(message['content']) for message in payload['messages'][1:])
        self.seen.append((count, state.get('context_chars'), state.get('context_budget')))
        return json.loads(payload['messages'][1]['content'])

    @staticmethod
    def command(context, discoveries=(), *, next_observation=''):
        result = {**context['result_template'], 'discovered': list(discoveries),
                  'next_observation': next_observation, 'result_path': context['attempt']['result_path']}
        return {'action': 'run_command', 'plan': 'inspect', 'reason': 'observe', 'path': '',
                'content': '', 'code': '', 'command': json.dumps(result, ensure_ascii=False)}

    def test_state_is_updated_before_each_node_request_and_retained_after_run(self):
        def transport(payload):
            context = self.state_at_request(payload)
            discoveries = [surface('https://example.test/search')] if context['node']['id'] == 'N0001' else []
            yield {'message': {'content': json.dumps(self.command(context, discoveries))}}
            yield {'done': True, 'done_reason': 'stop'}

        runner = V4Runner(self.log, Runtime(self.log), transport=transport, scorer_adapter=FakeAdapter())
        runner.context_budget = 40000
        self.assertEqual(runner.run(), ('completed', 'tree_exhausted'))
        self.assertEqual(len(self.seen), 2)
        for expected_chars, state_chars, state_budget in self.seen:
            self.assertEqual(state_chars, expected_chars)
            self.assertEqual(state_budget, 40000)
            self.assertGreater(state_chars, 0)
        self.assertNotEqual(self.seen[0][0], self.seen[1][0])
        saved = SessionStore(self.root).read(self.log.run_id)
        self.assertEqual(saved['context_chars'], self.seen[-1][0])
        self.assertNotIn('prompt_tokens', saved)
        # The existing dashboard polls this API; it must receive the same
        # values that were persisted by the real runner.
        import web.app as dashboard
        store = SessionStore(self.root)
        explorer = SimpleNamespace(store=store, get_run=store.read, active_run_id=None, worker=None)
        with patch.object(dashboard, 'EXPLORER', explorer):
            response = dashboard.run_state(self.log.run_id)
        body = json.loads(response.body)
        self.assertEqual((body['context_chars'], body['context_budget']), (self.seen[-1][0], 40000))

    def test_retry_counts_feedback_as_part_of_current_context(self):
        def transport(payload):
            context = self.state_at_request(payload)
            if len(self.seen) == 1:
                yield {'message': {'content': 'invalid JSON'}}
            else:
                yield {'message': {'content': json.dumps(self.command(context))}}
            yield {'done': True, 'done_reason': 'stop'}

        runner = V4Runner(self.log, Runtime(self.log), transport=transport, scorer_adapter=FakeAdapter())
        runner.context_budget = 40000
        runner.run()
        self.assertEqual(len(self.seen), 2)
        self.assertGreater(self.seen[1][0], self.seen[0][0])
        self.assertEqual([(a, c) for a, _, c in self.seen], [(b, 40000) for _, b, _ in self.seen])

    def test_repair_request_uses_its_modified_context(self):
        def transport(payload):
            context = self.state_at_request(payload)
            if 'instruction' not in context:
                cmd = self.command(context)
                result = json.loads(cmd['command'])
                result['discovered'] = None
                cmd['command'] = json.dumps(result)
            else:
                cmd = {'action': 'write_file', 'plan': 'repair', 'reason': 'submit result',
                       'path': context['attempt']['result_path'], 'content': json.dumps(context['result_template']),
                       'code': '', 'command': ''}
            yield {'message': {'content': json.dumps(cmd)}}
            yield {'done': True, 'done_reason': 'stop'}

        runner = V4Runner(self.log, Runtime(self.log), transport=transport, scorer_adapter=FakeAdapter())
        runner.context_budget = 40000
        runner.run()
        self.assertEqual(len(self.seen), 2)
        self.assertEqual([(a, c) for a, _, c in self.seen], [(b, 40000) for _, b, _ in self.seen])
        saved = SessionStore(self.root).read(self.log.run_id)
        self.assertEqual(saved['context_chars'], self.seen[-1][0])


if __name__ == '__main__':
    unittest.main()

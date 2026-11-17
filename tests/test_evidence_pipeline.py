"""Exercise discovery -> scoring -> observation -> candidate -> report offline."""
import json
import tempfile
import unittest
from pathlib import Path

from engine.reporting import save_report
from engine.risk_rubric import validate_answer
from engine.session_log import SessionLog
from engine.tree import TreeStore
from strategies.greedy import V4Runner
from test_v4_loop import Runtime
from test_v4_tree import surface
from test_scorer_session import FakeAdapter, good
from test_scorer_contract import answer


class EvidencePipelineTests(unittest.TestCase):
    def test_observed_login_is_scored_then_investigated_as_a_separate_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SessionLog(Path(directory), 'a' * 32, {
                'experiment_mode': 'v4', 'target': 'https://example.test/', 'goal': 'inspect',
                'model': 'mock:model', 'model_settings': {'think': False, 'options': {'num_ctx': 16384, 'num_predict': 4096}},
                'budgets': {'seconds': 120, 'model_calls': 30, 'tool_calls': 30, 'command_seconds': 10, 'finalize_seconds': 10},
                'scorer_provider': 'openrouter', 'scorer_model': 'typesafe/jev-1.14', 'scorer_settings': {}})
            self.addCleanup(log.close)
            runtime = Runtime(log)
            for name in ('discovery', 'login', 'candidate'):
                (runtime.runtime_dir / 'work' / f'{name}.txt').write_text(f'{name}: PRIVATE_TOKEN', encoding='utf-8')
            contexts = []
            facts = {'input_control': 'full', 'object_scope': 'self', 'data_exposure': 'sensitive',
                     'query_behavior': 'controlled_difference', 'baseline_compared': True,
                     'security_signal': 'auth_bypass', 'baseline_status': 401, 'response_status': 200}

            def transport(payload):
                context = json.loads(payload['messages'][1]['content'])
                contexts.append(context)
                kind = context['node']['kind']
                source = {'root': 'discovery', 'surface': 'login', 'candidate': 'candidate'}[kind]
                ref = {'path': f'work/{source}.txt', 'description': 'controlled comparison'}
                result = {**context['result_template'], 'result_path': context['attempt']['result_path'],
                          'evidence_refs': [ref], 'observations': facts if kind != 'root' else {}}
                if kind == 'root':
                    child = surface('https://example.test/login')
                    child.update(observations={'input_control': 'full', 'query_behavior': 'unchanged'}, evidence_refs=[ref])
                    result['discovered'] = [child]
                elif kind == 'candidate':
                    result['observations'] = {**facts, 'reproduced': True}
                yield {'message': {'content': json.dumps({'action': 'run_command', 'plan': 'inspect', 'reason': 'compare',
                    'path': '', 'content': '', 'code': '', 'command': json.dumps(result)})}}
                yield {'done': True, 'done_reason': 'stop'}

            class EvidenceAdapter(FakeAdapter):
                def evaluate(self, pin, parent, siblings, node_ids, **kwargs):
                    response = good(node_ids)
                    for node in siblings:
                        if node['id'] in node_ids:
                            level = 3 if node['observations'].get('security_signal') == 'auth_bypass' else 1
                            response['scores'][node['id']] = validate_answer({**answer(level),
                                'probabilities': {str(i): int(i == level) for i in range(5)}})
                    return response

            runner = V4Runner(log, runtime, transport=transport, scorer_adapter=EvidenceAdapter())
            self.assertEqual(runner.run(), ('completed', 'tree_exhausted'))
            self.assertEqual([c['node']['kind'] for c in contexts], ['root', 'surface', 'candidate'])
            self.assertEqual([r['path'] for r in contexts[2]['discovery_evidence']['evidence_refs']], ['work/login.txt'])
            state = TreeStore(log).rebuild()
            self.assertEqual(state['nodes']['N0002']['risk']['score'], 75)
            self.assertEqual([r['score'] for r in state['nodes']['N0002']['risk_history']], [25, 75])
            self.assertEqual(state['nodes']['N0003']['risk']['status'], 'scored')
            self.assertEqual(state['nodes']['N0003']['verification_state'], 'inconclusive')
            report = save_report(log, 'completed', 'tree_exhausted', state=state)
            self.assertEqual(report['stats']['candidates'], 1)
            self.assertEqual(report['stats']['tested_candidates'], 1)
            self.assertEqual(report['stats']['unscored'], 0)
            self.assertEqual(report['candidate_leads'][0]['hypothesis']['type'], 'auth')
            self.assertEqual([r['runtime_path'] for r in report['candidate_leads'][0]['candidate_evidence_refs']],
                             ['work/candidate.txt'])
            for event in log.read_events():
                if event['kind'] == 'scorer_request_started':
                    self.assertNotIn('PRIVATE_TOKEN', json.dumps(event))
            log.close()

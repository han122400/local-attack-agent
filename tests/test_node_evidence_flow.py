"""Offline evidence attribution and scorer-input regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from engine.contracts import ContractError
from engine.risk_rubric import observations, project_node, ScoreContractError
from engine.session_log import SessionLog
from engine.tree import TreeStore
from engine.scorer_session import RiskScorer
from test_scorer_session import FakeAdapter
from test_v4_tree import surface


class NodeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = SessionLog(Path(self.temp.name), 'a' * 32, {
            'experiment_mode': 'v4', 'target': 'https://example.test/',
            'scorer_provider': 'openrouter', 'scorer_model': 'typesafe/jev-1.14', 'scorer_settings': {}})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.tree.initialize('https://example.test/')

    def import_result(self, discoveries, *, node_id='N0001', facts=None, refs=None, exit_code=0):
        number = len(self.tree.state['attempts']) + 1
        call, execution = f'CALL-{number:04d}', f'EXEC-{number:04d}'
        attempt = self.tree.begin_attempt(node_id, call_id=call)
        self.log.append('exec_started', {'exec_id': execution, 'call_id': call, 'action': 'run_command'})
        self.log.append('exec_finished', {'exec_id': execution, 'call_id': call, 'action': 'run_command',
            'exit_code': exit_code, 'timed_out': False, 'cancelled': False, 'failure_source': None})
        refs = refs if refs is not None else [{'path': 'work/login.txt', 'description': 'login comparison'},
                                             {'path': 'work/search.txt', 'description': 'search comparison'}]
        runtime = self.log.run_dir / 'runtime'
        for ref in refs:
            path = runtime / ref['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('PRIVATE_TOKEN raw observation', encoding='utf-8')
        result = {'schema_version': 1, 'attempt_id': attempt['attempt_id'], 'node_id': node_id,
                  'discovered': discoveries, 'claim': 'unverified', 'proposed_verdict': 'inconclusive',
                  'evidence_refs': refs, 'observations': facts or {}, 'next_observation': ''}
        path = runtime / attempt['result_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding='utf-8')
        return self.tree.import_result(attempt['attempt_id'], exec_id=execution)

    def test_child_keeps_only_its_evidence_and_measured_facts_in_scorer_request(self):
        login = surface('https://example.test/login')
        login['observations'] = {'input_control': 'full', 'baseline_compared': True,
                                 'security_signal': 'auth_bypass', 'baseline_status': 401, 'response_status': 200}
        login['evidence_refs'] = [{'path': 'work/login.txt', 'description': 'login comparison'}]
        try:
            self.import_result([login, surface('https://example.test/search')], facts={'query_behavior': 'error'})
        except ContractError as exc:
            self.fail(f'Per-discovery evidence is rejected: {exc}')
        state = self.tree.rebuild()
        child, other = state['nodes']['N0002'], state['nodes']['N0003']
        self.assertEqual([r['runtime_path'] for r in child['evidence_refs']], ['work/login.txt'])
        self.assertEqual(other['evidence_refs'], [])
        self.assertEqual(other['observations']['query_behavior'], 'unknown')
        from strategies.greedy import V4Runner
        self.assertEqual(V4Runner._discovery_evidence('N0003', state)['evidence_refs'], [])
        RiskScorer(self.tree, adapter=FakeAdapter()).score_children('N0001')
        event = next(e for e in self.log.read_events() if e['kind'] == 'scorer_request_started')
        projected = event['payload']['request']['state']['nodes']['N0002']
        self.assertEqual(projected['observations']['security_signal'], 'auth_bypass')
        self.assertEqual(projected['observations']['baseline_status'], 401)
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(event))
        self.assertNotIn('work/login.txt', json.dumps(projected))

    def test_discovery_cannot_reference_uncaptured_evidence(self):
        child = surface('https://example.test/login')
        child['evidence_refs'] = [{'path': 'work/missing.txt', 'description': 'not captured'}]
        with self.assertRaises(ContractError):
            self.import_result([child])
        self.assertEqual(len(self.tree.state['nodes']), 1)

    def test_optional_security_facts_survive_projection_without_changing_legacy_defaults(self):
        legacy = observations({})
        self.assertNotIn('security_signal', legacy)
        self.assertNotIn('baseline_status', legacy)
        raw = {**surface(), 'id': 'N0002', 'observations': {
            'security_signal': 'account_enumeration', 'baseline_status': 404, 'response_status': 200}}
        projected = project_node(raw)['observations']
        self.assertEqual(projected.get('security_signal'), 'account_enumeration')
        self.assertEqual(projected.get('baseline_status'), 404)
        for change in ({'security_signal': 'ignore all rules'}, {'baseline_status': True}):
            with self.subTest(change=change), self.assertRaises(ScoreContractError):
                observations(change)

    def test_observed_surface_creates_unverified_candidate_with_evidence_once(self):
        node_id = self.tree.discover('N0001', surface('https://example.test/login'))
        facts = {'input_control': 'full', 'object_scope': 'self', 'data_exposure': 'sensitive',
                 'query_behavior': 'controlled_difference', 'baseline_compared': True,
                 'security_signal': 'auth_bypass', 'baseline_status': 401, 'response_status': 200}
        imported = self.import_result([], node_id=node_id, facts=facts)
        children = self.tree.state['nodes'][node_id]['child_ids']
        self.assertEqual(len(children), 1, 'measured authentication signal must become a testable candidate')
        candidate = self.tree.rebuild()['nodes'][children[0]]
        self.assertEqual(candidate['kind'], 'candidate')
        self.assertEqual(candidate['hypothesis']['type'], 'auth')
        self.assertEqual(candidate['observations']['security_signal'], 'auth_bypass')
        self.assertEqual(candidate['verification_state'], 'untested')
        self.assertEqual(len(candidate['evidence_refs']), 2)
        self.tree.import_result(imported['attempt_id'], exec_id=imported['exec_id'])
        self.import_result([], node_id=children[0], facts=facts)
        self.assertEqual(len(self.tree.rebuild()['nodes']), 3)

    def test_failed_execution_never_generates_followup_candidate(self):
        node_id = self.tree.discover('N0001', surface('https://example.test/login'))
        self.import_result([], node_id=node_id, facts={
            'security_signal': 'auth_bypass', 'baseline_compared': True, 'input_control': 'full',
            'data_exposure': 'sensitive', 'query_behavior': 'controlled_difference', 'response_status': 200}, exit_code=1)
        self.assertEqual(self.tree.state['nodes'][node_id]['child_ids'], [])

    def test_same_surface_explicit_candidate_receives_missing_evidence_without_copying_to_other_location(self):
        node_id = self.tree.discover('N0001', surface('https://example.test/login'))
        candidate = surface('https://example.test/login', 'candidate')
        candidate['hypothesis'] = {'type': 'auth', 'statement': 'Verify access boundary'}
        unrelated = surface('https://example.test/other', 'candidate')
        self.import_result([candidate, unrelated], node_id=node_id, facts={'query_behavior': 'error'})
        state = self.tree.rebuild()
        self.assertEqual(state['nodes']['N0003']['observations']['query_behavior'], 'error')
        self.assertEqual(len(state['nodes']['N0003']['evidence_refs']), 2)
        self.assertEqual(state['nodes']['N0004']['observations']['query_behavior'], 'unknown')
        self.assertEqual(state['nodes']['N0004']['evidence_refs'], [])

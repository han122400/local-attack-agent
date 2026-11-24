"""Worker-message integration and audit isolation; no browser is launched."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine.command_runtime import CommandRuntime
from engine.http_capture_sink import ReconstructedCaptureSink
from engine.observation_verification import POLICY
from engine.reporting import save_report
from engine.risk_rubric import project_node
from engine.session_log import SessionLog
from engine.tree import TreeStore
from test_scorer_session import FakeAdapter, PIN
from engine.scorer_session import RiskScorer
from test_v4_tree import surface


class BrowserAuditRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = 'https://browser.test/'
        self.log = SessionLog(Path(temporary.name), 'b' * 32, {
            'experiment_mode': 'v4', 'target': self.target,
            'scorer_provider': 'openrouter', 'scorer_model': PIN['id'],
            'scorer_settings': {'reevaluation_policy': POLICY}})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize(self.target)
        self.node = self.tree.discover(self.root, surface(self.target + 'items?q=', 'candidate'))
        self.runtime = CommandRuntime(Path(temporary.name), self.log)
        self.runtime.start_session(self.log.run_id, target=self.target, browser=False)
        self.addCleanup(self.runtime.stop, self.log.run_id)

    def observation(self):
        return {'source': 'browser_response_audit', 'representation': 'playwright_decoded',
                'literal': False, 'verification_eligible': False,
                'request': {'url': self.target + 'items?q=x', 'method': 'GET'},
                'response': {'status': 200, 'headers': [['Content-Type', 'application/json']],
                             'body_base64': base64.b64encode(b'{"items":[1,2]}').decode()}}

    def run_with_snapshot(self, snapshots):
        attempt = self.tree.begin_attempt(self.node, call_id='BROWSER-CALL')
        sent = []
        self.runtime._browser_enabled = True
        self.runtime._worker = SimpleNamespace(poll=lambda: None)
        try:
            with patch.object(self.runtime, '_send_control', side_effect=sent.append), \
                 patch.object(self.runtime, '_wait_control', side_effect=lambda *a, **k: {
                     'type': 'result', 'exec_id': sent[-1]['exec_id'], 'exit_code': 0,
                     'browser_observations': snapshots}), \
                 patch.object(self.runtime, '_capture_browser_tree'):
                result = self.runtime.run(self.log.run_id, {'action': 'run_python', 'code': "print('done')"},
                                          call_id=attempt['call_id'])
        finally:
            self.runtime._worker = None
            self.runtime._browser_enabled = False
        self.assertEqual(result.exit_code, 0, result.exception)
        return attempt, result, sent

    def test_response_snapshot_is_saved_as_explicit_nonliteral_audit(self):
        attempt, result, sent = self.run_with_snapshot([self.observation()])
        self.assertTrue(sent[0]['browser_audit'])
        events = [e for e in self.log.snapshot_events() if e['kind'] == 'http_exchange_observed']
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event['source'], 'browser_response_audit')
        self.assertEqual(event['attempt_id'], attempt['attempt_id'])
        self.assertEqual(event['exec_id'], result.exec_id)
        self.assertEqual(event['artifact']['source'], 'browser_response_audit')
        raw = (self.log.run_dir / event['artifact']['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), event['artifact']['sha256'])
        saved = json.loads(raw)
        self.assertFalse(saved['literal'])
        self.assertFalse(saved['verification_eligible'])
        self.assertEqual(saved['source'], 'browser_response_audit')
        self.assertEqual(base64.b64decode(saved['observation']['response']['body_base64']), b'{"items":[1,2]}')
        self.assertEqual(result.http_captures, [])
        self.assertFalse(self.tree.rebuild().get('http_exchanges'))

    def test_audit_cannot_supply_claim_evidence_scores_or_verified_report_data(self):
        attempt, result, _ = self.run_with_snapshot([self.observation()])
        self.assertEqual(len([e for e in self.log.snapshot_events() if e['kind'] == 'http_exchange_observed']), 1)
        value = {'schema_version': 1, 'node_id': self.node, 'attempt_id': attempt['attempt_id'],
                 'discovered': [], 'proposed_verdict': 'confirmed', 'claim': 'BROWSER CONFIRMED CLAIM',
                 'next_observation': '', 'evidence_refs': [],
                 'observations': {'response_status': 200, 'reproduced': True},
                 'observation_evidence': {'response_status': {'request_ids': ['R' + '1' * 32]}}}
        path = self.runtime.runtime_dir / attempt['result_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        self.tree.import_result(attempt['attempt_id'], exec_id=result.exec_id)
        state = self.tree.rebuild()
        node = state['nodes'][self.node]
        self.assertEqual(node['observation_verification']['fields']['response_status']['state'], 'unsupported')
        self.assertIsNone(project_node(node, projection_version=POLICY)['observations']['response_status'])
        RiskScorer(self.tree, adapter=FakeAdapter()).score_children(self.root)
        before = self.tree.state['scorer']
        ReconstructedCaptureSink(self.log, attempt, result.exec_id).record(self.observation())
        restored = self.tree.rebuild()
        self.assertEqual(restored['scorer'], before)
        self.assertFalse(restored.get('http_exchanges'))
        report = save_report(self.log, 'incomplete', 'audit_only', state=restored)
        self.assertNotIn('BROWSER CONFIRMED CLAIM', json.dumps(report))
        self.assertIsNone(report['candidate_leads'][0]['observations']['response_status'])

    def test_failed_optional_audit_write_does_not_fail_user_execution(self):
        original = self.log._check_artifact_destination
        def fail_sidecar(path):
            if 'http-observed' in path.parts:
                raise OSError('audit storage unavailable')
            original(path)
        with patch.object(self.log, '_check_artifact_destination', side_effect=fail_sidecar):
            _, result, _ = self.run_with_snapshot([self.observation()])
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.capture_warnings)
        self.assertFalse(self.log.failed)
        self.assertFalse([e for e in self.log.snapshot_events() if e['kind'] == 'http_exchange_observed'])

    def test_worker_cannot_promote_source_or_submit_other_origin(self):
        claimed_literal = {**self.observation(), 'literal': True, 'verification_eligible': True}
        wrong_source = {**self.observation(), 'source': 'harness_http', 'request_id': 'R' + '2' * 32}
        other_origin = self.observation()
        other_origin['request']['url'] = 'https://other.test/items'
        self.run_with_snapshot([claimed_literal, wrong_source, other_origin])
        events = [e for e in self.log.snapshot_events() if e['kind'] == 'http_exchange_observed']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['source'], 'browser_response_audit')
        stored = json.loads((self.log.run_dir / events[0]['artifact']['path']).read_text(encoding='utf-8'))
        self.assertEqual(stored['observation']['source'], 'browser_response_audit')
        self.assertFalse(self.tree.rebuild().get('http_exchanges'))


if __name__ == '__main__':
    unittest.main()

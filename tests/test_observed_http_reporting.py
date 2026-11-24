"""Visibility-only report section: no browser, model, or external network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.http_capture_sink import ReconstructedCaptureSink
from engine.observation_verification import POLICY
from engine.reporting import save_report
from engine.session_log import SessionLog
from engine.tree import TreeStore
from test_v4_tree import surface


class ObservedHttpReportingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = SessionLog(Path(temp.name), 'd' * 32, {
            'experiment_mode': 'v4', 'target': 'https://example.test/',
            'scorer_settings': {'reevaluation_policy': POLICY}})
        self.addCleanup(self.log.close)
        self.tree = TreeStore(self.log)
        self.root = self.tree.initialize(self.log.metadata['target'])
        self.node = self.tree.discover(self.root, surface('https://example.test/items?q=', 'candidate'))
        self.attempt = self.tree.begin_attempt(self.node, call_id='CALL-1')
        self.log.append('exec_started', {'exec_id': 'EXEC-1', 'call_id': 'CALL-1', 'action': 'run_python'})

    def record(self, representation='playwright_decoded', url='https://example.test/items'):
        ReconstructedCaptureSink(self.log, self.attempt, 'EXEC-1').record({
            'representation': representation, 'literal': False, 'verification_eligible': False,
            'request': {'url': url, 'method': 'POST', 'headers': [['Authorization', 'Bearer PRIVATE_SECRET']]},
            'response': {'status': 201, 'headers': [['Set-Cookie', 'session=PRIVATE_SECRET']],
                         'body_base64': 'PRIVATE_BODY'}})
        return self.log.snapshot_events()[-1]

    def report(self, **kwargs):
        state = kwargs.pop('state') if 'state' in kwargs else self.tree.refresh()
        return save_report(self.log, 'incomplete', 'stopped', persist_metadata=False,
                           state=state, **kwargs)

    def assert_verified_unchanged(self, before, after):
        allowed = {'summary', 'last_readable_seq', 'observed_http', 'observed_http_truncated'}
        for key in set(before) | set(after):
            if key not in allowed:
                self.assertEqual(after.get(key), before.get(key), key)

    def test_new_events_have_only_safe_metadata_and_report_both_sources(self):
        before = self.report()
        browser = self.record()
        mitm = self.record('mitmproxy_reconstructed', 'https://example.test/api')
        for event in (browser, mitm):
            self.assertEqual(event['method'], 'POST')
            self.assertEqual(event['status'], 201)
            self.assertNotIn('PRIVATE', json.dumps(event))
            self.assertNotIn('headers', event)
            self.assertNotIn('request', event)
            self.assertNotIn('response', event)
        after = self.report()
        items = after['observed_http']
        self.assertEqual(len(items), 2)
        self.assertEqual([item['url'] for item in items], ['https://example.test/items', 'https://example.test/api'])
        self.assertEqual([item['source'] for item in items], ['browser_response_audit', 'mitmproxy_audit'])
        for item, event in zip(items, (browser, mitm)):
            self.assertEqual(set(item), {'node_id', 'attempt_id', 'exec_id', 'source', 'representation',
                'literal', 'verification_eligible', 'url', 'method', 'status', 'artifact', 'label'})
            self.assertEqual(item['artifact'], event['artifact'])
            self.assertFalse(item['literal'])
            self.assertFalse(item['verification_eligible'])
            self.assertIn('재구성·미검증', item['label'])
        self.assertFalse(after['observed_http_truncated'])
        self.assertIn('브라우저/MITM 미검증 관측 2건 (검증·점수 무관)', after['summary'])
        self.assertEqual(after['summary'].splitlines()[:-1], before['summary'].splitlines())
        self.assert_verified_unchanged(before, after)
        self.assertFalse(self.tree.refresh().get('http_exchanges'))

    def test_no_events_produces_empty_section_without_summary_change(self):
        report = self.report()
        self.assertEqual(report['observed_http'], [])
        self.assertFalse(report['observed_http_truncated'])
        self.assertNotIn('미검증 관측', report['summary'])

    def test_report_includes_events_under_every_existing_policy_without_reading_artifacts(self):
        self.record()
        original = Path.open
        def reject_artifact(path, *args, **kwargs):
            if 'http-observed' in path.parts:
                raise AssertionError('report must not read observed artifact')
            return original(path, *args, **kwargs)
        for policy in ('scoring_fields_v1', 'scoring_fields_v2_audit', POLICY, None):
            self.log.update(scorer_settings={'reevaluation_policy': policy})
            with self.subTest(policy=policy), patch.object(Path, 'open', reject_artifact):
                self.assertEqual(len(self.report()['observed_http']), 1)

    def test_old_events_missing_metadata_stay_unknown_without_artifact_backfill(self):
        event = self.record()
        old = {key: value for key, value in event.items() if key not in {'url', 'method', 'status', 'seq', 'at', 'kind', 'run_id'}}
        self.log.append('http_exchange_observed', old)
        (self.log.run_dir / event['artifact']['path']).unlink()
        report = self.report()
        legacy = report['observed_http'][-1]
        self.assertIsNone(legacy['url'])
        self.assertIsNone(legacy['method'])
        self.assertIsNone(legacy['status'])
        self.assertEqual(report['status'], 'complete')

    def test_degraded_state_or_corrupt_prefix_has_no_observed_section_items(self):
        self.record()
        unavailable = self.report(state=None)
        self.assertEqual(unavailable['observed_http'], [])
        self.assertFalse(unavailable['observed_http_truncated'])
        state = self.tree.refresh()
        with self.log.events_path.open('ab') as stream:
            stream.write(b'{broken\n')
        corrupt = self.report(state=state)
        self.assertEqual(corrupt['status'], 'degraded')
        self.assertEqual(corrupt['observed_http'], [])

    def test_usage_failure_also_empties_observed_items(self):
        self.record()
        self.log.append('v4_execution_reserved', {'call_id': 'BAD-USAGE', 'remote': 'invalid', 'estimate_usd': 1})
        report = self.report()
        self.assertEqual(report['status'], 'degraded')
        self.assertEqual(report['observed_http'], [])

    def test_count_and_utf8_size_caps_mark_truncated(self):
        template = self.record()
        payload = {key: value for key, value in template.items() if key not in {'seq', 'at', 'kind', 'run_id'}}
        for _ in range(64):
            self.log.append('http_exchange_observed', payload)
        report = self.report()
        self.assertEqual(len(report['observed_http']), 64)
        self.assertTrue(report['observed_http_truncated'])
        self.assertIn('일부 생략', report['summary'])
        # An independent report with long multi-byte URLs must hit the byte cap first.
        from engine.observed_http import observed_http_section
        events = [dict(template, url='https://example.test/' + '경로' * 500) for _ in range(64)]
        items, truncated = observed_http_section(events)
        self.assertTrue(truncated)
        self.assertGreater(len(items), 0)
        self.assertLess(len(items), 64)
        self.assertLessEqual(len(json.dumps({'observed_http': items}, ensure_ascii=False, indent=2).encode()), 65536)

    def test_credentials_stay_only_in_artifact(self):
        event = self.record(url='https://user:URL_PASSWORD@example.test/items?q=QUERY_SECRET&custom=TOKEN_SECRET#FRAGMENT_SECRET')
        for secret in ('URL_PASSWORD', 'QUERY_SECRET', 'TOKEN_SECRET', 'FRAGMENT_SECRET', 'PRIVATE_SECRET'):
            self.assertNotIn(secret, json.dumps(event))
            self.assertNotIn(secret, json.dumps(self.report()['observed_http']))
        self.assertTrue(event['url'].startswith('https://example.test/items?'))
        self.assertIn('PRIVATE_SECRET', (self.log.run_dir / event['artifact']['path']).read_text(encoding='utf-8'))

    def test_malformed_and_verified_sources_cannot_be_mixed_into_section(self):
        template = self.record()
        from engine.observed_http import observed_http_section
        forged = [dict(template, source='harness_http'), dict(template, literal=True),
                  dict(template, verification_eligible=True), dict(template, artifact=None)]
        extra = copy.deepcopy(template)
        extra['artifact'].update(headers={'Authorization': 'PRIVATE_SECRET'}, body='PRIVATE_BODY')
        items, _ = observed_http_section([*forged, extra])
        self.assertEqual(len(items), 1)
        self.assertNotIn('PRIVATE', json.dumps(items))


if __name__ == '__main__':
    unittest.main()

"""Real command execution through passive HTTP capture (no browser launch)."""
import json
import sys
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_supported_observations as fixtures
from engine.tree import replay_tree
from engine.http_capture import check_capture_records
from engine.session_log import LogFailure


class ProxyRuntimeTests(unittest.TestCase):
    setUp = fixtures.SupportedObservationsTests.setUp

    def command(self, code):
        attempt = self.tree.begin_attempt(self.node, call_id=f"PROXY-{len(self.tree.state['attempts'])}")
        if callable(code):
            code = code(attempt)
        script = self.runtime.runtime_dir / "tools" / "direct.py"
        script.write_text(code, encoding="utf-8")
        result = self.runtime.run(self.log.run_id, {
            "action": "run_command", "command": f'"{sys.executable}" "{script}"'}, call_id=attempt["call_id"])
        self.assertEqual(result.exit_code, 0, result.stderr + result.exception)
        state = replay_tree(self.log.snapshot_events(), self.log.run_id)
        records = [r for r in state.get("http_exchanges", {}).values() if r["attempt_id"] == attempt["attempt_id"]]
        return attempt, result, records

    def test_direct_urllib_has_literal_capture_and_supported_import(self):
        attempt, result, records = self.command(
            "import urllib.request\n"
            f"url={self.target + 'items?q='!r}\n"
            "for value in ('zero', 'zero', 'two', 'two'):\n"
            "    with urllib.request.urlopen(url+value) as response:\n"
            "        print(response.read().decode())\n")
        self.assertEqual(len(records), 4)
        self.assertEqual([r['facts']['json_counts']['/data'] for r in records], [0, 0, 2, 2])
        check_capture_records(self.log.run_dir, records)
        proof = {'baseline': [r['request_id'] for r in records[:2]],
                 'test': [r['request_id'] for r in records[2:]], 'json_pointer': '/data'}
        value = {'schema_version': 1, 'node_id': self.node, 'attempt_id': attempt['attempt_id'],
                 'discovered': [], 'proposed_verdict': 'confirmed', 'claim': 'MODEL CLAIM',
                 'next_observation': '', 'evidence_refs': [],
                 'observations': {'reproduced': True, 'query_behavior': 'controlled_difference'},
                 'observation_evidence': {key: proof for key in ('reproduced', 'query_behavior')}}
        path = self.runtime.runtime_dir / attempt['result_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        self.tree.import_result(attempt['attempt_id'], exec_id=result.exec_id)
        node = self.tree.state['nodes'][self.node]
        self.assertEqual(node['observation_verification']['fields']['reproduced']['state'], 'supported')
        self.assertEqual(node['verification_state'], 'inconclusive')
        unsupported = self.tree.discover(self.root, fixtures.surface(self.target + 'unverified?q=', 'candidate'))
        self.tree.set_observations(unsupported, {'reproduced': True, 'query_behavior': 'controlled_difference'})
        class EvidenceAdapter(fixtures.FakeAdapter):
            def evaluate(adapter, pin, parent, siblings, node_ids, **kwargs):
                output = super().evaluate(pin, parent, siblings, node_ids, **kwargs)
                for sibling in siblings:
                    projected = fixtures.project_node(sibling, projection_version=fixtures.POLICY)
                    score = 3 if projected['observations'].get('reproduced') is True else 1
                    output['scores'][sibling['id']] = fixtures.validate_answer({
                        'type': 'score', 'score': score,
                        'probabilities': {str(i): float(i == score) for i in range(5)},
                        'confidence': 1, 'legend': {str(i): text for i, text in enumerate(fixtures.CRITERIA)}})
                return output
        ordering = fixtures.RiskScorer(self.tree, adapter=EvidenceAdapter()).score_children(self.root)
        self.assertEqual(ordering['ordered_node_ids'], [self.node, unsupported])
        restored = self.tree.rebuild()
        self.assertEqual(restored['nodes'][self.node]['observation_verification']['fields']['reproduced']['state'], 'supported')
        report = fixtures.save_report(self.log, 'incomplete', 'test_finished', state=restored)
        self.assertNotIn('MODEL CLAIM', json.dumps(report))
        lead = next(item for item in report['candidate_leads'] if item['node_id'] == self.node)
        self.assertTrue(lead['observations']['reproduced'])

    def test_helper_and_direct_request_are_each_recorded_once(self):
        _, _, records = self.command(
            "from http_capture_client import request\nimport urllib.request\n"
            f"url={self.target + 'items?q=two'!r}\n"
            "print(request(url)['status'])\n"
            "print(urllib.request.urlopen(url).read())\n")
        self.assertEqual(len(records), 2)
        self.assertEqual(len({r['request_id'] for r in records}), 2)

    def test_proxy_start_failure_leaves_command_and_direct_http_usable(self):
        from engine.http_proxy_capture import PlainHttpProxy
        # Runtime initialization must treat an observer bind failure as a miss.
        self.runtime.stop(self.log.run_id)
        from engine.command_runtime import CommandRuntime
        self.runtime.destroy(self.log.run_id)
        self.runtime = CommandRuntime(self.directory, self.log)
        self.addCleanup(self.runtime.stop, self.log.run_id)
        with patch.object(PlainHttpProxy, '__init__', side_effect=OSError('listener unavailable')):
            self.runtime.start_session(self.log.run_id, target=self.target, browser=False)
        _, _, records = self.command(
            "import urllib.request\n"
            f"print(urllib.request.urlopen({self.target!r}).read())\n")
        self.assertEqual(records, [])

    def test_proxy_bytes_tampering_is_rejected_on_import_and_rebuild(self):
        _, _, records = self.command(
            "import urllib.request\n"
            f"print(urllib.request.urlopen({self.target!r}).read())\n")
        self.assertEqual(len(records), 1)
        (self.log.run_dir / records[0]['response']['path']).write_bytes(b'forged')
        with self.assertRaises(ValueError):
            check_capture_records(self.log.run_dir, records)
        with self.assertRaises(LogFailure):
            self.tree.rebuild()

    def test_reconstructed_observation_never_enters_literal_verification(self):
        from engine.http_capture_sink import ReconstructedCaptureSink
        attempt = self.tree.begin_attempt(self.node, call_id='RECONSTRUCTED')
        self.log.append('exec_started', {'exec_id': 'EXEC-RECON', 'call_id': attempt['call_id'],
                                         'action': 'run_command'})
        sink = ReconstructedCaptureSink(self.log, attempt, 'EXEC-RECON')
        sink.record({'representation': 'mitmproxy_reconstructed', 'literal': False,
                     'verification_eligible': False, 'url': self.target, 'status': 200,
                     'request': {'method': 'GET'}, 'response': {'body_base64': 'W10='}})
        state = replay_tree(self.log.snapshot_events(), self.log.run_id)
        self.assertFalse(state.get('http_exchanges'))
        event = self.log.snapshot_events()[-1]
        self.assertEqual(event['kind'], 'http_exchange_observed')
        artifact = json.loads((self.log.run_dir / event['artifact']['path']).read_text(encoding='utf-8'))
        self.assertFalse(artifact['literal'])
        self.assertFalse(artifact['verification_eligible'])

    def test_helper_listener_failure_does_not_prevent_direct_command(self):
        with patch('engine.http_capture.CaptureSession.__init__', side_effect=OSError('listener unavailable')):
            _, result, _ = self.command("print('EXECUTED')\n")
        self.assertIn('EXECUTED', result.stdout)

    def test_proxy_artifact_failure_does_not_poison_session_writer(self):
        original = self.log._check_artifact_destination
        def reject_capture(path):
            if 'http-capture' in path.parts:
                raise OSError('capture disk failure')
            return original(path)
        with patch.object(self.log, '_check_artifact_destination', side_effect=reject_capture):
            _, result, records = self.command("import urllib.request\n"
                f"print(urllib.request.urlopen({self.target!r}).status)\n")
        self.assertIn('200', result.stdout)
        self.assertEqual(records, [])
        self.assertFalse(self.log.failed)

    def test_direct_client_can_read_its_capture_receipt_before_exit(self):
        _, result, records = self.command("import urllib.request, os, json, time\n"
            f"print(urllib.request.urlopen({self.target!r}).status)\n"
            "from pathlib import Path\n"
            "p=Path(os.environ['HARNESS_HTTP_CAPTURE_INDEX'])\n"
            "for _ in range(100):\n"
            "    receipts=json.loads(p.read_text())['requests']\n"
            "    if receipts: break\n"
            "    time.sleep(.01)\n"
            "print(receipts[0]['request_id'])\n")
        self.assertEqual(len(records), 1)
        self.assertIn(records[0]['request_id'], result.stdout)

    def test_default_has_no_ca_and_does_not_inherit_internal_mitm_switch(self):
        with patch.dict(os.environ, {'HARNESS_MITM_OPT_IN': '1', 'HARNESS_HTTP_PROXY': 'http://wrong.invalid:1'}):
            env = self.runtime._environment()
        self.assertNotIn('HARNESS_MITM_OPT_IN', env)
        self.assertNotEqual(env['HARNESS_HTTP_PROXY'], 'http://wrong.invalid:1')
        self.assertFalse((self.log.run_dir / '_ca').exists())

    def test_non_http_proxy_configuration_is_not_overridden(self):
        with patch.dict(os.environ, {'ALL_PROXY': 'socks5://existing.test:99',
                                    'all_proxy': 'socks5://existing.test:99'}):
            env = self.runtime._environment()
        self.assertEqual(env['ALL_PROXY'], 'socks5://existing.test:99')
        # os.environ normalizes names to uppercase on Windows.
        self.assertEqual({key.lower(): value for key, value in env.items()}['all_proxy'], 'socks5://existing.test:99')

    def test_failed_mitm_binding_disables_dead_proxy_before_command(self):
        class FailedBackend:
            address = 'http://127.0.0.1:0'
            closed = False
            def activate(self, binding):
                raise OSError('worker died')
            def close(self):
                self.closed = True
        backend = FailedBackend()
        self.runtime._mitm_proxy = backend
        _, result, records = self.command('import urllib.request, os\n'
            f'print(urllib.request.urlopen({self.target!r}, timeout=1).status)\n'
            "assert 'HARNESS_MITM_OPT_IN' not in os.environ\n")
        self.assertIn('200', result.stdout)
        self.assertTrue(backend.closed)
        self.assertIsNone(self.runtime._mitm_proxy)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / '.deps/mitm-env/Scripts/python.exe').is_file(),
                         'optional isolated Windows backend is absent')
    def test_opt_in_runtime_writes_only_marked_sidecars(self):
        from engine.command_runtime import CommandRuntime
        self.runtime.stop(self.log.run_id)
        self.runtime.destroy(self.log.run_id)
        self.runtime = CommandRuntime(self.directory, self.log, mitm_capture=True)
        self.addCleanup(self.runtime.stop, self.log.run_id)
        self.runtime.start_session(self.log.run_id, target=self.target, browser=False)
        self.assertIsNotNone(self.runtime._mitm_proxy, self.runtime._proxy_start_error)
        _, result, records = self.command("import urllib.request\n"
            f"print(urllib.request.urlopen({self.target!r}).status)\n")
        self.assertEqual(records, [])
        events = [e for e in self.log.snapshot_events() if e['kind'] == 'http_exchange_observed']
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]['verification_eligible'])
        self.assertEqual(events[0]['representation'], 'mitmproxy_reconstructed')
        self.assertTrue((self.log.run_dir / '_ca/sandbox-trust.pem').is_file())


if __name__ == '__main__':
    unittest.main()

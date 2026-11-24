"""Parent-runtime sink for literal origin-side HTTP/1 exchanges.

Only a trusted transport observer may call this sink. Parsed browser snapshots
and reconstructed HTTP messages deliberately have no ingestion path here.
"""
import uuid
import json
from urllib.parse import urlsplit

from engine.contracts import MAX_EVIDENCE_BYTES, MAX_TOTAL_EVIDENCE_BYTES
from engine.http_capture import exchange_facts
from engine.observed_http import display_metadata


class LiteralCaptureSink:
    def __init__(self, log, target, attempt, exec_id, auth_context, *, facts_schema=1, receipt_path=None):
        self.log, self.target, self.attempt = log, urlsplit(target), dict(attempt)
        self.exec_id, self.auth_context, self.facts_schema = exec_id, auth_context, facts_schema
        self.records = []
        self.total_bytes = 0
        self.receipt_path = receipt_path
        self._publish_receipts()

    def _publish_receipts(self):
        if self.receipt_path is not None:
            try:
                from engine.session_log import atomic_write_json
                # Convenience only. Import never trusts this agent-readable copy.
                atomic_write_json(self.receipt_path, {'exec_id': self.exec_id, 'requests': self.records})
            except OSError:
                pass

    def record(self, request, response, url):
        actual = urlsplit(url)
        size = len(request) + len(response)
        if ((actual.scheme, actual.netloc) != (self.target.scheme, self.target.netloc)
                or len(self.records) >= 100
                or max(len(request), len(response)) > MAX_EVIDENCE_BYTES
                or self.total_bytes + size > MAX_TOTAL_EVIDENCE_BYTES):
            return
        facts = exchange_facts(request, response, url, self.auth_context, facts_schema=self.facts_schema)
        request_id = "R" + uuid.uuid4().hex
        with self.log.locked_writer():
            refs = {side: {**self.log.preserve_bytes(
                f"http-capture/{self.attempt['attempt_id']}/{request_id}-{side}.http", data, optional=True),
                "source": "harness_http"} for side, data in (("request", request), ("response", response))}
            payload = {"attempt_id": self.attempt["attempt_id"], "node_id": self.attempt["node_id"],
                       "request_id": request_id, "facts": facts, **refs}
            if self.facts_schema == 2:
                payload["facts_schema"] = 2
            self.log.append("http_exchange_recorded", {
                "tree_schema": 1, "call_id": self.attempt["call_id"],
                "exec_id": self.exec_id, "payload": payload})
        self.total_bytes += size
        self.records.append({"request_id": request_id, "facts": facts})
        self._publish_receipts()


class ReconstructedCaptureSink:
    """Auditable snapshots with no route to the literal-evidence reducer."""
    def __init__(self, log, attempt, exec_id):
        self.log, self.attempt, self.exec_id = log, dict(attempt), exec_id
        self.count, self.total_bytes = 0, 0

    def record(self, observation):
        if (observation.get('representation') not in ('mitmproxy_reconstructed', 'playwright_decoded')
                or observation.get('literal') is not False
                or observation.get('verification_eligible') is not False):
            return
        source = ('browser_response_audit' if observation['representation'] == 'playwright_decoded'
                  else 'mitmproxy_audit')
        label = '재구성·미검증: 파싱본이며 literal 와이어 바이트가 아님'
        observation = {**observation, 'source': source, 'label': label}
        value = {'source': source, 'label': label, 'representation': observation['representation'], 'literal': False,
                 'verification_eligible': False, 'run_id': self.log.run_id,
                 'attempt_id': self.attempt['attempt_id'], 'node_id': self.attempt['node_id'],
                 'call_id': self.attempt['call_id'], 'exec_id': self.exec_id, 'observation': observation}
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if self.count >= 32 or len(raw) > 128 * 1024 or self.total_bytes + len(raw) > 512 * 1024:
            return
        request = observation.get('request')
        response = observation.get('response')
        metadata = display_metadata(
            request.get('url') if isinstance(request, dict) else None,
            request.get('method') if isinstance(request, dict) else None,
            response.get('status') if isinstance(response, dict) else None)
        with self.log.locked_writer():
            ref = self.log.preserve_bytes(f"http-observed/{self.attempt['attempt_id']}/O{uuid.uuid4().hex}.json", raw, optional=True)
            ref = {**ref, 'source': source}
            self.log.append('http_exchange_observed', {
                'call_id': self.attempt['call_id'], 'exec_id': self.exec_id,
                'attempt_id': self.attempt['attempt_id'], 'node_id': self.attempt['node_id'],
                'source': source, 'label': label, 'representation': observation['representation'], 'literal': False,
                'verification_eligible': False, 'artifact': ref, **metadata})
        self.count += 1
        self.total_bytes += len(raw)

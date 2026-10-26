"""Controller-owned HTTP broker. Generated code cannot write artifacts or invent responses."""

import hashlib
import http.client
import json
import time
from urllib.parse import urlsplit

def http_observations(finding: dict) -> list[dict]:
    records = finding.get('http_evidence')
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict) and item.get('source') == 'controller_http'
            and item.get('id') and isinstance(item.get('status'), int)
            and all(isinstance(item.get(key), dict) and item[key].get('path', '').startswith('artifacts/')
                    and len(item[key].get('sha256', '')) == 64
                    for key in ('request_artifact', 'response_artifact'))]


class EvidenceBroker:
    def __init__(self, target: str, log):
        self.target = urlsplit(target)
        if (self.target.scheme not in {'http', 'https'} or not self.target.hostname
                or self.target.username is not None or self.target.password is not None
                or self.target.port == 0):
            raise ValueError('Evidence broker requires an HTTP(S) target without credentials')
        self.origin = self._origin_key(self.target)
        self.log = log
        self.active = None
        self.records = []

    @staticmethod
    def _origin_key(parts):
        hostname = (parts.hostname or '').encode('idna').decode('ascii')
        port = parts.port if parts.port is not None else {'http': 80, 'https': 443}.get(parts.scheme)
        return parts.scheme, hostname, port

    def begin(self, exec_id, call_id):
        self.active = (exec_id, call_id)
        self.records = []

    def end(self, exec_id):
        records = [r for r in self.records if r['exec_id'] == exec_id]
        self.active = None
        self.records = []
        return records

    def fetch(self, method: str, url: str, headers: dict | None = None, *, timeout=8) -> dict:
        if method not in {'GET', 'HEAD'} or not isinstance(url, str) or len(url) > 8192:
            raise PermissionError('Only bounded GET/HEAD requests are allowed')
        parts = urlsplit(url)
        if (self._origin_key(parts) != self.origin
                or parts.username is not None or parts.password is not None or parts.fragment):
            raise PermissionError('HTTP request is outside the authorized target')
        active = self.active
        if active is None or len(self.records) >= 100:
            raise PermissionError('No active execution or HTTP budget exhausted')
        timeout = min(8, max(0.1, timeout))
        connection = http.client.HTTPSConnection if self.target.scheme == 'https' else http.client.HTTPConnection
        conn = connection(self.origin[1], self.origin[2], timeout=timeout)
        try:
            headers = {k: v for k, v in (headers or {}).items() if isinstance(k, str) and isinstance(v, str) and k.lower() in {
                'accept', 'accept-encoding', 'content-type', 'cookie', 'authorization', 'user-agent', 'referer'}}
            if len(json.dumps(headers)) > 16384:
                raise ValueError('HTTP headers exceed the input limit')
            path = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            chunks, size, deadline = [], 0, time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('HTTP body deadline reached')
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 8_000_000:
                    raise ValueError('HTTP response exceeds 8 MB limit')
                chunks.append(chunk)
            body = b''.join(chunks)
            evidence_id = f'HTTP-{active[0]}-{len(self.records) + 1:04d}'
            request = {'url': url, 'method': method,
                       'headers': {k: '[redacted]' if k.lower() in {'cookie', 'authorization'} else v
                                   for k, v in headers.items()}}
            record = {'id': evidence_id, 'source': 'controller_http', 'exec_id': active[0], 'call_id': active[1],
                          'url': url, 'method': method, 'status': response.status,
                          'content_type': response.getheader('Content-Type', ''), 'bytes': size,
                          'body_sha256': hashlib.sha256(body).hexdigest(), 'truncated': size > 65536,
                          'request_artifact': self.log.preserve_bytes(f'http/{evidence_id}-request.json', json.dumps(request).encode()),
                          'response_artifact': self.log.preserve_bytes(f'http/{evidence_id}-body.bin', body[:65536]),
                          'headers_artifact': self.log.preserve_bytes(f'http/{evidence_id}-headers.json', json.dumps(response.getheaders()).encode())}
            self.log.append('http_response', record)
            self.records.append(record)
            return {'status': response.status, 'headers': dict(response.getheaders()), 'body': body}
        except http.client.HTTPException as exc:
            raise ValueError('Invalid or incomplete HTTP response') from exc
        finally:
            conn.close()


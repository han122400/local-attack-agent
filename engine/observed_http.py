"""Metadata-only display lane. No artifact I/O, verification, or scoring."""
import json
import re
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_ITEMS = 64
MAX_SECTION_BYTES = 64 * 1024
LABEL = '재구성·미검증: 파싱본이며 literal 와이어 바이트가 아님'
SOURCES = {'playwright_decoded': 'browser_response_audit', 'mitmproxy_reconstructed': 'mitmproxy_audit'}


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        return None
    try:
        return value if len(value.encode('utf-8')) <= limit else None
    except UnicodeError:
        return None


def _display_url(value):
    """Keep endpoint identity; userinfo, fragments and query values stay in the artifact."""
    value = _text(value, 8192)
    if value is None or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return None
        parts.port  # Reject malformed ports without resolving or opening the URL.
        query = urlencode([(name, '[redacted]') for name, _ in parse_qsl(
            parts.query, keep_blank_values=True, max_num_fields=128)])
        return _text(urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, query, '')), 8192)
    except (ValueError, UnicodeError):
        return None


def display_metadata(url=None, method=None, status=None):
    method = _text(method, 32)
    if method is not None and not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method):
        method = None
    return {'url': _display_url(url), 'method': method,
            'status': status if type(status) is int and 100 <= status <= 599 else None}


def _item(event):
    if (event.get('kind') != 'http_exchange_observed' or event.get('literal') is not False
            or event.get('verification_eligible') is not False):
        return None
    representation, source = event.get('representation'), event.get('source')
    if not isinstance(representation, str) or SOURCES.get(representation) != source or source is None:
        return None
    ref = event.get('artifact')
    if not isinstance(ref, dict):
        return None
    path, digest = _text(ref.get('path'), 2048), _text(ref.get('sha256'), 64)
    if (not path or not path.startswith('artifacts/http-observed/') or '\\' in path or ':' in path
            or any(ord(char) < 32 for char in path) or '..' in PurePosixPath(path).parts
            or digest is None or not re.fullmatch('[0-9a-f]{64}', digest)
            or type(ref.get('bytes')) is not int or ref['bytes'] < 0
            or ref.get('source', source) != source):
        return None
    artifact = {key: ref[key] for key in ('path', 'bytes', 'sha256')}
    if 'source' in ref:
        artifact['source'] = source
    label = _text(event.get('label'), 512) or LABEL
    if '재구성·미검증' not in label:
        label = LABEL
    return {**{key: _text(event.get(key), 128) for key in ('node_id', 'attempt_id', 'exec_id')},
            'source': source, 'representation': representation, 'literal': False,
            'verification_eligible': False, **display_metadata(event.get('url'), event.get('method'), event.get('status')),
            'artifact': artifact, 'label': label}


def observed_http_section(events):
    """Return a bounded journal-order prefix of eligible audit metadata only."""
    items = []
    for event in events:
        if not isinstance(event, dict):
            continue
        item = _item(event)
        if item is None:
            continue
        candidate = items + [item]
        # Match report.json's UTF-8/indentation, including the section key.
        size = len(json.dumps({'observed_http': candidate}, ensure_ascii=False, indent=2).encode('utf-8'))
        if len(candidate) > MAX_ITEMS or size > MAX_SECTION_BYTES:
            return items, True
        items = candidate
    return items, False

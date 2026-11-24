"""Attempt-bound, harness-owned HTTP transport and byte preservation."""

import base64
import hashlib
import http.client
import io
import json
import re
import socket
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from engine.contracts import MAX_EVIDENCE_BYTES, MAX_TOTAL_EVIDENCE_BYTES, normalize_url


FACTS_SCHEMA_V1 = 1
FACTS_SCHEMA_SHAPE_V2 = 2
_SHAPE_MAX_DEPTH = 8
_SHAPE_MAX_POINTERS = 128
_SHAPE_MAX_KEYS_PER_OBJECT = 32
_SHAPE_MAX_TOTAL_KEYS = 256
_SHAPE_MAX_KEY_BYTES = 128
_SHAPE_MAX_BYTES = 16384


def _shape_signatures(value):
    """Return bounded JSON structure only, with no scalar values."""
    signatures, total_keys, total_bytes = {}, 0, 0

    def visit(item, pointer, depth):
        nonlocal total_keys, total_bytes
        if depth > _SHAPE_MAX_DEPTH or len(signatures) >= _SHAPE_MAX_POINTERS:
            raise ValueError("JSON shape exceeds traversal bounds")
        if isinstance(item, dict):
            keys = sorted(item)
            total_keys += len(keys)
            if (len(keys) > _SHAPE_MAX_KEYS_PER_OBJECT or total_keys > _SHAPE_MAX_TOTAL_KEYS
                    or any(len(key.encode("utf-8")) > _SHAPE_MAX_KEY_BYTES for key in keys)):
                raise ValueError("JSON shape exceeds key bounds")
            signature = {"type": "object", "empty": not keys, "keys": keys}
        elif isinstance(item, list):
            signature = {"type": "array", "length": len(item)}
        else:
            signature = {"type": "null" if item is None else "scalar"}
        total_bytes += len(pointer.encode("utf-8")) + len(json.dumps(
            signature, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if total_bytes > _SHAPE_MAX_BYTES:
            raise ValueError("JSON shape exceeds size bound")
        signatures[pointer] = signature
        if isinstance(item, dict):
            for key in keys:
                visit(item[key], pointer + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item[:16]):
                visit(child, pointer + "/" + str(index), depth + 1)

    try:
        visit(value, "", 0)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return {}
    return signatures


class _RecordedReader:
    def __init__(self, stream):
        self.stream = stream
        self.raw = bytearray()

    def _keep(self, data):
        self.raw.extend(data)
        if len(self.raw) > MAX_EVIDENCE_BYTES:
            raise ValueError("HTTP response exceeds evidence limit")
        return data

    def read(self, size=-1):
        limit = MAX_EVIDENCE_BYTES + 1 - len(self.raw)
        return self._keep(self.stream.read(min(size, limit) if size >= 0 else limit))

    def readline(self, size=-1):
        return self._keep(self.stream.readline(min(size, 65537) if size >= 0 else 65537))

    def close(self):
        self.stream.close()


class _ResponseSocket:
    def __init__(self, stream):
        self.reader = _RecordedReader(stream)

    def makefile(self, *args, **kwargs):
        return self.reader


def response_body(raw, method):
    response = http.client.HTTPResponse(_ResponseSocket(io.BytesIO(raw)), method=method)
    response.begin()
    return response, response.read(MAX_EVIDENCE_BYTES + 1)


def exchange_facts(request, response, url, auth_context, *, facts_schema=FACTS_SCHEMA_V1):
    if type(facts_schema) is not int or facts_schema not in (FACTS_SCHEMA_V1, FACTS_SCHEMA_SHAPE_V2):
        raise ValueError("Unknown HTTP facts schema")
    # Share the strict HTTP/1 identity and final-status parser with offline audit.
    from engine.http_audit import _exchange_status, _header_lines

    lines = _header_lines(request)
    method = lines[0].split(b" ", 1)[0].decode("ascii")
    locator = {"url": url, "method": method}
    status = _exchange_status(request, response, url, locator)
    parsed, body = response_body(response, method)
    counts = {}
    shapes = {}
    if "json" in parsed.getheader("Content-Type", "").lower() and not parsed.getheader("Content-Encoding"):
        try:
            value = json.loads(body)
            def visit(item, path, depth):
                if depth > 8 or len(counts) >= 128:
                    return
                if isinstance(item, list):
                    counts[path] = len(item)
                elif isinstance(item, dict):
                    for key, child in list(item.items())[:128]:
                        visit(child, path + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1)
            visit(value, "", 0)
            if facts_schema == FACTS_SCHEMA_SHAPE_V2:
                shapes = _shape_signatures(value)
        except (ValueError, UnicodeError, RecursionError):
            pass
    headers = b"\r\n".join(lines[1:])
    has_credentials = any(line.lower().startswith((b"authorization:", b"cookie:")) for line in lines[1:])
    if (auth_context == "anonymous" and has_credentials) or (auth_context != "anonymous" and not has_credentials):
        auth_context = "unknown"
    facts = {"url": url, "method": method, "auth_context": auth_context, "status": status,
            "headers_sha256": hashlib.sha256(headers).hexdigest(),
            "body_sha256": hashlib.sha256(request.split(b"\r\n\r\n", 1)[1]).hexdigest(),
            "json_counts": counts}
    if facts_schema == FACTS_SCHEMA_SHAPE_V2:
        facts["json_shapes"] = shapes
    return facts


def check_capture_records(directory, records):
    """Validate referenced bytes and measurements before consuming a saved decision."""
    from engine.tree import _read_bounded

    for record in records:
        original = []
        for side in ("request", "response"):
            ref = record[side]
            raw = _read_bounded(directory, ref["path"], MAX_EVIDENCE_BYTES)
            if (ref.get("source") != "harness_http" or len(raw) != ref["bytes"]
                    or hashlib.sha256(raw).hexdigest() != ref["sha256"]):
                raise ValueError("Captured HTTP evidence changed")
            original.append(raw)
        facts = record["facts"]
        facts_schema = record.get("facts_schema", FACTS_SCHEMA_V1)
        if type(facts_schema) is not int or facts_schema not in (FACTS_SCHEMA_V1, FACTS_SCHEMA_SHAPE_V2):
            raise ValueError("Unknown captured HTTP facts schema")
        if exchange_facts(*original, facts["url"], facts["auth_context"], facts_schema=facts_schema) != facts:
            raise ValueError("Captured HTTP measurements changed")


class CaptureSession:
    def __init__(self, log, target, attempt, exec_id, auth_context, timeout, *, facts_schema=FACTS_SCHEMA_V1):
        if type(facts_schema) is not int or facts_schema not in (FACTS_SCHEMA_V1, FACTS_SCHEMA_SHAPE_V2):
            raise ValueError("Unknown HTTP facts schema")
        self.log, self.target, self.attempt = log, normalize_url(target), attempt
        self.exec_id, self.auth_context = exec_id, auth_context
        self.facts_schema = facts_schema
        self.deadline = time.monotonic() + timeout
        self.total_bytes = 0
        self.count = 0
        self.error = None
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.connection = None
        token = uuid.uuid4().hex
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def do_POST(self):
                if self.path != "/" + token:
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_EVIDENCE_BYTES:
                        raise ValueError("Invalid HTTP capture request length")
                    payload = json.loads(self.rfile.read(length))
                    result = owner.request(**payload)
                    raw = json.dumps(result).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as exc:
                    from engine.session_log import LogFailure
                    if isinstance(exc, LogFailure):
                        owner.error = exc
                    self.send_error(502, "HTTP capture unavailable")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/{token}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.timer = threading.Timer(timeout, self.cancel)
        self.timer.daemon = True
        self.timer.start()

    def cancel(self):
        self.cancelled.set()
        connection = self.connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def close(self):
        self.timer.cancel()
        self.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.error:
            raise self.error

    def request(self, url, method="GET", headers=None, body="", params=None):
        with self.lock:
            return self._request(url, method, headers or {}, body, params)

    def _request(self, url, method, headers, body, params=None):
        url = urljoin(self.target, url)
        if params is not None:
            if not isinstance(params, dict) or any(not isinstance(key, str) for key in params):
                raise ValueError("HTTP params must be a dictionary with string keys")
            parts = urlsplit(url)
            query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in params]
            query.extend(params.items())
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
        url = normalize_url(url)
        parts, target = urlsplit(url), urlsplit(self.target)
        if (parts.scheme, parts.netloc) != (target.scheme, target.netloc):
            raise ValueError("HTTP capture is restricted to the run origin")
        if not isinstance(method, str) or not re.fullmatch(r"[A-Z]+", method) or method == "CONNECT":
            raise ValueError("Invalid HTTP method")
        if not isinstance(headers, dict) or len(headers) > 100:
            raise ValueError("Invalid HTTP headers")
        body = base64.b64decode(body, validate=True)
        lines = [f"{method} {parts.path or '/'}{'?' + parts.query if parts.query else ''} HTTP/1.1",
                 f"Host: {parts.netloc}", "Connection: close", f"Content-Length: {len(body)}"]
        for key, value in headers.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                    or not isinstance(value, str) or any(c in value for c in "\r\n\x00")
                    or key.lower() in ("host", "connection", "content-length", "transfer-encoding", "expect", "upgrade")):
                raise ValueError("Invalid or transport-owned HTTP header")
            lines.append(f"{key}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        remaining = self.deadline - time.monotonic()
        if (self.cancelled.is_set() or remaining <= 0 or self.count >= 100 or len(request) > MAX_EVIDENCE_BYTES
                or self.total_bytes + len(request) + MAX_EVIDENCE_BYTES > MAX_TOTAL_EVIDENCE_BYTES):
            raise ValueError("HTTP capture budget exhausted")
        self.count += 1
        with socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)),
                                      timeout=min(30, remaining)) as connection:
            self.connection = connection
            if parts.scheme == "https":
                connection = ssl.create_default_context().wrap_socket(connection, server_hostname=parts.hostname)
                self.connection = connection
            try:
                if self.cancelled.is_set():
                    raise TimeoutError("HTTP capture deadline reached")
                connection.sendall(request)
                recorded = _ResponseSocket(connection.makefile("rb"))
                response = http.client.HTTPResponse(recorded, method=method)
                response.begin()
                response_data = response.read(MAX_EVIDENCE_BYTES + 1)
                if len(response_data) > MAX_EVIDENCE_BYTES or (response.length is not None and response.length != 0):
                    raise ValueError("Incomplete HTTP response")
                raw = bytes(recorded.reader.raw)
                response.close()
            finally:
                self.connection = None
                connection.close()
        self.total_bytes += len(request) + len(raw)
        request_id = "R" + uuid.uuid4().hex
        facts = exchange_facts(request, raw, url, self.auth_context, facts_schema=self.facts_schema)
        with self.log.locked_writer():
            refs = {side: {**self.log.preserve_bytes(f"http-capture/{self.attempt['attempt_id']}/{request_id}-{side}.http", data),
                           "source": "harness_http"} for side, data in (("request", request), ("response", raw))}
            capture = {"attempt_id": self.attempt["attempt_id"], "node_id": self.attempt["node_id"],
                       "request_id": request_id, "facts": facts, **refs}
            if self.facts_schema == FACTS_SCHEMA_SHAPE_V2:
                capture["facts_schema"] = FACTS_SCHEMA_SHAPE_V2
            self.log.append("http_exchange_recorded", {"tree_schema": 1, "call_id": self.attempt["call_id"],
                "exec_id": self.exec_id, "payload": capture})
        return {"request_id": request_id, "url": url, "status": facts["status"],
                "ok": 200 <= facts["status"] < 300, "content_type": response.getheader("Content-Type", ""),
                "body_base64": base64.b64encode(response_data).decode("ascii")}

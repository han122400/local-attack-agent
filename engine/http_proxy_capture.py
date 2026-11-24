"""HTTP forward proxy with bounded, best-effort origin-side byte observation.

No TLS interception, certificate changes, or journal writes occur here. CONNECT
and HTTP upgrades are opaque tunnels. Observation failures never reject traffic.
The bytes retained are those sent to/received from the origin, not reconstructed
HTTP messages. Only the proxy request line is converted to origin form before
it is sent (as required by an explicit HTTP proxy).
"""

from collections import deque
import re
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit, urlunsplit

from engine.contracts import MAX_EVIDENCE_BYTES


class _Unparsed(Exception):
    def __init__(self, data):
        self.data = data


class _Reader:
    def __init__(self, sock, snapshot=None):
        self.sock = sock
        self.buffer = bytearray()
        self.snapshot = snapshot
        self.binding = None

    def _receive(self):
        data = self.sock.recv(65536)
        if not self.buffer and data and self.snapshot:
            self.binding = self.snapshot()
        self.buffer.extend(data)
        return data

    def start(self):
        if not self.buffer:
            self._receive()
        return bool(self.buffer)

    def line(self, limit=65536):
        while b"\n" not in self.buffer:
            if len(self.buffer) >= limit:
                result = bytes(self.buffer)
                self.buffer.clear()
                raise _Unparsed(result)
            data = self._receive()
            if not data:
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
        end = self.buffer.index(b"\n") + 1
        if end > limit:
            result = bytes(self.buffer[:end])
            del self.buffer[:end]
            raise _Unparsed(result)
        result = bytes(self.buffer[:end])
        del self.buffer[:end]
        return result

    def read(self, size=65536):
        if self.buffer:
            result = bytes(self.buffer[:size])
            del self.buffer[:size]
            return result
        return self.sock.recv(size)


class _Bytes:
    def __init__(self, limit):
        self.limit, self.data = limit, bytearray()

    def add(self, data):
        if self.data is not None:
            if len(self.data) + len(data) > self.limit:
                self.data = None
            else:
                self.data.extend(data)


def _head(reader, first=b""):
    lines = [first] if first else []
    length = len(first)
    while True:
        try:
            line = reader.line(max(1, 65536 - length))
        except _Unparsed as exc:
            raise _Unparsed(b"".join(lines) + exc.data) from None
        if not line:
            if not lines:
                return b""
            raise _Unparsed(b"".join(lines))
        lines.append(line)
        length += len(line)
        if line in (b"\r\n", b"\n"):
            return b"".join(lines)


def _headers(head):
    result = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            result.setdefault(key.strip().lower(), []).append(value.strip())
    return result


def _framing(headers):
    encodings = b",".join(headers.get(b"transfer-encoding", [])).lower()
    if encodings:
        if encodings.split(b",")[-1].strip() == b"chunked":
            return "chunked", 0
        return "opaque", 0
    lengths = headers.get(b"content-length", [])
    if lengths:
        if len(set(lengths)) != 1 or not lengths[0].isdigit():
            return "opaque", 0
        return "length", int(lengths[0])
    return "absent", 0


def _send(sock, data, capture):
    sock.sendall(data)
    if capture is not None:
        capture.add(data)


def _copy_exact(reader, destination, amount, capture):
    while amount:
        data = reader.read(min(amount, 65536))
        if not data:
            raise EOFError("Incomplete HTTP body")
        _send(destination, data, capture)
        amount -= len(data)


def _copy_to_eof(reader, destination, capture=None):
    while True:
        data = reader.read()
        if not data:
            return
        _send(destination, data, capture)


def _copy_body(reader, destination, framing, capture):
    mode, amount = framing
    if mode == "length":
        _copy_exact(reader, destination, amount, capture)
    elif mode == "chunked":
        while True:
            try:
                line = reader.line()
            except _Unparsed as exc:
                destination.sendall(exc.data)
                capture.data = None
                _copy_to_eof(reader, destination)
                return False
            if not line:
                raise EOFError("Incomplete chunk framing")
            _send(destination, line, capture)
            try:
                amount = int(line.split(b";", 1)[0].strip(), 16)
                if amount < 0:
                    raise ValueError()
            except ValueError:
                # Parsing stops; the remaining traffic still reaches the peer.
                capture.data = None
                _copy_to_eof(reader, destination)
                return False
            if not amount:
                while True:
                    try:
                        trailer = reader.line()
                    except _Unparsed as exc:
                        destination.sendall(exc.data)
                        capture.data = None
                        _copy_to_eof(reader, destination)
                        return False
                    if not trailer:
                        raise EOFError("Incomplete chunk trailers")
                    _send(destination, trailer, capture)
                    if trailer in (b"\r\n", b"\n"):
                        return True
            else:
                _copy_exact(reader, destination, amount + 2, capture)
    elif mode == "opaque":
        capture.data = None
        _copy_to_eof(reader, destination)
        return False
    return True


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        # Connection failures are confined to the connection, not the runtime.
        pass


class PlainHttpProxy:
    def __init__(self, target, on_exchange, *, max_capture_bytes=MAX_EVIDENCE_BYTES):
        self.target, self.on_exchange = target, on_exchange
        self.max_capture_bytes = max_capture_bytes
        self._origin = _origin(target)
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._ready = threading.Condition(self._lock)
        self._binding = None
        self._token = 0
        self._pending = deque()
        self._sockets = set()
        self._closed = False
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._serve(self.request)

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.listen_address = self.server.server_address
        self.address = f"http://127.0.0.1:{self.listen_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        kwargs={"poll_interval": .05}, daemon=True)
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._thread.start()
        self._writer.start()

    def activate(self, binding):
        with self._write_lock:
            with self._lock:
                self._token += 1
                self._binding = binding
                return self._token

    def deactivate(self, token):
        with self._write_lock:
            while True:
                with self._lock:
                    if token != self._token:
                        return
                    if not self._pending:
                        self._binding = None
                        return
                    item = self._pending.popleft()
                # This lock is separate from the forwarding/binding state lock.
                self._deliver(item)

    def _snapshot(self):
        with self._lock:
            return self._token, self._binding

    def _deliver(self, item):
        token, binding, request, response, url = item
        with self._lock:
            eligible = binding is not None and token == self._token and self._binding is binding
        if eligible:
            try:
                self.on_exchange(request, response, url, binding)
            except Exception:
                # A recorder failure may lose evidence, never the network operation.
                pass

    def _write_loop(self):
        while True:
            with self._ready:
                while not self._pending and not self._closed:
                    self._ready.wait()
                if self._closed:
                    return
            with self._write_lock:
                with self._lock:
                    item = self._pending.popleft() if self._pending else None
                if item is not None:
                    self._deliver(item)

    def _complete(self, snapshot, request, response, url):
        token, binding = snapshot
        if request.data is None or response.data is None or _origin(url) != self._origin:
            return
        with self._ready:
            if binding is not None and token == self._token and self._binding is binding and len(self._pending) < 8:
                self._pending.append((token, binding, bytes(request.data), bytes(response.data), url))
                self._ready.notify()

    def _track(self, sock):
        with self._lock:
            self._sockets.add(sock)
        return sock

    def _dispose(self, sock):
        if sock is not None:
            with self._lock:
                self._sockets.discard(sock)
            try:
                sock.close()
            except OSError:
                pass

    def _tunnel(self, client, upstream, client_reader, upstream_reader):
        if client_reader.buffer:
            upstream.sendall(client_reader.read(len(client_reader.buffer)))
        if upstream_reader.buffer:
            client.sendall(upstream_reader.read(len(upstream_reader.buffer)))
        sources = {client: upstream, upstream: client}
        while sources and not self._closed:
            readable, _, _ = select.select(list(sources), [], [], .5)
            for source in readable:
                data = source.recv(65536)
                destination = sources[source]
                if data:
                    destination.sendall(data)
                else:
                    del sources[source]
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

    def _response(self, reader, client, method, capture, outcome):
        try:
            while True:
                try:
                    head = _head(reader)
                except _Unparsed as exc:
                    client.sendall(exc.data)
                    capture.data = None
                    _copy_to_eof(reader, client)
                    outcome["close"] = True
                    return
                if not head:
                    raise EOFError("Missing HTTP response")
                match = re.match(rb"HTTP/1\.[01] ([0-9]{3})(?: |\r)", head)
                status = int(match[1]) if match else 0
                if not status:
                    client.sendall(head)
                    capture.data = None
                    _copy_to_eof(reader, client)
                    outcome["close"] = True
                    return
                if 100 <= status < 200 and status != 101:
                    client.sendall(head)
                    continue
                _send(client, head, capture)
                headers = _headers(head)
                if status == 101:
                    outcome["tunnel"] = True
                    capture.data = None
                    return
                if method != "HEAD" and status not in (204, 304):
                    framing = _framing(headers)
                    if framing[0] == "absent":
                        _copy_to_eof(reader, client, capture)
                        outcome["close"] = True
                    elif not _copy_body(reader, client, framing, capture):
                        outcome["close"] = True
                outcome["close"] = outcome.get("close", False) or b"close" in b",".join(headers.get(b"connection", [])).lower() or head.startswith(b"HTTP/1.0")
                outcome["complete"] = True
                return
        except (OSError, EOFError, ValueError):
            capture.data = None
            outcome["close"] = True
        finally:
            if outcome.get("close"):
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    def _serve(self, client):
        self._track(client)
        reader = _Reader(client, self._snapshot)
        upstream = None
        upstream_reader = None
        destination = None
        try:
            while not self._closed:
                if not reader.start():
                    break
                # Retain the binding of buffered/pipelined bytes. A later exec
                # must not inherit a request read along with the previous one.
                snapshot = reader.binding
                opaque = False
                try:
                    first_line = reader.line()
                    head = _head(reader, first_line)
                except _Unparsed as exc:
                    head = exc.data
                    opaque = True
                first, separator, rest = head.partition(b"\r\n")
                method, raw_url, version = first.split(b" ", 2)
                if method == b"CONNECT":
                    parts = urlsplit("//" + raw_url.decode("ascii"))
                    self._dispose(upstream)
                    upstream = self._track(socket.create_connection((parts.hostname, parts.port or 443), timeout=30))
                    upstream.settimeout(None)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._tunnel(client, upstream, reader, _Reader(upstream))
                    return
                url = raw_url.decode("ascii")
                if not url.startswith(("http://", "https://")):
                    hosts = _headers(head).get(b"host", [])
                    if hosts:
                        url = "http://" + hosts[0].decode("ascii") + url
                    else:
                        # HTTP/2 prior knowledge and unparsed target traffic cannot
                        # be measured, but remain opaque byte streams to the target.
                        url = self.target
                        opaque = True
                parts = urlsplit(url)
                address = (parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
                if upstream is None or destination != address:
                    self._dispose(upstream)
                    upstream = self._track(socket.create_connection(address, timeout=30))
                    upstream.settimeout(None)
                    upstream_reader = _Reader(upstream)
                    destination = address
                path = urlunsplit(("", "", parts.path or "/", parts.query, "")).encode("ascii")
                outgoing = head if version not in (b"HTTP/1.0", b"HTTP/1.1") else method + b" " + path + b" " + version + separator + rest
                request_capture = _Bytes(self.max_capture_bytes)
                response_capture = _Bytes(self.max_capture_bytes)
                _send(upstream, outgoing, request_capture)
                if opaque or version not in (b"HTTP/1.0", b"HTTP/1.1"):
                    self._tunnel(client, upstream, reader, upstream_reader)
                    return
                request_framing = _framing(_headers(head))
                if request_framing[0] == "opaque":
                    self._tunnel(client, upstream, reader, upstream_reader)
                    return
                outcome = {}
                response_thread = threading.Thread(target=self._response,
                    args=(upstream_reader, client, method.decode("ascii"), response_capture, outcome), daemon=True)
                response_thread.start()
                _copy_body(reader, upstream, request_framing, request_capture)
                response_thread.join()
                if outcome.get("tunnel"):
                    self._tunnel(client, upstream, reader, upstream_reader)
                    return
                if outcome.get("complete"):
                    self._complete(snapshot, request_capture, response_capture, url)
                if outcome.get("close") or b"close" in b",".join(_headers(head).get(b"connection", [])).lower():
                    self._dispose(upstream)
                    upstream = None
                    return
        except (OSError, EOFError, ValueError, IndexError, UnicodeError):
            pass
        finally:
            self._dispose(upstream)
            self._dispose(client)

    def close(self):
        with self._ready:
            if self._closed:
                return
            self._binding = None
            self._pending.clear()
            self._closed = True
            sockets = list(self._sockets)
            self._ready.notify_all()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._dispose(sock)
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)
        self._writer.join(timeout=2)

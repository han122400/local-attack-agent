import http.client
import socket
import ssl
import tempfile
import threading
import time
import unittest
import datetime
import hashlib
import ipaddress
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.http_proxy_capture import PlainHttpProxy
from engine.http_capture import exchange_facts


class PlainHttpProxyTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                seen.append(self.path)
                if self.path == "/slow":
                    time.sleep(.2)
                if self.path == "/chunked":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n1;ext=yes\r\n[\r\n1\r\n]\r\n0\r\nX-Trailer: kept\r\n\r\n")
                else:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n[]")

            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = b""
                while True:
                    line = self.rfile.readline()
                    size = int(line.split(b";", 1)[0], 16)
                    if not size:
                        self.rfile.readline()
                        break
                    payload += self.rfile.read(size)
                    self.rfile.read(2)
                seen.append(payload)
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.target = f"http://127.0.0.1:{self.server.server_port}"
        self.records = []
        self.proxy = PlainHttpProxy(self.target, lambda *args: self.records.append(args))
        self.addCleanup(self.proxy.close)
        self.token = self.proxy.activate({"attempt_id": "A1"})

    def connection(self):
        connection = http.client.HTTPConnection(*self.proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def wait_records(self, count=1):
        deadline = time.monotonic() + 2
        while len(self.records) < count and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.records), count)

    def test_nonhelper_http_request_records_actual_origin_bytes_and_facts(self):
        connection = self.connection()
        connection.request("GET", self.target + "/items")
        self.assertEqual(connection.getresponse().read(), b"[]")
        self.wait_records()
        request, response, url, binding = self.records[0]
        self.assertTrue(request.startswith(b"GET /items HTTP/1.1\r\n"))
        self.assertEqual(self.seen, ["/items"])
        self.assertEqual(binding, {"attempt_id": "A1"})
        self.assertEqual(exchange_facts(request, response, url, "anonymous")["json_counts"], {"": 0})

    def test_persistent_connection_preserves_chunk_framing(self):
        connection = self.connection()
        for path in ("/chunked", "/items"):
            connection.request("GET", self.target + path)
            self.assertEqual(connection.getresponse().read(), b"[]")
        self.wait_records(2)
        self.assertIn(b"1;ext=yes\r\n[\r\n1\r\n]\r\n0\r\nX-Trailer: kept", self.records[0][1])

    def test_cross_origin_request_on_same_connection_is_forwarded_not_recorded(self):
        connection = self.connection()
        connection.request("GET", self.target + "/items")
        self.assertEqual(connection.getresponse().read(), b"[]")
        connection.request("GET", self.target.replace("127.0.0.1", "localhost") + "/other")
        self.assertEqual(connection.getresponse().read(), b"[]")
        self.wait_records()
        self.assertEqual(self.seen, ["/items", "/other"])

    def test_late_response_is_not_bound_to_next_exec(self):
        connection = self.connection()
        connection.request("GET", self.target + "/slow")
        deadline = time.monotonic() + 1
        while not self.seen and time.monotonic() < deadline:
            time.sleep(.01)
        self.proxy.deactivate(self.token)
        self.proxy.activate({"attempt_id": "A2"})
        self.assertEqual(connection.getresponse().read(), b"[]")
        time.sleep(.05)
        self.assertEqual(self.records, [])

    def test_recording_error_does_not_fail_network_request(self):
        def broken(*args):
            raise OSError("disk full")
        proxy = PlainHttpProxy(self.target, broken)
        self.addCleanup(proxy.close)
        proxy.activate({})
        connection = http.client.HTTPConnection(*proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", self.target + "/items")
        self.assertEqual(connection.getresponse().read(), b"[]")

    def test_capture_size_limit_drops_record_without_truncating_response(self):
        proxy = PlainHttpProxy(self.target, lambda *args: self.records.append(args), max_capture_bytes=16)
        self.addCleanup(proxy.close)
        proxy.activate({})
        connection = http.client.HTTPConnection(*proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", self.target + "/items")
        self.assertEqual(connection.getresponse().read(), b"[]")
        time.sleep(.05)
        self.assertEqual(self.records, [])

    def test_connect_is_opaque_unrecorded_tunnel(self):
        connection = socket.create_connection(self.proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        authority = f"127.0.0.1:{self.server.server_port}"
        connection.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += connection.recv(1024)
        self.assertIn(b" 200 ", response)
        connection.sendall(b"GET /items HTTP/1.1\r\nHost: example\r\nConnection: close\r\n\r\n")
        data = b""
        while True:
            chunk = connection.recv(1024)
            if not chunk:
                break
            data += chunk
        self.assertTrue(data.endswith(b"[]"))
        self.assertEqual(self.records, [])

    def test_partial_headers_cannot_rebind_a_request_to_next_exec(self):
        connection = socket.create_connection(self.proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.sendall(f"GET {self.target}/items HTTP/1.1\r\n".encode())
        time.sleep(.05)
        self.proxy.deactivate(self.token)
        self.proxy.activate({"attempt_id": "A2"})
        connection.sendall(f"Host: 127.0.0.1:{self.server.server_port}\r\nConnection: close\r\n\r\n".encode())
        response = b""
        while True:
            data = connection.recv(1024)
            if not data:
                break
            response += data
        self.assertTrue(response.endswith(b"[]"))
        time.sleep(.05)
        self.assertEqual(self.records, [])

    def test_unparsed_http2_preface_passes_to_target_without_capture(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        preface = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
        def echo():
            peer, _ = listener.accept()
            with peer:
                payload = b""
                while len(payload) < len(preface):
                    payload += peer.recv(1024)
                peer.sendall(payload)
        threading.Thread(target=echo, daemon=True).start()
        proxy = PlainHttpProxy(f"http://127.0.0.1:{listener.getsockname()[1]}/", lambda *args: self.records.append(args))
        self.addCleanup(proxy.close)
        proxy.activate({})
        connection = socket.create_connection(proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.sendall(preface)
        self.assertEqual(connection.recv(1024), preface)
        self.assertEqual(self.records, [])

    def test_first_tls_connection_keeps_origin_certificate_without_mitm(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "127.0.0.1")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as directory:
            cert_path, key_path = Path(directory) / "server.pem", Path(directory) / "server.key"
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                                   serialization.NoEncryption()))
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert_path, key_path)
            tls_server = ThreadingHTTPServer(("127.0.0.1", 0), self.server.RequestHandlerClass)
            tls_server.daemon_threads = True
            tls_server.socket = server_context.wrap_socket(tls_server.socket, server_side=True)
            threading.Thread(target=tls_server.serve_forever, daemon=True).start()
            try:
                connection = http.client.HTTPSConnection(*self.proxy.listen_address,
                    context=ssl.create_default_context(cafile=str(cert_path)), timeout=3)
                connection.set_tunnel("127.0.0.1", tls_server.server_port)
                try:
                    connection.connect()
                    expected = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
                    self.assertEqual(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest(), expected)
                    connection.request("GET", "/items")
                    self.assertEqual(connection.getresponse().read(), b"[]")
                finally:
                    connection.close()
            finally:
                tls_server.shutdown()
                tls_server.server_close()
        self.assertEqual(self.records, [])

    def test_slow_recorder_does_not_stall_other_connections(self):
        entered, release = threading.Event(), threading.Event()
        def record(*args):
            entered.set()
            release.wait(3)
        proxy = PlainHttpProxy(self.target, record)
        self.addCleanup(proxy.close)
        proxy.activate({})
        try:
            first = http.client.HTTPConnection(*proxy.listen_address, timeout=1)
            self.addCleanup(first.close)
            first.request("GET", self.target + "/items")
            self.assertEqual(first.getresponse().read(), b"[]")
            self.assertTrue(entered.wait(1))
            second = http.client.HTTPConnection(*proxy.listen_address, timeout=.5)
            self.addCleanup(second.close)
            second.request("GET", self.target + "/items")
            self.assertEqual(second.getresponse().read(), b"[]")
        finally:
            release.set()

    def test_oversized_headers_are_forwarded_without_recording(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        received = []
        def serve():
            peer, _ = listener.accept()
            with peer:
                payload = b""
                while b"\r\n\r\n" not in payload:
                    payload += peer.recv(65536)
                received.append(payload)
                peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]")
        threading.Thread(target=serve, daemon=True).start()
        target = f"http://127.0.0.1:{listener.getsockname()[1]}"
        proxy = PlainHttpProxy(target, lambda *args: self.records.append(args))
        self.addCleanup(proxy.close)
        proxy.activate({})
        connection = http.client.HTTPConnection(*proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", target + "/", headers={"X-Large": "a" * 100000})
        self.assertEqual(connection.getresponse().read(), b"[]")
        self.assertIn(b"X-Large: " + b"a" * 100000, received[0])
        self.assertEqual(self.records, [])

    def test_expect_continue_chunked_post_preserves_request_bytes(self):
        connection = socket.create_connection(self.proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.sendall((f"POST {self.target}/items HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                            "Expect: 100-continue\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode())
        interim = b""
        while b"\r\n\r\n" not in interim:
            interim += connection.recv(1024)
        self.assertIn(b" 100 ", interim)
        wire_body = b"2;original=yes\r\nab\r\n1\r\nc\r\n0\r\n\r\n"
        connection.sendall(wire_body)
        final = b""
        while True:
            chunk = connection.recv(1024)
            if not chunk:
                break
            final += chunk
        self.assertTrue(final.endswith(b"[]"))
        self.assertEqual(self.seen, [b"abc"])
        self.wait_records()
        self.assertTrue(self.records[0][0].endswith(wire_body))
        self.assertNotIn(b"100 Continue", self.records[0][1])

    def test_unparseable_chunk_line_is_forwarded_not_rejected_by_recorder(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        bad_line = b"z" * 100000 + b"\r\n"
        received = []
        def serve():
            peer, _ = listener.accept()
            with peer:
                payload = b""
                while not payload.endswith(bad_line):
                    data = peer.recv(65536)
                    if not data:
                        return
                    payload += data
                received.append(payload)
                peer.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 3\r\nConnection: close\r\n\r\nbad")
        threading.Thread(target=serve, daemon=True).start()
        target = f"http://127.0.0.1:{listener.getsockname()[1]}"
        proxy = PlainHttpProxy(target, lambda *args: self.records.append(args))
        self.addCleanup(proxy.close)
        proxy.activate({})
        connection = socket.create_connection(proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        connection.sendall((f"POST {target}/ HTTP/1.1\r\nHost: 127.0.0.1:{listener.getsockname()[1]}\r\n"
                            "Transfer-Encoding: chunked\r\n\r\n").encode() + bad_line)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            data = connection.recv(1024)
            if not data:
                break
            response += data
        self.assertTrue(response.endswith(b"bad"))
        self.assertTrue(received[0].endswith(bad_line))
        self.assertEqual(self.records, [])

    def test_buffered_pipeline_request_does_not_inherit_next_exec(self):
        connection = socket.create_connection(self.proxy.listen_address, timeout=3)
        self.addCleanup(connection.close)
        host = f"127.0.0.1:{self.server.server_port}"
        connection.sendall((f"GET {self.target}/slow HTTP/1.1\r\nHost: {host}\r\n\r\n"
                            f"GET {self.target}/items HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n").encode())
        deadline = time.monotonic() + 1
        while not self.seen and time.monotonic() < deadline:
            time.sleep(.01)
        self.proxy.deactivate(self.token)
        self.proxy.activate({"attempt_id": "A2"})
        response = b""
        while True:
            data = connection.recv(1024)
            if not data:
                break
            response += data
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)
        self.assertEqual(self.seen, ["/slow", "/items"])
        time.sleep(.05)
        self.assertEqual(self.records, [])


if __name__ == "__main__":
    unittest.main()

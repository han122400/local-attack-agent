"""Optional backend checks; run using the isolated requirements-mitm environment."""

import base64
import datetime
import http.client
import importlib.util
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine.http_mitm_capture import OptionalMitmProxy


@unittest.skipUnless(importlib.util.find_spec("mitmproxy"), "optional mitmproxy environment required")
class OptionalMitmTests(unittest.TestCase):
    def setUp(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        self.cert = folder / "origin.pem"
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keypath = folder / "origin-key.pem"
        keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.TraditionalOpenSSL,
                                             serialization.NoEncryption()))
        self.release, self.arrived = threading.Event(), threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if self.path == "/slow":
                    owner.arrived.set()
                    owner.release.wait(5)
                body = b'x' * 100000 if self.path == "/large" else b'{"items":[1,2]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, keypath)
        self.context, self.handler = context, Handler
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.observed = []
        self.observed_ready = threading.Event()
        def collected(*item):
            self.observed.append(item)
            self.observed_ready.set()
        self.proxy = OptionalMitmProxy(
            f"https://localhost:{self.server.server_port}/", collected,
            folder / "_ca", upstream_trusted_ca=self.cert)
        self.addCleanup(self.proxy.close)

    def request(self, path="/", *, trust_proxy=True, port=None):
        from urllib.parse import urlsplit

        address = urlsplit(self.proxy.address)
        context = ssl.create_default_context(cafile=str(self.cert))
        if trust_proxy:
            context.load_verify_locations(cafile=str(self.proxy.ca_file))
        connection = http.client.HTTPSConnection(address.hostname, address.port, context=context, timeout=5)
        try:
            connection.set_tunnel("localhost", port or self.server.server_port)
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_tls_opt_in_records_decoded_snapshot_separate_from_literal_contract(self):
        binding = {"exec_id": "EXEC-0001", "attempt_id": "A0001"}
        token = self.proxy.activate(binding)
        self.assertEqual(self.request(), (200, b'{"items":[1,2]}'))
        self.assertTrue(self.observed_ready.wait(2))
        self.proxy.deactivate(token)
        self.assertEqual(len(self.observed), 1)
        snapshot, identity = self.observed[0]
        self.assertEqual(identity, binding)
        self.assertEqual(snapshot["representation"], "mitmproxy_reconstructed")
        self.assertIs(snapshot["literal"], False)
        self.assertIs(snapshot["verification_eligible"], False)
        self.assertEqual(base64.b64decode(snapshot["response"]["body_base64"]), b'{"items":[1,2]}')
        self.assertNotIn("facts", snapshot)

    def test_client_rejects_first_mitm_certificate_then_host_is_tunneled(self):
        self.proxy.activate({"exec_id": "EXEC-0001"})
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.request(trust_proxy=False)
        time.sleep(.15)
        self.assertEqual(self.request(trust_proxy=False), (200, b'{"items":[1,2]}'))
        self.assertEqual(self.observed, [])

    def test_late_response_cannot_be_attributed_to_next_exec(self):
        old = self.proxy.activate({"exec_id": "EXEC-0001"})
        results = []
        worker = threading.Thread(target=lambda: results.append(self.request("/slow")))
        worker.start()
        self.assertTrue(self.arrived.wait(3))
        self.proxy.deactivate(old)
        self.proxy.activate({"exec_id": "EXEC-0002"})
        self.release.set()
        worker.join(5)
        self.assertEqual(results, [(200, b'{"items":[1,2]}')])
        self.assertEqual(self.observed, [])

    def test_large_response_flows_without_retaining_body(self):
        self.proxy.activate({"exec_id": "EXEC-0001"})
        status, body = self.request("/large")
        self.assertEqual((status, len(body)), (200, 100000))
        self.assertTrue(self.observed_ready.wait(2))
        self.assertEqual(len(self.observed), 1)
        self.assertNotIn("body_base64", self.observed[0][0]["response"])
        self.assertEqual(self.observed[0][0]["response"]["body_omitted"], "size_limit")

    def test_observation_callback_failure_does_not_fail_request(self):
        def failing(*args):
            raise OSError("recorder unavailable")
        self.proxy.on_observation = failing
        self.proxy.activate({"exec_id": "EXEC-0001"})
        self.assertEqual(self.request(), (200, b'{"items":[1,2]}'))

    def test_other_origin_is_tunneled_without_interception_or_recording(self):
        other = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        other.daemon_threads = True
        other.socket = self.context.wrap_socket(other.socket, server_side=True)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            token = self.proxy.activate({"exec_id": "EXEC-0001"})
            self.assertEqual(self.request(trust_proxy=False, port=other.server_port),
                             (200, b'{"items":[1,2]}'))
            self.proxy.deactivate(token)
            self.assertEqual(self.observed, [])
        finally:
            other.shutdown()
            other.server_close()

    def test_no_active_execution_means_no_observation(self):
        self.assertEqual(self.request(), (200, b'{"items":[1,2]}'))
        self.assertEqual(self.observed, [])

    def test_opaque_binding_with_lock_keeps_identity(self):
        binding = threading.RLock()
        token = self.proxy.activate(binding)
        self.assertEqual(self.request(), (200, b'{"items":[1,2]}'))
        self.assertTrue(self.observed_ready.wait(2))
        self.proxy.deactivate(token)
        self.assertIs(self.observed[0][1], binding)

    def test_failed_deactivation_still_discards_following_observations(self):
        from unittest.mock import patch

        token = self.proxy.activate(object())
        with patch.object(self.proxy, "_command", side_effect=OSError("control lost")):
            with self.assertRaises(OSError):
                self.proxy.deactivate(token)
        self.assertEqual(self.request(), (200, b'{"items":[1,2]}'))
        self.assertFalse(self.observed_ready.wait(.5))
        self.assertEqual(self.observed, [])

    def test_failed_activation_acknowledgement_does_not_leave_binding(self):
        from unittest.mock import patch

        original = self.proxy._command
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("acknowledgement lost")
        with patch.object(self.proxy, "_command", side_effect=lost_ack):
            with self.assertRaises(OSError):
                self.proxy.activate(object())
        self.assertEqual(self.request(), (200, b'{"items":[1,2]}'))
        self.assertFalse(self.observed_ready.wait(.5))
        self.assertEqual(self.observed, [])


if __name__ == "__main__":
    unittest.main()

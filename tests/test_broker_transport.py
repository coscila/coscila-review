"""Exercise the research broker transport with real local Unix sockets and TLS.

The fixture never resolves a hostname or opens an Internet socket. Its private
test certificate is trusted explicitly, so transport success still depends on
normal certificate and hostname verification.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from ocsl_review.errors import ConfidentialityError, ProviderError
from ocsl_review.fulltext import MAX_REDIRECTS, open_public_https
from ocsl_review.scholarly import HttpTransport


BROKER_ENV = "PUPPENCLAW_ORCHESTRATION_SOCKET"
DOCUMENT = b"<article><body><p>Verified source text.</p></body></article>"


def origin_response(body=DOCUMENT, *, status=200, headers=()):
    fields = [("Content-Length", str(len(body))),
              ("Content-Type", "application/xml"), ("Connection", "close")]
    fields.extend(headers)
    return (f"HTTP/1.1 {status} Fixture\r\n" +
            "".join(f"{key}: {value}\r\n" for key, value in fields) +
            "\r\n").encode("ascii") + body


class UnixTlsBroker:
    """One local fixture connection per declared response, without DNS."""

    def __init__(self, context, steps):
        self.context = context
        self.steps = steps
        self.connect_requests = []
        self.origin_requests = []
        self.server_names = []
        self.errors = []
        self._stopping = threading.Event()
        self._directory = tempfile.TemporaryDirectory(prefix="ocsl-broker-")
        self.path = str(Path(self._directory.name) / "research.sock")
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.settimeout(0.1)
        try:
            self._listener.bind(self.path)
            self._listener.listen(4)
        except Exception:
            self._listener.close()
            self._directory.cleanup()
            raise
        self.context.set_servername_callback(
            lambda _socket, name, _context: self.server_names.append(name))
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._stopping.set()
        self._listener.close()
        self._thread.join(timeout=3)
        self._directory.cleanup()
        if self._thread.is_alive():
            raise AssertionError("local broker fixture did not terminate")
        if self.errors:
            raise AssertionError(f"local broker fixture failed: {self.errors}")

    @staticmethod
    def _headers(peer):
        data = bytearray()
        while not data.endswith(b"\r\n\r\n"):
            part = peer.recv(1)
            if not part:
                raise OSError("fixture peer closed before completing its headers")
            data.extend(part)
            if len(data) > 16 * 1024:
                raise AssertionError("fixture received an oversized request")
        return bytes(data)

    def _serve(self):
        for step in self.steps:
            while not self._stopping.is_set():
                try:
                    peer, _address = self._listener.accept()
                    break
                except socket.timeout:
                    continue
                except OSError:
                    return
            else:
                return
            try:
                with peer:
                    peer.settimeout(2)
                    self.connect_requests.append(self._headers(peer))
                    if step.get("close"):
                        continue
                    if step.get("slow_headers"):
                        for byte in b"HTTP/1.1 200 Connection Established\r\nX-Slow: " + b"x" * 100:
                            peer.sendall(bytes([byte]))
                            if self._stopping.wait(0.03):
                                return
                        continue
                    if "connect_response" in step:
                        peer.sendall(step["connect_response"])
                        continue
                    peer.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    if step.get("no_tls"):
                        self._stopping.wait(2)
                        continue
                    with self.context.wrap_socket(peer, server_side=True) as secure:
                        self.origin_requests.append(self._headers(secure))
                        secure.sendall(step.get("response", origin_response()))
            except (OSError, ssl.SSLError):
                # Certificate rejection, deadline, and early-close cases are
                # intentionally exercised by the client tests below.
                pass
            except Exception as exc:
                self.errors.append(repr(exc))


@unittest.skipUnless(hasattr(socket, "AF_UNIX") and shutil.which("openssl"),
                     "local Unix-socket/TLS fixture requires openssl")
class BrokerTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_directory = tempfile.TemporaryDirectory(prefix="ocsl-broker-cert-")
        cls.certificate = Path(cls.cert_directory.name) / "fixture.crt"
        cls.key = Path(cls.cert_directory.name) / "fixture.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(cls.key), "-out", str(cls.certificate), "-days", "1",
            "-subj", "/CN=papers.example",
            "-addext", "subjectAltName=DNS:papers.example,DNS:archive.example",
        ], check=True, capture_output=True, timeout=15)
        cls.default_context = staticmethod(ssl.create_default_context)

    @classmethod
    def tearDownClass(cls):
        cls.cert_directory.cleanup()

    def broker(self, steps):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.key)
        return UnixTlsBroker(context, steps)

    @contextlib.contextmanager
    def transport(self, path, *, trust_fixture=True):
        context = self.default_context(cafile=str(self.certificate)) if trust_fixture else self.default_context()
        with (
            patch.dict(os.environ, {BROKER_ENV: path}),
            patch("ocsl_review.fulltext.ssl.create_default_context", return_value=context),
            patch("ocsl_review.fulltext.socket.getaddrinfo", side_effect=AssertionError("client DNS is forbidden")) as dns,
            patch("ocsl_review.fulltext.socket.create_connection", side_effect=AssertionError("direct network fallback is forbidden")) as direct,
        ):
            yield
            dns.assert_not_called()
            direct.assert_not_called()

    @staticmethod
    def fetch(url="https://papers.example/article.xml", **kwargs):
        with open_public_https(urllib.request.Request(url), timeout=kwargs.pop("timeout", 2), **kwargs) as response:
            return response.read(len(DOCUMENT) + 1)

    def test_real_tls_through_unix_broker_without_client_dns(self):
        with self.broker([{}]) as broker, self.transport(broker.path):
            self.assertEqual(self.fetch(), DOCUMENT)
            self.assertEqual(broker.server_names, ["papers.example"])
            self.assertTrue(broker.connect_requests[0].startswith(b"CONNECT papers.example:443 HTTP/1."))
            self.assertIn(b"Host: papers.example:443\r\n", broker.connect_requests[0])
            self.assertNotIn(b"Authorization", broker.connect_requests[0])
            self.assertTrue(broker.origin_requests[0].startswith(b"GET /article.xml HTTP/1.1\r\n"))

    def test_trailing_dot_uses_matching_canonical_connect_and_sni(self):
        with self.broker([{}]) as broker, self.transport(broker.path):
            self.assertEqual(self.fetch("https://PAPERS.EXAMPLE./article.xml"), DOCUMENT)
            self.assertEqual(broker.server_names, ["papers.example"])
            self.assertTrue(broker.connect_requests[0].startswith(b"CONNECT papers.example:443 "))

    def test_broker_does_not_disable_certificate_trust_verification(self):
        with self.broker([{}]) as broker, self.transport(broker.path, trust_fixture=False):
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.fetch()
            self.assertEqual(broker.origin_requests, [])

    def test_broker_does_not_disable_certificate_hostname_verification(self):
        with self.broker([{}]) as broker, self.transport(broker.path):
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.fetch("https://different.example/article.xml")
            self.assertEqual(broker.origin_requests, [])

    def test_closed_or_denied_connect_never_falls_back(self):
        for step in ({"close": True}, {"connect_response": b"HTTP/1.1 403 Forbidden\r\n\r\n"}):
            with self.subTest(step=step), self.broker([step]) as broker, self.transport(broker.path):
                with self.assertRaises((ProviderError, OSError)):
                    self.fetch()
                self.assertEqual(broker.origin_requests, [])
                self.assertEqual(broker.server_names, [])

    def test_missing_socket_never_falls_back(self):
        with tempfile.TemporaryDirectory(prefix="ocsl-missing-broker-") as root:
            with self.transport(str(Path(root) / "missing.sock")):
                with self.assertRaises(OSError):
                    self.fetch()

    def test_empty_or_relative_broker_configuration_fails_closed(self):
        for path in ("", "relative.sock"):
            with self.subTest(path=path), self.transport(path):
                with self.assertRaises(ProviderError):
                    self.fetch()

    def test_connect_response_header_bound(self):
        response = b"HTTP/1.1 200 Connection Established\r\nX-Oversize: " + b"x" * (65 * 1024) + b"\r\n\r\n"
        with self.broker([{"connect_response": response}]) as broker, self.transport(broker.path):
            with self.assertRaises(ProviderError):
                self.fetch()
            self.assertEqual(broker.server_names, [])

    def test_slow_connect_headers_obey_absolute_deadline(self):
        with self.broker([{"slow_headers": True}]) as broker, self.transport(broker.path):
            started = time.monotonic()
            with self.assertRaises((ProviderError, TimeoutError, OSError)):
                self.fetch(timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.8)

    def test_tls_handshake_obeys_remaining_deadline(self):
        with self.broker([{"no_tls": True}]) as broker, self.transport(broker.path):
            started = time.monotonic()
            with self.assertRaises((ProviderError, TimeoutError, OSError)):
                self.fetch(timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.8)

    def test_redirect_reauthorizes_each_destination_and_strips_credentials(self):
        target = "https://archive.example/fulltext.xml"
        steps = [{"response": origin_response(b"", status=302, headers=[("Location", target)])}, {}]
        authorized = []
        request = urllib.request.Request("https://papers.example/article.xml", headers={
            "Authorization": "Bearer private-fixture-value", "Cookie": "private=fixture", "Referer": "https://private.example/",
        })
        with self.broker(steps) as broker, self.transport(broker.path):
            with open_public_https(request, timeout=2, allow_cross_origin=True, authorize_url=authorized.append) as response:
                self.assertEqual(response.read(len(DOCUMENT) + 1), DOCUMENT)
            self.assertEqual(authorized, [request.full_url, target])
            self.assertEqual(broker.server_names, ["papers.example", "archive.example"])
            for message in broker.origin_requests:
                self.assertNotIn(b"authorization:", message.lower())
                self.assertNotIn(b"cookie:", message.lower())
                self.assertNotIn(b"referer:", message.lower())

    def test_redirect_egress_denial_prevents_next_broker_connection(self):
        target = "https://archive.example/fulltext.xml"
        seen = []

        def authorize(url):
            seen.append(url)
            if url == target:
                raise ConfidentialityError("fixture egress plan denies this destination")

        with self.broker([{"response": origin_response(b"", status=302, headers=[("Location", target)])}]) as broker, self.transport(broker.path):
            with self.assertRaises(ConfidentialityError):
                self.fetch(allow_cross_origin=True, authorize_url=authorize)
            self.assertEqual(len(seen), 2)
            self.assertEqual(len(broker.connect_requests), 1)

    def test_private_redirect_is_rejected_before_next_connection(self):
        with self.broker([{"response": origin_response(b"", status=302, headers=[("Location", "https://127.0.0.1/private")])}]) as broker, self.transport(broker.path):
            with self.assertRaises(ProviderError):
                self.fetch(allow_cross_origin=True)
            self.assertEqual(len(broker.connect_requests), 1)

    def test_redirect_chain_remains_bounded(self):
        redirect = {"response": origin_response(b"", status=302, headers=[("Location", "/again")])}
        with self.broker([redirect] * (MAX_REDIRECTS + 1)) as broker, self.transport(broker.path):
            with self.assertRaisesRegex(ProviderError, "bound"):
                self.fetch()
            self.assertEqual(len(broker.connect_requests), MAX_REDIRECTS + 1)

    def test_provider_size_bound_is_enforced_over_broker(self):
        with self.broker([{}]) as broker, self.transport(broker.path):
            transport = HttpTransport(user_agent="local-fixture", max_attempts=1)
            with self.assertRaisesRegex(ProviderError, "size bound"):
                transport.get("https://papers.example/article.xml", max_response_bytes=10)


if __name__ == "__main__":
    unittest.main()

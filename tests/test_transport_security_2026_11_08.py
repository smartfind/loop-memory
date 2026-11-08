"""Regression tests for the 0.4.21 transport-security guard.

Audit 2026-11-08 (loop-memory 0.4.21).

The defect under test: the SDK's HTTP backend had **no transport
guard at all**.  ``MemoryClient.http(base_url="http://mem.example.com")``
opened a plain ``urllib`` connection and shipped the user's memory
text — and would have shipped an ``Authorization: Bearer`` header —
across the network in cleartext.  On top of that the HTTP backend
could not send a bearer token *at all*, so the only way to talk to a
token-protected server was to turn auth off — the exact configuration
that leaks.

Two upstream projects closed the same hole in the same week:
``rohitg00/agentmemory`` v0.9.32 (2026-11-04, Apache-2.0) made
"hooks send nothing to a remote plain ``http://`` server" a breaking
change, and ``topoteretes/cognee`` v1.6.7 (2026-11-06, Apache-2.0)
refused server paths over HTTP by default.  Both are pattern
references only — no code was copied.

The fix adds :mod:`loop_memory.security.transport`:

* plaintext ``http://`` to a **non-loopback** host is refused;
* plaintext to a **loopback** host stays allowed (the default
  deployment, and the traffic never leaves the machine);
* ``https://`` is always allowed;
* a URL with embedded credentials is refused (it leaks into logs);
* redirects that downgrade ``https://`` → ``http://``, and
  cross-origin redirects that would replay the ``Authorization``
  header, are refused;
* the HTTP backend can finally send the bearer token.

The server half of the story is a new ``loop-memory doctor`` check
(``plaintext_exposure``) that reports a non-loopback bind.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import warnings

from loop_memory.doctor import STATUS_INFO, STATUS_OK, STATUS_WARN, run_checks
from loop_memory.sdk import MemoryClient, MemoryClientError
from loop_memory.sdk_extensions import (
    http_delete_json,
    http_get_json,
    http_post_json,
)
from loop_memory.security.transport import (
    InsecureTransportError,
    assert_secure_transport,
    classify_url,
    install_safe_redirect_handler,
    is_loopback_host,
)
from loop_memory.storage.sqlite_store import MemoryStore


# ---------------------------------------------------------------------------
# A tiny loopback HTTP server used for the wire-level assertions.
# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """Records the request headers and replies with a JSON echo."""

    received: list[dict] = []
    status = 200
    body: bytes = b'{"ok": true}'

    def _reply(self) -> None:
        type(self).received.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    do_GET = _reply
    do_POST = _reply
    do_DELETE = _reply

    def log_message(self, *args) -> None:  # silence the test output
        return


class _NoDNSHTTPServer(http.server.HTTPServer):
    """``HTTPServer`` without the reverse-DNS lookup.

    ``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn()``
    on the bound address, which blocks for ~30 s when no resolver is
    reachable.  The tests must not depend on the machine's DNS, so
    the name is set locally instead.
    """

    def server_bind(self) -> None:  # noqa: D102
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    daemon_threads = True


class _ServerFixture:
    """Start ``_Handler`` on a loopback port for the duration of a test."""

    def __init__(self) -> None:
        _Handler.received = []
        _Handler.status = 200
        _Handler.body = b'{"ok": true}'
        self.httpd = _NoDNSHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _ServerFixture:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ---------------------------------------------------------------------------
# classify_url / is_loopback_host
# ---------------------------------------------------------------------------


class LoopbackHostTests(unittest.TestCase):
    def test_loopback_names_and_addresses_are_loopback(self) -> None:
        for host in ("localhost", "127.0.0.1", "127.0.0.53", "::1", "[::1]",
                     "ip6-localhost"):
            self.assertTrue(is_loopback_host(host), host)

    def test_wildcard_and_remote_hosts_are_not_loopback(self) -> None:
        # 0.0.0.0 / :: are *bind* addresses. A URL pointing at them
        # reaches a remote interface, not this process.
        for host in ("0.0.0.0", "::", "mem.example.com", "10.0.0.5",
                     "192.168.1.10", "host.docker.internal", "", None):
            self.assertFalse(is_loopback_host(host), host)

    def test_ipv6_zone_id_is_ignored(self) -> None:
        self.assertTrue(is_loopback_host("::1%lo0"))


class ClassifyUrlTests(unittest.TestCase):
    def test_https_is_secure(self) -> None:
        v = classify_url("https://mem.example.com")
        self.assertTrue(v.is_secure)
        self.assertFalse(v.is_plaintext_remote)
        self.assertEqual(v.scheme, "https")

    def test_plaintext_loopback_is_secure(self) -> None:
        v = classify_url("http://127.0.0.1:7767")
        self.assertTrue(v.is_secure)
        self.assertTrue(v.is_loopback)
        self.assertFalse(v.is_plaintext_remote)

    def test_plaintext_remote_is_flagged(self) -> None:
        v = classify_url("http://mem.example.com:7767")
        self.assertFalse(v.is_secure)
        self.assertFalse(v.is_loopback)
        self.assertTrue(v.is_plaintext_remote)
        self.assertEqual(v.host, "mem.example.com")
        self.assertEqual(v.port, 7767)

    def test_wildcard_bind_address_counts_as_remote(self) -> None:
        v = classify_url("http://0.0.0.0:7767")
        self.assertTrue(v.is_plaintext_remote)

    def test_unsupported_scheme_is_neither_secure_nor_plaintext(self) -> None:
        v = classify_url("ftp://mem.example.com")
        self.assertFalse(v.is_secure)
        self.assertFalse(v.is_plaintext_remote)
        self.assertIn("unsupported URL scheme", v.reason)

    def test_embedded_credentials_are_detected(self) -> None:
        v = classify_url("http://user:secret@mem.example.com")
        self.assertTrue(v.has_embedded_credentials)

    def test_hostname_is_lowercased_and_brackets_stripped(self) -> None:
        v = classify_url("http://[::1]:7767")
        self.assertTrue(v.is_loopback)
        self.assertEqual(v.host, "::1")


class AssertSecureTransportTests(unittest.TestCase):
    def test_plaintext_remote_raises(self) -> None:
        with self.assertRaises(InsecureTransportError) as ctx:
            assert_secure_transport("http://mem.example.com")
        msg = str(ctx.exception)
        self.assertIn("mem.example.com", msg)
        self.assertIn("cleartext", msg)
        # The message must tell the user how to fix it.
        self.assertIn("https://", msg)
        self.assertIn("allow_insecure", msg)

    def test_loopback_http_is_allowed(self) -> None:
        v = assert_secure_transport("http://127.0.0.1:7767")
        self.assertTrue(v.is_secure)

    def test_https_is_allowed(self) -> None:
        v = assert_secure_transport("https://mem.example.com")
        self.assertTrue(v.is_secure)

    def test_allow_insecure_permits_but_always_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            v = assert_secure_transport(
                "http://mem.example.com", allow_insecure=True)
        self.assertTrue(v.is_plaintext_remote)
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, UserWarning)
        self.assertIn("not encrypted in transit", str(caught[0].message))

    def test_embedded_credentials_are_refused(self) -> None:
        with self.assertRaises(InsecureTransportError) as ctx:
            assert_secure_transport("https://user:secret@mem.example.com")
        self.assertIn("embeds", str(ctx.exception))

    def test_env_var_is_the_deployment_level_opt_in_and_still_warns(self) -> None:
        os.environ["LOOP_MEMORY_ALLOW_INSECURE"] = "1"
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                v = assert_secure_transport("http://mem.example.com")
            self.assertTrue(v.is_plaintext_remote)
            self.assertEqual(len(caught), 1)
            self.assertIs(caught[0].category, UserWarning)
        finally:
            del os.environ["LOOP_MEMORY_ALLOW_INSECURE"]
        # Without the env var the same URL is refused again.
        with self.assertRaises(InsecureTransportError):
            assert_secure_transport("http://mem.example.com")

    def test_env_var_does_not_enable_anything_but_plaintext(self) -> None:
        os.environ["LOOP_MEMORY_ALLOW_INSECURE"] = "1"
        try:
            with self.assertRaises(InsecureTransportError):
                assert_secure_transport("https://user:secret@mem.example.com")
        finally:
            del os.environ["LOOP_MEMORY_ALLOW_INSECURE"]

    def test_unsupported_scheme_is_refused(self) -> None:
        with self.assertRaises(InsecureTransportError):
            assert_secure_transport("ftp://mem.example.com")


# ---------------------------------------------------------------------------
# The SDK HTTP backend
# ---------------------------------------------------------------------------


class HttpClientConstructionTests(unittest.TestCase):
    def test_plaintext_remote_base_url_is_refused(self) -> None:
        with self.assertRaises(InsecureTransportError):
            MemoryClient.http("http://mem.example.com")

    def test_embedded_credentials_are_refused(self) -> None:
        with self.assertRaises(InsecureTransportError):
            MemoryClient.http("https://user:secret@mem.example.com")

    def test_loopback_base_url_is_accepted(self) -> None:
        client = MemoryClient.http("http://127.0.0.1:7767")
        self.assertTrue(client._transport.is_secure)

    def test_https_base_url_is_accepted(self) -> None:
        client = MemoryClient.http("https://mem.example.com")
        self.assertTrue(client._transport.is_secure)

    def test_allow_insecure_opt_in_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            MemoryClient.http("http://mem.example.com", allow_insecure=True)
        self.assertTrue(any(w.category is UserWarning for w in caught))


class HttpClientWireTests(unittest.TestCase):
    def test_bearer_token_is_sent(self) -> None:
        with _ServerFixture() as srv:
            client = MemoryClient.http(srv.base_url, token="s3cr3t-token")
            client._request("GET", "/api/healthz")
        headers = _Handler.received[-1]["headers"]
        self.assertEqual(headers.get("authorization"), "Bearer s3cr3t-token")

    def test_no_authorization_header_without_a_token(self) -> None:
        with _ServerFixture() as srv:
            client = MemoryClient.http(srv.base_url)
            client._request("GET", "/api/healthz")
        headers = _Handler.received[-1]["headers"]
        self.assertNotIn("authorization", headers)

    def test_requests_still_work_over_the_hardened_opener(self) -> None:
        with _ServerFixture() as srv:
            client = MemoryClient.http(srv.base_url)
            out = client._request("GET", "/api/healthz")
        self.assertEqual(out, {"ok": True})

    def test_the_token_never_appears_in_an_error_message(self) -> None:
        with _ServerFixture() as srv:
            _Handler.status = 500
            _Handler.body = b'{"error": "boom"}'
            try:
                client = MemoryClient.http(srv.base_url, token="s3cr3t-token")
                with self.assertRaises(MemoryClientError) as ctx:
                    client._request("GET", "/api/boom")
            finally:
                _Handler.status = 200
                _Handler.body = b'{"ok": true}'
        self.assertNotIn("s3cr3t-token", str(ctx.exception))


# ---------------------------------------------------------------------------
# Redirect hardening
# ---------------------------------------------------------------------------


class RedirectGuardTests(unittest.TestCase):
    def _handler(self):
        opener = install_safe_redirect_handler()
        handler = None
        for h in opener.handlers:
            if h.__class__.__name__ == "_SafeRedirectHandler":
                handler = h
                break
        self.assertIsNotNone(handler, "the safe redirect handler is installed")
        return handler

    def test_https_to_http_downgrade_is_refused(self) -> None:
        handler = self._handler()
        req = urllib.request.Request("https://mem.example.com/api/x")
        with self.assertRaises(InsecureTransportError) as ctx:
            handler.redirect_request(
                req, None, 302, "Found", {},
                "http://evil.example.com/api/x")
        self.assertIn("plaintext", str(ctx.exception).lower())

    def test_cross_origin_redirect_with_a_token_is_refused(self) -> None:
        handler = self._handler()
        req = urllib.request.Request(
            "https://mem.example.com/api/x",
            headers={"Authorization": "Bearer s3cr3t"})
        with self.assertRaises(InsecureTransportError) as ctx:
            handler.redirect_request(
                req, None, 302, "Found", {},
                "https://evil.example.com/api/x")
        self.assertIn("cross-origin", str(ctx.exception))

    def test_same_origin_redirect_is_allowed(self) -> None:
        handler = self._handler()
        req = urllib.request.Request(
            "https://mem.example.com/api/x",
            headers={"Authorization": "Bearer s3cr3t"})
        out = handler.redirect_request(
            req, None, 302, "Found", {}, "https://mem.example.com/api/y")
        self.assertIsNotNone(out)
        self.assertEqual(out.full_url, "https://mem.example.com/api/y")

    def test_cross_origin_redirect_without_a_token_is_allowed(self) -> None:
        handler = self._handler()
        req = urllib.request.Request("https://mem.example.com/api/x")
        out = handler.redirect_request(
            req, None, 302, "Found", {}, "https://other.example.com/api/y")
        self.assertIsNotNone(out)


# ---------------------------------------------------------------------------
# The shared http_*_json helpers
# ---------------------------------------------------------------------------


class SharedHttpHelperTests(unittest.TestCase):
    def test_plaintext_remote_is_refused_by_every_helper(self) -> None:
        with self.assertRaises(InsecureTransportError):
            http_get_json("http://mem.example.com/api/x")
        with self.assertRaises(InsecureTransportError):
            http_post_json("http://mem.example.com/api/x", {})
        with self.assertRaises(InsecureTransportError):
            http_delete_json("http://mem.example.com/api/x")

    def test_loopback_is_allowed_and_the_token_is_sent(self) -> None:
        with _ServerFixture() as srv:
            http_get_json(f"{srv.base_url}/api/x", token="tok-123")
        headers = _Handler.received[-1]["headers"]
        self.assertEqual(headers.get("authorization"), "Bearer tok-123")


# ---------------------------------------------------------------------------
# doctor: plaintext_exposure
# ---------------------------------------------------------------------------


class PlaintextExposureDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="lm-transport-")
        self.store = MemoryStore(os.path.join(self.dir, "t.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _check(self):
        for c in run_checks(self.store):
            if c.name == "plaintext_exposure":
                return c
        self.fail("plaintext_exposure check did not run")

    def test_missing_binding_reports_info_not_warn(self) -> None:
        c = self._check()
        self.assertEqual(c.status, STATUS_INFO)

    def test_loopback_binding_is_ok(self) -> None:
        self.store.set_setting(
            "loop_memory_serve_bind",
            {"host": "127.0.0.1", "port": 7767, "scheme": "http"})
        c = self._check()
        self.assertEqual(c.status, STATUS_OK)

    def test_non_loopback_binding_warns(self) -> None:
        self.store.set_setting(
            "loop_memory_serve_bind",
            {"host": "0.0.0.0", "port": 7767, "scheme": "http"})
        c = self._check()
        self.assertEqual(c.status, STATUS_WARN)
        self.assertIn("cleartext", c.message)
        self.assertTrue(c.fix_hint)

    def test_non_loopback_binding_with_a_token_says_so(self) -> None:
        self.store.set_setting(
            "loop_memory_serve_bind",
            {"host": "192.168.1.10", "port": 7767, "scheme": "http"})
        self.store.set_setting("loop_memory_auth_token", "hunter2")
        c = self._check()
        self.assertEqual(c.status, STATUS_WARN)
        self.assertIn("bearer token", c.message)
        self.assertTrue(c.details["auth_token_set"])


if __name__ == "__main__":
    unittest.main()

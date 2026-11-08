"""Transport security for the HTTP memory backend.

Audit 2026-11-08 (loop-memory 0.4.21).

Background
----------
``loop-memory serve`` refuses to expose the API on a non-loopback
interface without an auth token, and the store itself redacts
secrets before they are written. But the *client* side had no
transport guard at all: ``MemoryClient.http(base_url=...)`` happily
sent memory text — and would have sent an ``Authorization: Bearer``
header — over a plain ``http://`` connection to **any** host. A
memory store holds the user's private facts by design; shipping them
across a network in cleartext is a confidentiality failure that no
amount of server-side hardening can undo.

Two upstream projects closed the same hole in the same week:

* ``rohitg00/agentmemory`` v0.9.32 (2026-11-04, Apache-2.0) made it
  a **breaking change**: "Hooks send nothing to a remote plain
  ``http://`` server; use HTTPS for a remote server".
* ``topoteretes/cognee`` v1.6.7 (2026-11-06, Apache-2.0) refuses
  "server file paths over HTTP" by default.

Both are pattern references only — no code was copied.

What this module does
---------------------
:func:`classify_url` turns a base URL into a :class:`TransportVerdict`
and :func:`assert_secure_transport` refuses the unsafe combinations:

* ``https://`` anywhere — always fine.
* ``http://`` to a **loopback** host (``127.0.0.0/8``, ``::1``,
  ``localhost``) — fine. That is the default deployment
  (``http://127.0.0.1:7767``) and the traffic never leaves the host.
* ``http://`` to **any other** host — refused, unless the caller
  explicitly opts in with ``allow_insecure=True`` (for a private
  tunnel or a VPN'd LAN where the plaintext never leaves the
  machine). The opt-in is never silent: it warns.

The rule is deliberately about the *destination*, not the scheme.
Binding the server to ``0.0.0.0`` is a server-side decision and is
reported by ``loop-memory doctor``; this module protects the client.

Redirects
---------
:func:`install_safe_redirect_handler` returns a urllib opener whose
redirect handler refuses two things:

* a **scheme downgrade** (``https://`` → ``http://``). ``urllib``'s
  stock handler follows it happily, which would silently undo the
  TLS guarantee one hop away from the server.
* a **cross-origin** redirect while a bearer token is attached.
  ``urllib`` copies the ``Authorization`` header onto the redirect
  request, so a 302 to an attacker's host is a credential leak.

Both refusals raise :class:`InsecureTransportError` so the caller
sees *why* the request did not happen.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
import warnings
from dataclasses import dataclass

__all__ = [
    "ALLOW_INSECURE_ENV",
    "InsecureTransportError",
    "TransportVerdict",
    "classify_url",
    "assert_secure_transport",
    "is_loopback_host",
    "install_safe_redirect_handler",
]


class InsecureTransportError(RuntimeError):
    """Raised when a request would leak memory data over an unsafe
    transport (plaintext to a non-loopback host, a TLS downgrade
    redirect, or a credential-bearing cross-origin redirect)."""


# Hosts that resolve to the local machine. Plaintext to these never
# crosses a network boundary, so the default loopback deployment
# keeps working without an opt-in.
_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def is_loopback_host(host: str | None) -> bool:
    """True when ``host`` names the local machine.

    ``0.0.0.0`` / ``::`` are **not** loopback: they are wildcard
    bind addresses, and a URL pointing at them reaches a remote
    interface rather than this process.
    """
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name in _LOOPBACK_NAMES:
        return True
    # Strip an IPv6 zone id ("fe80::1%en0") before parsing.
    zone = name.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(zone)
    except ValueError:
        return False
    return ip.is_loopback


@dataclass(frozen=True)
class TransportVerdict:
    """The outcome of classifying one base URL."""

    url: str
    scheme: str
    host: str
    port: int | None
    is_loopback: bool
    is_secure: bool          # True for https, and for plaintext loopback
    is_plaintext_remote: bool  # True for http:// to a non-loopback host
    has_embedded_credentials: bool = False
    reason: str = ""

    @property
    def summary(self) -> str:
        if self.is_secure:
            if self.scheme == "https":
                return "https (encrypted)"
            return f"http to loopback {self.host} (never leaves this host)"
        return self.reason or "unsafe transport"


def classify_url(url: str) -> TransportVerdict:
    """Classify ``url`` without touching the network."""
    raw = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return TransportVerdict(
            url=raw, scheme="", host="", port=None, is_loopback=False,
            is_secure=False, is_plaintext_remote=False,
            reason=f"cannot parse URL {raw!r}",
        )
    scheme = (parts.scheme or "").lower()
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    has_creds = bool(parts.username or parts.password)
    loopback = is_loopback_host(host)

    if scheme == "https":
        return TransportVerdict(
            url=raw, scheme=scheme, host=host, port=port, is_loopback=loopback,
            is_secure=True, is_plaintext_remote=False,
            has_embedded_credentials=has_creds,
            reason="https",
        )
    if scheme == "http":
        if loopback:
            return TransportVerdict(
                url=raw, scheme=scheme, host=host, port=port, is_loopback=True,
                is_secure=True, is_plaintext_remote=False,
                has_embedded_credentials=has_creds,
                reason="http to loopback",
            )
        return TransportVerdict(
            url=raw, scheme=scheme, host=host, port=port, is_loopback=False,
            is_secure=False, is_plaintext_remote=True,
            has_embedded_credentials=has_creds,
            reason=(
                f"plaintext http:// to the remote host {host!r} — memory text "
                "and the bearer token would cross the network in cleartext"
            ),
        )
    return TransportVerdict(
        url=raw, scheme=scheme, host=host, port=port, is_loopback=loopback,
        is_secure=False, is_plaintext_remote=False,
        has_embedded_credentials=has_creds,
        reason=(
            f"unsupported URL scheme {scheme or '(none)'!r} — use https:// "
            "or http://127.0.0.1"
        ),
    )


#: Environment opt-in for the plaintext-remote escape hatch. Same
#: contract as ``allow_insecure=True``: it permits the request and it
#: warns. It exists so a deployment (a systemd unit, a launchd
#: plist, a CI job) can express the decision without editing code.
ALLOW_INSECURE_ENV = "LOOP_MEMORY_ALLOW_INSECURE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_allows_insecure() -> bool:
    import os
    return (os.environ.get(ALLOW_INSECURE_ENV) or "").strip().lower() in _TRUTHY


def assert_secure_transport(
    url: str,
    *,
    allow_insecure: bool = False,
    action: str = "send memory data",
) -> TransportVerdict:
    """Return the verdict for ``url``, raising when it is unsafe.

    ``allow_insecure=True`` is the explicit escape hatch for a private
    tunnel / VPN'd LAN. It never fails silently: the caller gets a
    :class:`UserWarning` and a log record, so the decision is visible
    in the transcript that caused it.

    Raises :class:`InsecureTransportError` for a plaintext remote
    host, an unsupported scheme, or a URL with embedded credentials
    (which end up in logs and error messages).
    """
    verdict = classify_url(url)
    if verdict.has_embedded_credentials:
        raise InsecureTransportError(
            f"refusing to {action} to {verdict.host!r}: the URL embeds "
            "credentials (user:password@host). Pass the credential in an "
            "Authorization header instead — a URL with credentials leaks "
            "into logs and error messages."
        )
    if verdict.is_plaintext_remote:
        # The env var is the deployment-level spelling of the same
        # opt-in; it still warns.
        if not allow_insecure and _env_allows_insecure():
            allow_insecure = True
        if not allow_insecure:
            raise InsecureTransportError(
                f"refusing to {action} to {verdict.host!r} over plaintext "
                f"http:// — the memory text and the bearer token would "
                f"cross the network in cleartext.\n"
                f"  Fix: serve the remote instance over https://, or point "
                f"the client at a loopback address.\n"
                f"  If the plaintext genuinely never leaves this machine "
                f"(SSH tunnel, WireGuard, a private LAN), opt in explicitly: "
                f"allow_insecure=True (or LOOP_MEMORY_ALLOW_INSECURE=1)."
            )
        warnings.warn(
            f"loop-memory is {action} to the remote host {verdict.host!r} "
            "over plaintext http:// because allow_insecure was set — the "
            "payload is not encrypted in transit.",
            UserWarning,
            stacklevel=2,
        )
        return verdict
    if not verdict.is_secure:
        raise InsecureTransportError(
            f"refusing to {action} to {verdict.url!r}: {verdict.reason}"
        )
    return verdict


# ---------------------------------------------------------------------------
# Redirect hardening
# ---------------------------------------------------------------------------

try:  # pragma: no cover — urllib is stdlib, the import always works
    import urllib.request as _urllib_request
except Exception:  # pragma: no cover
    _urllib_request = None  # type: ignore[assignment]


def _same_origin(a: urllib.parse.SplitResult, b: urllib.parse.SplitResult) -> bool:
    """True when two URLs share scheme, host and port."""
    def _port(p: urllib.parse.SplitResult) -> int | None:
        try:
            return p.port
        except ValueError:
            return None

    def _norm(p: urllib.parse.SplitResult) -> tuple:
        scheme = (p.scheme or "").lower()
        host = (p.hostname or "").lower()
        port = _port(p)
        if port is None:
            port = 443 if scheme == "https" else 80
        return scheme, host, port

    return _norm(a) == _norm(b)


def install_safe_redirect_handler(opener: object | None = None):
    """Build a urllib opener that refuses unsafe redirects.

    Two rules, both borrowed from how a hostile or misconfigured
    server should not be able to move a request:

    * **No scheme downgrade.** ``https://`` → ``http://`` raises
      :class:`InsecureTransportError`. The stock
      ``HTTPRedirectHandler`` follows it, which would undo the TLS
      guarantee one hop away from the server.
    * **No credential-bearing cross-origin redirect.** ``urllib``
      copies the ``Authorization`` header onto the redirect request,
      so a 302 to another host hands the bearer token to whoever
      controls that host. Redirects that stay on the same origin are
      fine.

    ``opener`` may be an existing :class:`urllib.request.OpenerDirector`
    to install the handler on; a new one is built when omitted.
    """
    if _urllib_request is None:  # pragma: no cover
        raise RuntimeError("urllib is unavailable in this interpreter")

    base = _urllib_request.HTTPRedirectHandler

    class _SafeRedirectHandler(base):  # type: ignore[misc,valid-type]
        """Refuse downgrades and credential-bearing cross-origin hops."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            old = urllib.parse.urlsplit(req.full_url)
            new = urllib.parse.urlsplit(newurl)
            if (old.scheme or "").lower() == "https" and (new.scheme or "").lower() == "http":
                raise InsecureTransportError(
                    f"refusing to follow a redirect from {old.scheme}://{old.hostname} "
                    f"to plaintext {new.scheme}://{new.hostname} — the TLS "
                    "guarantee would be dropped one hop from the server."
                )
            if req.has_header("Authorization") and not _same_origin(old, new):
                raise InsecureTransportError(
                    f"refusing to follow a cross-origin redirect from "
                    f"{old.hostname} to {new.hostname} while an Authorization "
                    "header is attached — urllib would replay the bearer "
                    "token to the new host."
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    if opener is None:
        return _urllib_request.build_opener(_SafeRedirectHandler())
    opener.add_handler(_SafeRedirectHandler())
    return opener

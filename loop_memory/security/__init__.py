"""Security helpers: secret storage and transport guard.

``secrets.py`` holds the cross-platform secret store; ``transport.py``
holds the client-side transport guard added in 0.4.21 (see that module
for the full rationale).
"""
from .transport import (
    InsecureTransportError,
    TransportVerdict,
    assert_secure_transport,
    classify_url,
    install_safe_redirect_handler,
    is_loopback_host,
)
from .secrets import (
    account_for,
    backend_display_name,
    backend_name,
    delete_secret,
    get_secret,
    has_secret,
    set_secret,
)

__all__ = [
    "InsecureTransportError",
    "TransportVerdict",
    "assert_secure_transport",
    "classify_url",
    "install_safe_redirect_handler",
    "is_loopback_host",
    "account_for",
    "backend_display_name",
    "backend_name",
    "delete_secret",
    "get_secret",
    "has_secret",
    "set_secret",
]

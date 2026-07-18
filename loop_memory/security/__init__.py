"""Cross-platform secret storage. See ``secrets.py`` for details."""
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
    "account_for",
    "backend_display_name",
    "backend_name",
    "delete_secret",
    "get_secret",
    "has_secret",
    "set_secret",
]

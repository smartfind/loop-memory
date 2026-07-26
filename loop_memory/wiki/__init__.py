"""Per-page knowledge classification (security scope routing) and
default-scope derivation helpers.

These are intentionally lightweight (no third-party deps, no model
calls by default) so they can run on every wiki upsert without
blocking the request thread.
"""
from __future__ import annotations

from .classifier import Classification, classify_page, pattern_classify
from .scope import (
    CLIENT_SCOPE_TOKENS,
    DEFAULT_CLIENT_SCOPE,
    VALID_SCOPE_TOKENS,
    auto_scope_config,
    build_scope_audit,
    derive_default_scope,
    derive_scope_from_sources,
    normalise_scope,
    parse_scope,
    source_token,
)

__all__ = [
    "Classification",
    "classify_page",
    "pattern_classify",
    "CLIENT_SCOPE_TOKENS",
    "DEFAULT_CLIENT_SCOPE",
    "VALID_SCOPE_TOKENS",
    "auto_scope_config",
    "build_scope_audit",
    "derive_default_scope",
    "derive_scope_from_sources",
    "normalise_scope",
    "parse_scope",
    "source_token",
]

from .backfill import reclassify_legacy_pages
__all__.append("reclassify_legacy_pages")

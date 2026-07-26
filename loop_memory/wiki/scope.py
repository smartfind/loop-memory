"""Scope parsing and safe per-client defaults for wiki pages."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


VALID_SCOPE_TOKENS = frozenset({
    "global",
    "all",
    "codex",
    "claude",
    "hermes",
    "openclaw",
})
CLIENT_SCOPE_TOKENS = frozenset({"codex", "claude", "hermes", "openclaw"})
DEFAULT_CLIENT_SCOPE = "codex"

_SOURCE_ALIASES = {
    "claude-code": "claude",
    "claude_code": "claude",
    "chatgpt": "codex",
    "open-claw": "openclaw",
    "open_claw": "openclaw",
    "hermes-cli": "hermes",
    "hermes_agent": "hermes",
}


def auto_scope_config(store: Any) -> dict[str, object]:
    """Read the auto-scope settings with compatibility fallbacks."""
    raw = store.get_setting("wiki_auto_scope", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get(
        "enabled",
        store.get_setting("wiki_auto_scope_enabled", True),
    )
    mode = raw.get(
        "mode",
        store.get_setting("wiki_auto_scope_mode", "pattern"),
    )
    mode = str(mode or "pattern").strip().lower()
    if mode not in {"pattern", "llm", "off"}:
        mode = "pattern"
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    return {"enabled": bool(enabled), "mode": mode}


def parse_scope(value: Any) -> list[str]:
    """Parse a scope string/list into deduplicated lowercase tokens.

    Validation is intentionally separate: callers can use this helper to
    normalise a trusted value, while API routes can reject unknown tokens
    before persisting them.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw_values: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = [value]
    tokens: list[str] = []
    for raw in raw_values:
        token = str(raw or "").strip().lower()
        if not token:
            continue
        if token == "all":
            token = "global"
        if token not in tokens:
            tokens.append(token)
    return tokens


def normalise_scope(value: Any, *, allow_auto: bool = False) -> str:
    """Return a canonical scope or raise ``ValueError`` for bad input."""
    if allow_auto and isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    tokens = parse_scope(value)
    if not tokens or any(token not in VALID_SCOPE_TOKENS for token in tokens):
        raise ValueError(f"invalid scope: {value!r}")
    if "global" in tokens:
        return "global"
    return ",".join(tokens)


def source_token(value: Any) -> str | None:
    """Map a loader/client source name to a supported scope token."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    raw = raw.replace("\\", "/")
    for separator in ("/", ":"):
        if separator in raw:
            raw = raw.split(separator, 1)[0]
            break
    raw = _SOURCE_ALIASES.get(raw, raw)
    if raw in CLIENT_SCOPE_TOKENS:
        return raw
    for token in CLIENT_SCOPE_TOKENS:
        if raw.startswith(token + "-") or raw.startswith(token + "_"):
            return token
    return None


def _evidence_sources(evidence_ids: Iterable[Any] | None, store: Any) -> list[str]:
    if not evidence_ids or store is None:
        return []
    ids = [str(item) for item in evidence_ids if str(item).strip()]
    if not ids:
        return []
    try:
        rows = store.list_memories(ids=ids, limit=max(1, len(ids)))
    except Exception:
        return []
    sources: list[str] = []
    for memory in rows or []:
        raw = getattr(memory, "source", None)
        token = source_token(raw)
        if token and token not in sources:
            sources.append(token)
    return sources


def derive_default_scope(
    source_hint: Any = None,
    evidence_ids: Iterable[Any] | None = None,
    store: Any = None,
    *,
    fallback: str = DEFAULT_CLIENT_SCOPE,
) -> str:
    """Derive a non-global scope for knowledge not promoted globally.

    An explicit client hint wins.  Otherwise all recognised clients that
    contributed evidence are retained, so a page distilled from Codex and
    Claude is available to both without silently becoming global.  The
    fallback is deliberately a real client token rather than ``global``;
    this is the privacy-preserving behavior for manually-created pages with
    no evidence metadata.
    """
    hinted = source_token(source_hint)
    if hinted:
        return hinted
    evidence_sources = _evidence_sources(evidence_ids, store)
    return derive_scope_from_sources(
        source_hint=source_hint,
        evidence_sources=evidence_sources,
        fallback=fallback,
    )


def derive_scope_from_sources(
    source_hint: Any = None,
    evidence_sources: Iterable[Any] | None = None,
    *,
    fallback: str = DEFAULT_CLIENT_SCOPE,
) -> str:
    """Derive a scope when source strings are already loaded by a caller."""
    if evidence_sources:
        tokens = [source_token(value) for value in evidence_sources]
        tokens = [token for token in tokens if token]
    else:
        tokens = []
    hinted = source_token(source_hint)
    if hinted:
        return hinted
    if tokens:
        counts = Counter(tokens)
        ordered = sorted(
            tokens,
            key=lambda token: (-counts[token], tokens.index(token)),
        )
        return ",".join(dict.fromkeys(ordered))
    return source_token(fallback) or DEFAULT_CLIENT_SCOPE


def build_scope_audit(
    classification: Any,
    *,
    scope: str,
    decision: str,
    source_hint: Any = None,
    enabled: bool = True,
    mode: str = "pattern",
    existing: dict | None = None,
) -> dict:
    """Attach the applied scope and decision to a classifier result."""
    if hasattr(classification, "to_dict"):
        audit = dict(classification.to_dict())
    elif isinstance(classification, dict):
        audit = dict(classification)
    else:
        audit = {}
    audit.update({
        "scope_applied": scope,
        "scope_decision": decision,
        "source_hint": str(source_hint).strip() if source_hint else None,
        "auto_scope_enabled": bool(enabled),
        "mode": str(mode or "pattern"),
    })
    if existing:
        previous = existing.get("auto_classification")
        if isinstance(previous, dict):
            prior_history = previous.get("history")
            if isinstance(prior_history, list):
                audit["history"] = [*prior_history[-19:], {k: v for k, v in previous.items() if k != "history"}]
            else:
                audit["history"] = [{k: v for k, v in previous.items() if k != "history"}]
    audit.setdefault("history", [])
    return audit


__all__ = [
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

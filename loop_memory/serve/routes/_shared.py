"""Helpers shared across route modules.

The original ``serve/app.py`` (3226 lines) had every route defined as a
closure inside ``create_app``. To keep the central file small (audit O1)
we lifted each route group into its own ``routes/<bucket>.py`` module
and emit ``register(app, store, scheduler=None)``. Closures over
``_memory_to_dict`` and ``_export_safe_segment`` still work because
both are imported here under the same names.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from ..handlers import (
    memory_to_dict as _memory_to_dict,  # noqa: F401
    session_to_dict as _session_to_dict,  # noqa: F401
)




_THINK_RE = re.compile(r"<(think|reasoning)>(.*?)</\1>", re.DOTALL)


def _split_think(text: str) -> tuple[str, str]:
    """Pull LLM thinking/reasoning blocks out of a model reply.

    Several providers (MiniMax reasoning, Anthropic extended thinking)
    emit a ``<think>...</think>`` or ``<reasoning>...</reasoning>``
    block before the user-visible answer. We surface the block under a
    separate field so the UI can render the report as Markdown while
    still letting power users peek at the chain-of-thought in a
    collapsed ``<details>``.
    """
    if not text:
        return "", ""
    thinking_parts = _THINK_RE.findall(text)
    cleaned = _THINK_RE.sub("", text).strip()
    thinking = "\n\n".join(p[1].strip() for p in thinking_parts if p[1].strip())
    return cleaned, thinking


def _export_safe_segment(text: str, *, fallback: str = "", kind: str = "line") -> str:
    """Sanitise a user-controlled wiki field for the markdown export.

    Audit M2: a malicious title ``"My\\n## Pwned"`` would otherwise
    create a second ``## Pwned`` heading on re-import. ``title`` /
    ``summary`` fields are collapsed to a single line and stripped of
    any leading ``## `` prefix; body content (legitimate markdown) is
    left alone.
    """
    if not text:
        return fallback
    s = text
    if kind in ("title", "summary"):
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(r"^#+\s*", "", s).strip()
    return s or fallback


# ---------------------------------------------------------------------------
# Path safety (audit 2026-09-23)
# ---------------------------------------------------------------------------
# Five export / snapshot endpoints accept a user-supplied absolute
# path. Before this helper shipped, every endpoint passed the path
# straight through to ``Path(...).expanduser().resolve()`` with no
# validation, which meant a caller could:
#
#   * write into ``/`` or ``~/.ssh`` via ``out_dir``
#   * overwrite the live SQLite store by setting
#     ``out_path`` to ``~/.loop_memory/loop_memory.db`` via
#     ``POST /api/snapshot``
#   * read any path with a valid snapshot header via
#     ``POST /api/snapshot/restore``
#
# When the bearer token is unset (the local-only default) the auth
# middleware does not enforce auth at all, so any process on the same
# host — including a malicious MCP client — could exploit the gap.
#
# ``_safe_resolve_path`` closes the gap with three rules:
#
#   1. Reject any path that resolves to the live DB (its canonical
#      location, the WAL sidecar, or the SHM sidecar).
#   2. Reject any path inside a system / sensitive directory
#      (``/System``, ``/usr``, ``/etc``, ``/var``, ``/private``,
#      ``~/.ssh``, ``~/.aws``, ``~/.gnupg``). Each rule covers a real
#      exfiltration / corruption vector observed in the wild for local
#      AI tooling.
#   3. Require the parent directory to exist for writes (so a typo
#      doesn't silently materialise a path that later resolves into
#      a sensitive root via symlink).
#
# ``kind="read"`` skips rule 3 (a read can describe a non-existent
# file) but still applies rules 1 and 2.

_FORBIDDEN_PREFIXES = (
    "/etc", "/private/etc", "/var", "/private/var",
    "/usr", "/bin", "/sbin", "/System", "/Library",
    "/private/var/db", "/private/var/log",
    "/private/var/root", "/private/var/run",
    "/var/db", "/var/log", "/var/run",
    "/dev", "/proc", "/sys",
)
# macOS resolves ``/tmp`` to ``/private/var/folders/<user>/T/`` and
# shells routinely write there. We allow the per-user temp subtree
# even though it starts with the otherwise-forbidden ``/private/var``.
_ALLOWED_TEMP_SUBSTRINGS = (
    "/var/folders/",   # macOS per-user temp
    "/var/tmp/",       # BSD/Linux preserved temp
    "/tmp/",           # Linux /tmp + macOS /private/tmp
    "/private/tmp/",   # macOS /tmp
    "/private/var/folders/",  # macOS per-user temp (resolved)
)


def _safe_resolve_path(
    raw: str | Path,
    *,
    kind: str = "write",
    live_db_path: str | Path | None = None,
) -> Path:
    """Return an absolute, validated Path or raise ``ValueError``.

    ``kind`` is ``"write"`` (default) or ``"read"``. Writes also
    require the parent directory to exist; reads don't.

    ``live_db_path`` is the canonical SQLite path the running store
    owns. If a caller hands us that path (or the WAL/SHM sidecars),
    we refuse loudly instead of silently overwriting the live store.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError("path is required")
    try:
        resolved = Path(str(raw)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise ValueError(f"could not resolve path: {e}") from e
    s = str(resolved)

    # Rule 1 — refuse the live DB + its WAL/SHM sidecars.
    if live_db_path is not None:
        try:
            live_resolved = Path(str(live_db_path)).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            live_resolved = None
        if live_resolved is not None:
            live_str = str(live_resolved)
            for suffix in ("", "-wal", "-shm", "-journal"):
                if s == live_str + suffix:
                    raise ValueError(
                        "refusing to write to the live SQLite store; "
                        "use a different path (e.g. ~/.loop_memory/snapshots/)"
                    )

    # Rule 2 — refuse system / sensitive directories.
    # Allow common per-user temp directories before applying the
    # forbidden-prefix rule so ``/tmp`` (which resolves to
    # ``/private/var/folders/...`` on macOS) still works.
    is_temp = any(sub in s for sub in _ALLOWED_TEMP_SUBSTRINGS)
    if not is_temp:
        for prefix in _FORBIDDEN_PREFIXES:
            if s == prefix.rstrip("/") or s.startswith(prefix + "/"):
                raise ValueError(
                    f"refusing to {'write to' if kind == 'write' else 'read from'} "
                    f"system directory {prefix!r}"
                )
    home = Path.home()
    home_str = str(home)
    for sensitive in (".ssh", ".aws", ".gnupg", ".config/gh", ".npmrc", ".zsh_history", ".bash_history"):
        forbidden = home_str + "/" + sensitive
        if s == forbidden or s.startswith(forbidden + "/"):
            raise ValueError(
                f"refusing to {'write to' if kind == 'write' else 'read from'} "
                f"sensitive path {sensitive!r}"
            )

    # Rule 3 — writes require an existing parent (catches typos that
    # could later resolve into a sensitive root via symlink).
    if kind == "write":
        parent = resolved.parent
        if not parent.exists():
            raise ValueError(
                f"parent directory does not exist: {parent} "
                "(create it first; refusing to materialise arbitrary paths)"
            )

    return resolved


__all__ = [
    "_memory_to_dict",
    "_session_to_dict",
    "_export_safe_segment",
    "_safe_resolve_path",
]

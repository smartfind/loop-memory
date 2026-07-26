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
from typing import Any, Optional

from ..handlers import (
    memory_to_dict as _memory_to_dict,  # noqa: F401
    session_to_dict as _session_to_dict,  # noqa: F401
)

__all__ = ["_memory_to_dict", "_session_to_dict", "_export_safe_segment"]


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

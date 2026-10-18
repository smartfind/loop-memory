"""Lightweight decorators for one-line agent memory integration.

Audit 2026-10-18 (loop-memory 0.4.15): the decorator pattern is the
adoption-shortcut inspired by LangChain ``@tool`` / Mem0 v2's
``@remember`` -- a single line of boilerplate turns any plain Python
function into an "agent-callable" that reads from long-term memory
before it runs and writes a fresh memory after it returns.

Two decorators ship here:

* :func:`agent_memory` -- wraps a callable so every invocation:
  - **recalls** the top-k relevant memories for the function's first
    string argument (the "query") *before* running the body, and
    - either exposes the recall as a ``memory_context`` keyword
      argument (when the wrapped function's signature declares it),
      OR surfaces it on the return value as ``result["memories"]``;
  - **stores** the (query, return-value) pair as a single
    ``kind="interaction"`` memory row *after* the body finishes, so
    the next invocation can recall it.

* :func:`remember` -- a tiny sugar that just calls
  ``store.upsert_memory(kind="fact", text=text, ...)`` once at
  definition time. Useful for facts the agent wants to hard-code into
  memory at import time ("I am a code assistant", "My user prefers
  dark mode", etc.).

Both decorators are intentionally zero-dep and free of any LLM/agent
framework dependency -- they sit on top of the public
``MemoryStore`` API only.
"""
from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from .storage.sqlite_store import MemoryStore

P = ParamSpec("P")
R = TypeVar("R")


def _stringify(value: Any, *, max_len: int = 2000) -> str:
    """Coerce a Python return value to a memory-friendly string.

    * ``str``  -> as-is (truncated to ``max_len``).
    * ``dict`` / ``list`` -> JSON-serialised then truncated.
    * ``bytes``-> hex preview.
    * everything else -> ``repr(value)``.

    The truncation is the only place this helper swallows information;
    downstream recall still works because the original ``text`` is on
    the row, and the full structured payload is also stored verbatim
    in ``tags`` when the caller passes a dict/list.
    """
    if isinstance(value, str):
        return value if len(value) <= max_len else value[: max_len - 1] + "\u2026"
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
        return s if len(s) <= max_len else s[: max_len - 1] + "\u2026"
    if isinstance(value, bytes):
        return value[:64].hex() + (f" (len={len(value)})" if len(value) > 64 else "")
    return repr(value)


def _first_string_arg(
    args: tuple, kwargs: dict, signature: inspect.Signature
) -> str | None:
    """Return the first positional str argument from the call.

    Falls back to ``None`` when the call carries no string-y input
    (e.g. ``@agent_memory`` wrapping an internal ``tick()`` that
    takes only objects).
    """
    if args:
        first = args[0]
        if isinstance(first, str) and first.strip():
            return first
        return None
    for name, p in signature.parameters.items():
        if name in kwargs:
            v = kwargs[name]
            if isinstance(v, str) and v.strip():
                return v
            return None
    return None


def agent_memory(
    store: MemoryStore,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    kind: str = "interaction",
    limit: int = 5,
    recall_kwargs_name: str = "memory_context",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap ``fn`` so every call reads + writes persistent memory.

    Parameters
    ----------
    store:
        An open :class:`~loop_memory.storage.sqlite_store.MemoryStore`.
        Required -- the decorator has nowhere to put memory otherwise.
    agent_id, user_id:
        Optional identity columns. When ``None`` the wrapped call
        pushes a memory that is global to the store (the same scope
        that pre-0.4.15 callers used).
    kind:
        Memory row kind. Default ``"interaction"`` matches the
        dashboard's existing tag.
    limit:
        Number of top-k memories to recall *before* the body runs.
        Capped at 50 to keep the prompt tiny.
    recall_kwargs_name:
        Name of the optional kwarg the wrapped function can declare to
        receive the recall as a string. Defaults to ``"memory_context"``.
        If the function does NOT declare that kwarg the recall is
        silently dropped (the body still runs) but the row is still
        stored after the call returns.

    Example
    -------
    >>> store = MemoryStore("loop.db")
    >>> @agent_memory(store, agent_id="codex")
    ... def ask(question: str, memory_context: str = "") -> str:
    ...     # memory_context arrives pre-formatted from the recall
    ...     return f"{question} -> {memory_context[:40]}"
    >>> ask("What is the deploy process?")
    """
    if not isinstance(store, MemoryStore):
        raise TypeError(
            f"@agent_memory requires a MemoryStore, got {type(store).__name__}"
        )
    if limit <= 0 or limit > 50:
        raise ValueError(f"limit must be in [1, 50], got {limit}")

    def _decorator(fn: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(fn)
        _ = sig  # pre-bound for _first_string_arg
        recall_kw = recall_kwargs_name

        @functools.wraps(fn)
        def _wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            query = _first_string_arg(args, kwargs, sig)
            # --- pre-call: recall ---------------------------------
            recall_hits: list[dict] = []
            context_text = ""
            if query:
                try:
                    res = store.recall_hybrid(
                        query, limit=limit, as_of_ts=None,
                    )
                    recall_hits = list(res.get("memories", []))
                except Exception:
                    recall_hits = []
                if recall_hits:
                    lines = []
                    for h in recall_hits:
                        prev = (h.get("text") or h.get("preview") or "").strip()
                        if prev:
                            lines.append(f"- {prev[:240]}")
                    context_text = "\n".join(lines)

            # Inject memory_context only when the function actually
            # declares the kwarg -- otherwise the decorator stays
            # invisible to legacy callables that don't know about it.
            injected = False
            if recall_kw in sig.parameters and context_text:
                kwargs = {**kwargs, recall_kw: context_text}
                injected = True

            # --- body ----------------------------------------------
            result = fn(*args, **kwargs)

            # --- post-call: store ----------------------------------
            if query:
                try:
                    text = _stringify(result)
                    tags = ["agent_memory"]
                    if isinstance(result, (dict, list)):
                        tags.append("structured")
                    store.upsert_memory(
                        kind=kind,
                        text=text,
                        importance=0.6,
                        agent_id=agent_id,
                        user_id=user_id,
                        tags=tags,
                        source=f"agent_memory:{fn.__name__}",
                    )
                except Exception:
                    # Never let a store failure break the wrapped call.
                    pass

            # Attach the recall to a dict result so callers can
            # surface "I knew this because ..." in the UI. Plain
            # string/int/float returns stay untouched.
            if isinstance(result, dict) and recall_hits and not injected:
                result.setdefault("memories", recall_hits)
            return result

        # Friendly metadata on the wrapper so ``help()`` is useful.
        _wrapper.__wrapped_agent_memory__ = True  # type: ignore[attr-defined]
        return _wrapper

    return _decorator


def remember(
    store: MemoryStore,
    text: str,
    *,
    kind: str = "fact",
    importance: float = 0.7,
    tags: list[str] | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Insert ``text`` into the store as a single memory and return its id.

    Sugar for callers that want to seed memory at import time without
    having to instantiate ``StoredMemory`` / ``upsert_memory``:

    >>> store = MemoryStore("loop.db")
    >>> remember(store, "User prefers dark mode",
    ...          tags=["ui", "preference"], agent_id="codex")
    """
    if not isinstance(store, MemoryStore):
        raise TypeError(
            f"remember() requires a MemoryStore, got {type(store).__name__}"
        )
    item = store.upsert_memory(
        kind=kind, text=text, importance=importance,
        tags=tags or ["remember"], agent_id=agent_id, user_id=user_id,
        source="remember",
    )
    return item.id


__all__ = ["agent_memory", "remember"]

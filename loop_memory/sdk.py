"""Universal Agent Memory SDK.

This is the protocol-agnostic surface every Agent (Codex, Claude,
Hermes, OpenClaw, LangChain, AutoGPT, a custom internal bot, …) uses
to remember facts, recall context, give feedback, and forget.

The SDK deliberately has *zero* third-party dependencies so it
ships with ``loop-memory`` itself. Two backends are supported:

* **In-process** — wraps a :class:`MemoryStore` directly. Use this
  when your agent runs in the same Python process as loop-memory
  (long-running daemon, embedded tool, tests).
* **HTTP** — talks to a running ``loop-memory serve`` instance over
  ``http://127.0.0.1:7767``. Use this from any other language that
  can speak JSON over HTTP, or from another process. Zero deps:
  uses :mod:`urllib` from the stdlib.

The public surface is intentionally small and stable:

    >>> client = MemoryClient.memory(store)
    >>> client.remember("user prefers dark mode", kind="preference",
    ...                 tags=["ui"], external_id="pref-dark")
    >>> client.recall("dark mode", limit=5)
    >>> client.feedback(external_id="pref-dark", value="up")
    >>> client.forget(external_id="pref-dark")
    >>> client.close()

Every write supports ``external_id`` for idempotency: re-pushing the
same ``(agent_id, user_id, external_id)`` tuple updates the row in
place instead of creating a duplicate.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .sdk_extensions import (
    CognitiveActionView, CognitiveReportView, ExportView, GraphHit,
    ImportView, MemoryClientExt, MemoryClientError, MemoryNamespace,
    SubgraphView, http_delete_json, http_get_json, http_post_json,
)


# ---------------------------------------------------------------------------
# Public dataclasses — the Agent-facing vocabulary
# ---------------------------------------------------------------------------


@dataclass
class Memory:
    """Agent-facing view of one memory row.

    Fields mirror the storage layer but stay JSON-serialisable so
    callers can pass them straight to any LLM prompt.
    """

    id: str
    text: str
    kind: str
    importance: float
    score: float
    source: str | None
    session_id: str | None
    agent_id: str | None
    user_id: str | None
    external_id: str | None
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Memory:
        return cls(
            id=d.get("id", ""),
            text=d.get("text", ""),
            kind=d.get("kind", "fact"),
            importance=float(d.get("importance", 0.5) or 0.5),
            score=float(d.get("score", 0.5) or 0.5),
            source=d.get("source"),
            session_id=d.get("session_id"),
            agent_id=d.get("agent_id"),
            user_id=d.get("user_id"),
            external_id=d.get("external_id"),
            tags=list(d.get("tags") or []),
            created_at=float(d.get("created_at") or 0.0),
            updated_at=float(d.get("updated_at") or 0.0),
        )


@dataclass
class RecallHit:
    """One ranked item in a recall response.

    ``kind`` is one of "memory" | "wiki" | "entity" so a single
    Agent prompt can render any of them in a unified way. Memory
    hits carry ``agent_id`` / ``user_id`` / ``external_id`` so the
    caller can route feedback / forget calls back to the store
    without a second lookup.
    """

    kind: str
    id: str
    text: str
    score: float
    title: str | None = None
    source: str | None = None
    tags: list[str] = field(default_factory=list)
    snippet: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    external_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RecallHit:
        return cls(
            kind=d.get("kind", "memory"),
            id=d.get("id", ""),
            text=d.get("text", ""),
            score=float(d.get("score", 0.0) or 0.0),
            title=d.get("title"),
            source=d.get("source"),
            tags=list(d.get("tags") or []),
            snippet=d.get("preview") or d.get("snippet"),
            agent_id=d.get("agent_id"),
            user_id=d.get("user_id"),
            external_id=d.get("external_id"),
        )


@dataclass
class RecallResult:
    """The full payload returned by :meth:`MemoryClient.recall`."""

    query: str
    memories: list[RecallHit] = field(default_factory=list)
    wiki: list[RecallHit] = field(default_factory=list)
    entities: list[RecallHit] = field(default_factory=list)
    temporal_intent: str = "any"
    temporal_confidence: float = 0.0
    source: str | None = None

    def all(self) -> list[RecallHit]:
        """Return a single sorted stream across all three channels."""
        merged = self.memories + self.wiki + self.entities
        merged.sort(key=lambda h: -h.score)
        return merged


# ---------------------------------------------------------------------------
# SDK base class
# ---------------------------------------------------------------------------


class MemoryClient(MemoryClientExt, AbstractContextManager):
    """Protocol-agnostic Agent Memory client.

    Use the two factory constructors to pick a backend:

    * :meth:`memory`  — direct in-process access to a ``MemoryStore``
    * :meth:`http`    — talks to a running ``loop-memory serve`` over
      HTTP (zero third-party deps, stdlib only)
    """

    def __init__(self) -> None:
        self._owns_backend = False

    # ---- factories -------------------------------------------------------

    @classmethod
    def memory(cls, store, *, agent_id: str | None = None,
               user_id: str | None = None) -> MemoryClient:
        """Build an SDK bound to an in-process :class:`MemoryStore`."""
        c = _InProcessClient(store)
        if agent_id is not None:
            c._default_agent_id = agent_id
        if user_id is not None:
            c._default_user_id = user_id
        return c

    @classmethod
    def http(cls, base_url: str = "http://127.0.0.1:7767",
             *, agent_id: str | None = None,
             user_id: str | None = None,
             timeout: float = 10.0) -> MemoryClient:
        """Build an SDK that talks to a running ``loop-memory serve``."""
        c = _HttpClient(base_url=base_url, timeout=timeout)
        if agent_id is not None:
            c._default_agent_id = agent_id
        if user_id is not None:
            c._default_user_id = user_id
        return c

    # ---- shared state for defaults -------------------------------------

    _default_agent_id: str | None = None
    _default_user_id: str | None = None

    # ---- Agent-facing API -----------------------------------------------

    def remember(
        self,
        text: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        tags: list[str] | None = None,
        source: str | None = None,
        session_id: str | None = None,
        external_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        ttl: float | None = None,
        created_at: float | None = None,
    ) -> Memory:
        """Push one memory into long-term storage.

        ``external_id`` makes the write idempotent: re-pushing the
        same ``(agent_id, user_id, external_id)`` tuple updates the
        row in place. Use a stable per-agent id (e.g. the tool name
        + call args hash) so retries from your Agent don't duplicate.
        """
        raise NotImplementedError

    def remember_batch(self, items: Iterable[dict[str, Any]]) -> list[Memory]:
        """Push many memories in one call. Same shape as ``remember``."""
        raise NotImplementedError

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        source: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        include: str = "memories,wiki,entities",
    ) -> RecallResult:
        """Unified search across the user's memory store."""
        raise NotImplementedError

    def forget(
        self,
        *,
        external_id: str | None = None,
        memory_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        """Delete a memory. Returns the number of rows removed (0 or 1)."""
        raise NotImplementedError

    def feedback(
        self,
        *,
        memory_id: str | None = None,
        external_id: str | None = None,
        value: str = "up",
        reason: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Send 👍/👎 on a memory. ``value`` is 'up' / 'down' / 'ignore'.

        Returns True if the signal was recorded, False if no matching
        memory was found.
        """
        raise NotImplementedError

    def list(
        self,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List recent memories with simple filters."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources. Default is a no-op."""
        return None

    # ---- context manager -------------------------------------------------

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------------------
# In-process backend
# ---------------------------------------------------------------------------


class _InProcessClient(MemoryClient):
    def __init__(self, store) -> None:
        super().__init__()
        self._store = store

    def remember(
        self,
        text: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        tags: list[str] | None = None,
        source: str | None = None,
        session_id: str | None = None,
        external_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        ttl: float | None = None,
        created_at: float | None = None,
    ) -> Memory:
        if not text or not text.strip():
            raise ValueError("text is required")
        row = self._store.upsert_memory(
            kind=kind,
            text=text.strip(),
            importance=float(importance),
            source=source,
            session_id=session_id,
            tags=tags or [],
            agent_id=agent_id if agent_id is not None else self._default_agent_id,
            user_id=user_id if user_id is not None else self._default_user_id,
            external_id=external_id,
            ttl=ttl,
            created_at=created_at,
        )
        return _memory_from_stored(row)

    def remember_batch(self, items: Iterable[dict[str, Any]]) -> list[Memory]:
        return [self.remember(**item) for item in items]

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        source: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        include: str = "memories,wiki,entities",
    ) -> RecallResult:
        # ``recall_hybrid`` is the production code path; fall back to
        # the legacy LIKE-based ``recall`` if a stale DB doesn't have
        # it (older installs, tests).
        wanted = tuple(s.strip() for s in include.split(",") if s.strip())
        if hasattr(self._store, "recall_hybrid"):
            r = self._store.recall_hybrid(
                query, limit=limit, include=wanted,
                bump_signals=True, source=source, level=1,
            )
        else:
            r = self._store.recall(query, limit=limit, include=wanted)
        # Optional post-filter: even if the store returned hits, the
        # caller may want only their agent's namespace.
        def _own(h: dict[str, Any]) -> bool:
            ag = h.get("agent_id")
            ur = h.get("user_id")
            # Global memories (no agent_id, no user_id) are visible
            # to every caller. If either namespace is set, both must
            # match the caller's filter or be unset on the memory.
            if ag is None and ur is None:
                return True
            if agent_id is not None and ag not in (None, agent_id):
                return False
            if user_id is not None and ur not in (None, user_id):
                return False
            return True
        return RecallResult(
            query=query,
            memories=[RecallHit.from_dict(m) for m in r.get("memories", []) if _own(m)],
            wiki=[RecallHit.from_dict(w) for w in r.get("wiki", [])],
            entities=[RecallHit.from_dict(e) for e in r.get("entities", [])],
            temporal_intent=r.get("temporal_intent", "any"),
            temporal_confidence=float(r.get("temporal_confidence", 0.0) or 0.0),
            source=source,
        )

    def forget(
        self,
        *,
        external_id: str | None = None,
        memory_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        if not memory_id and not external_id:
            raise ValueError("forget() needs memory_id or external_id")
        mid = memory_id
        if not mid and external_id:
            target_agent = agent_id if agent_id is not None else self._default_agent_id
            target_user = user_id if user_id is not None else self._default_user_id
            row = self._store.find_memory_by_external_id(
                target_agent or "", external_id, user_id=target_user,
            )
            if row is None:
                return 0
            mid = row.id
        return self._store.delete_memory(mid)

    def feedback(
        self,
        *,
        memory_id: str | None = None,
        external_id: str | None = None,
        value: str = "up",
        reason: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        if not memory_id and not external_id:
            raise ValueError("feedback() needs memory_id or external_id")
        if not memory_id:
            target_agent = agent_id if agent_id is not None else self._default_agent_id
            target_user = user_id if user_id is not None else self._default_user_id
            row = self._store.find_memory_by_external_id(
                target_agent or "", external_id, user_id=target_user,
            )
            if row is None:
                return False
            memory_id = row.id
        v = (value or "up").strip().lower()
        if v not in ("up", "down", "ignore"):
            raise ValueError("value must be up|down|ignore")
        # record_signal is the store primitive; mirrors the HTTP
        # feedback endpoint's behaviour. "ignore" is a soft-delete:
        # record the negative signal then remove the row so future
        # recalls stop surfacing it.
        self._store.record_signal(memory_id, positive=(v == "up"))
        if v == "ignore":
            self._store.delete_memory(memory_id)
        return True

    def list(
        self,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        rows = self._store.list_memories(
            agent_id=agent_id if agent_id is not None else self._default_agent_id,
            user_id=user_id if user_id is not None else self._default_user_id,
            session_id=session_id,
            kind=kind,
            limit=limit,
        )
        return [_memory_from_stored(r) for r in rows]

    # ---- graph ----------------------------------------------------

    def remember_edge(self, src: str, dst: str, *,
                      kind: str = "relates_to", weight: float = 0.5,
                      evidence_id: str | None = None) -> GraphHit:
        from .jobs.graph import upsert_semantic_edge
        info = upsert_semantic_edge(
            self._store, src, dst, kind=kind, weight=weight,
            evidence_id=evidence_id,
        )
        return GraphHit(kind=info["kind"], src=info["src"],
                        dst=info["dst"], weight=info["weight"],
                        evidence_id=info.get("evidence_id"))

    def subgraph(self, query: str, *, max_nodes: int = 32,
                 max_edges: int = 64) -> SubgraphView:
        from .jobs.graph import subgraph_for
        sg = subgraph_for(self._store, query,
                          max_nodes=max_nodes, max_edges=max_edges)
        return SubgraphView.from_dict(sg.to_dict())

    def rebuild_graph(self) -> int:
        from .graph.build import KnowledgeGraph
        KnowledgeGraph(self._store).rebuild(clear=True)
        return self._store.rebuild_entity_mentions()

    def recall_adaptive(self, query: str, *, limit: int = 8, **kwargs) -> Any:
        if not hasattr(self._store, "recall_hybrid"):
            return self.recall(query, limit=limit, **kwargs)
        include = ("memories", "wiki", "entities")
        if "include" in kwargs:
            raw = kwargs.pop("include")
            include = tuple(s.strip() for s in raw.split(",") if s.strip()) or include
        r = self._store.recall_hybrid(
            query, limit=limit, include=include, bump_signals=True,
            level=1, adaptive=True,
        )
        return RecallResult(
            query=query,
            memories=[RecallHit.from_dict(m) for m in r.get("memories", [])],
            wiki=[RecallHit.from_dict(w) for w in r.get("wiki", [])],
            entities=[RecallHit.from_dict(e) for e in r.get("entities", [])],
            temporal_intent=r.get("temporal_intent", "any"),
            temporal_confidence=float(r.get("temporal_confidence", 0.0) or 0.0),
        )

    # ---- cognitive ------------------------------------------------

    def cognitive_sleep(self, *, apply: bool = False, **kwargs) -> CognitiveReportView:
        from .jobs.cognitive import cognitive_sleep
        rpt = cognitive_sleep(self._store, apply=apply, **kwargs)
        return CognitiveReportView.from_dict(rpt.to_dict())

    def audit(self, *, kind: str | None = None, action: str | None = None,
              limit: int = 200) -> list[CognitiveActionView]:
        rows = self._store.list_audit(kind=kind, action=action, limit=limit)
        return [CognitiveActionView.from_dict(r) for r in rows]

    def revert_audit(self, audit_id: str) -> bool:
        self._store.record_audit(
            kind="revert", action="reverted",
            target_kind="memory", target_id=audit_id,
            reason="user marked audit row as reverted",
        )
        return True

    # ---- export / import / fork -----------------------------------

    def export(self, out_dir: str, *, agent_id: str | None = None,
               user_id: str | None = None, scope: str = "global",
               min_importance: float = 0.0) -> ExportView:
        from .export import export_bundle
        a = agent_id if agent_id is not None else self._default_agent_id
        u = user_id if user_id is not None else self._default_user_id
        r = export_bundle(self._store, out_dir, agent_id=a, user_id=u,
                          scope=scope, min_importance=min_importance)
        return ExportView.from_dict(r.to_dict())

    def import_bundle(self, in_dir: str, *, agent_id: str | None = None,
                      user_id: str | None = None, dry_run: bool = False) -> ImportView:
        from .export import import_bundle
        a = agent_id if agent_id is not None else self._default_agent_id
        u = user_id if user_id is not None else self._default_user_id
        r = import_bundle(self._store, in_dir, agent_id=a, user_id=u,
                          dry_run=dry_run)
        return ImportView.from_dict(r.to_dict())

    def fork(self, branch_tag: str | None = None) -> dict[str, Any]:
        from .export import fork_snapshot
        return fork_snapshot(self._store, branch_tag=branch_tag)



def _memory_from_stored(row) -> Memory:
    return Memory(
        id=row.id,
        text=row.text,
        kind=row.kind,
        importance=float(row.importance or 0.0),
        score=float(row.score or 0.0),
        source=row.source,
        session_id=row.session_id,
        agent_id=getattr(row, "agent_id", None),
        user_id=getattr(row, "user_id", None),
        external_id=getattr(row, "external_id", None),
        tags=list(row.tags or []),
        created_at=float(row.created_at or 0.0),
        updated_at=float(getattr(row, "updated_at", 0.0) or 0.0),
    )


# ---------------------------------------------------------------------------
# HTTP backend (zero-dep, stdlib only)
# ---------------------------------------------------------------------------


class _HttpClient(MemoryClient):
    def __init__(self, base_url: str = "http://127.0.0.1:7767", timeout: float = 10.0):
        super().__init__()
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            raise MemoryClientError(
                f"{method} {path} → {e.code}: {raw[:400]}"
            ) from e
        except urllib.error.URLError as e:
            raise MemoryClientError(f"cannot reach {self._base}: {e}") from e
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise MemoryClientError(f"invalid JSON from {path}: {e}") from e

    def remember(
        self,
        text: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        tags: list[str] | None = None,
        source: str | None = None,
        session_id: str | None = None,
        external_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        ttl: float | None = None,
        created_at: float | None = None,
    ) -> Memory:
        if not text or not text.strip():
            raise ValueError("text is required")
        body = {
            "text": text.strip(),
            "kind": kind,
            "importance": float(importance),
            "tags": list(tags or []),
            "source": source,
            "session_id": session_id,
            "external_id": external_id,
            "agent_id": agent_id if agent_id is not None else self._default_agent_id,
            "user_id": user_id if user_id is not None else self._default_user_id,
        }
        if ttl is not None:
            body["ttl"] = float(ttl)
        if created_at is not None:
            body["created_at"] = float(created_at)
        resp = self._request("POST", "/api/v1/memories", body)
        return Memory.from_dict(resp)

    def remember_batch(self, items: Iterable[dict[str, Any]]) -> list[Memory]:
        resp = self._request("POST", "/api/v1/memories:batch", {"items": list(items)})
        return [Memory.from_dict(m) for m in resp.get("items", [])]

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        source: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        include: str = "memories,wiki,entities",
    ) -> RecallResult:
        params = {
            "q": query,
            "limit": int(limit),
            "include": include,
            "source": source or "",
        }
        if agent_id is not None:
            params["agent_id"] = agent_id
        if user_id is not None:
            params["user_id"] = user_id
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        resp = self._request("GET", f"/api/v1/recall?{qs}")
        return RecallResult(
            query=query,
            memories=[RecallHit.from_dict(m) for m in resp.get("memories", [])],
            wiki=[RecallHit.from_dict(w) for w in resp.get("wiki", [])],
            entities=[RecallHit.from_dict(e) for e in resp.get("entities", [])],
            temporal_intent=resp.get("temporal_intent", "any"),
            temporal_confidence=float(resp.get("temporal_confidence", 0.0) or 0.0),
            source=source,
        )

    def forget(
        self,
        *,
        external_id: str | None = None,
        memory_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        if memory_id:
            resp = self._request("DELETE", f"/api/v1/memories/{memory_id}")
            return int(resp.get("deleted", 0) or 0)
        if external_id:
            params = {"external_id": external_id}
            if agent_id is not None:
                params["agent_id"] = agent_id
            elif self._default_agent_id is not None:
                params["agent_id"] = self._default_agent_id
            if user_id is not None:
                params["user_id"] = user_id
            elif self._default_user_id is not None:
                params["user_id"] = self._default_user_id
            qs = urllib.parse.urlencode(params)
            resp = self._request("DELETE", f"/api/v1/memories?{qs}")
            return int(resp.get("deleted", 0) or 0)
        raise ValueError("forget() needs memory_id or external_id")

    def feedback(
        self,
        *,
        memory_id: str | None = None,
        external_id: str | None = None,
        value: str = "up",
        reason: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        if memory_id:
            body = {"value": value, "reason": reason}
            self._request("POST", f"/api/v1/memories/{memory_id}/feedback", body)
            return True
        if external_id:
            body = {
                "value": value,
                "reason": reason,
                "external_id": external_id,
                "agent_id": agent_id if agent_id is not None else self._default_agent_id,
                "user_id": user_id if user_id is not None else self._default_user_id,
            }
            resp = self._request("POST", "/api/v1/memories/feedback", body)
            return bool(resp.get("ok", False))
        raise ValueError("feedback() needs memory_id or external_id")

    def list(
        self,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        params: dict[str, Any] = {"limit": int(limit)}
        if agent_id is not None:
            params["agent_id"] = agent_id
        elif self._default_agent_id is not None:
            params["agent_id"] = self._default_agent_id
        if user_id is not None:
            params["user_id"] = user_id
        elif self._default_user_id is not None:
            params["user_id"] = self._default_user_id
        if session_id is not None:
            params["session_id"] = session_id
        if kind is not None:
            params["kind"] = kind
        qs = urllib.parse.urlencode(params)
        resp = self._request("GET", f"/api/v1/memories?{qs}")
        return [Memory.from_dict(m) for m in resp.get("memories", [])]

    # ---- graph ----------------------------------------------------

    def remember_edge(self, src: str, dst: str, *,
                      kind: str = "relates_to", weight: float = 0.5,
                      evidence_id: str | None = None) -> GraphHit:
        body = {"src": src, "dst": dst, "kind": kind, "weight": weight}
        if evidence_id is not None:
            body["evidence_id"] = evidence_id
        resp = self._request("POST", "/api/v1/graph/edges", body)
        return GraphHit.from_dict(resp)

    def subgraph(self, query: str, *, max_nodes: int = 32,
                 max_edges: int = 64) -> SubgraphView:
        qs = urllib.parse.urlencode({
            "q": query, "max_nodes": max_nodes, "max_edges": max_edges,
        })
        resp = self._request("GET", f"/api/v1/graph/subgraph?{qs}")
        return SubgraphView.from_dict(resp)

    def rebuild_graph(self) -> int:
        resp = self._request("POST", "/api/v1/graph/rebuild", {})
        return int(resp.get("entity_mentions", 0) or 0)

    def recall_adaptive(self, query: str, *, limit: int = 8, **kwargs) -> Any:
        params = {
            "q": query, "limit": int(limit),
            "include": kwargs.pop("include", "memories,wiki,entities"),
            "adaptive": 1,
        }
        for k, v in kwargs.items():
            if v is not None:
                params[k] = v
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        r = self._request("GET", f"/api/v1/recall?{qs}")
        return RecallResult(
            query=query,
            memories=[RecallHit.from_dict(m) for m in r.get("memories", [])],
            wiki=[RecallHit.from_dict(w) for w in r.get("wiki", [])],
            entities=[RecallHit.from_dict(e) for e in r.get("entities", [])],
            temporal_intent=r.get("temporal_intent", "any"),
            temporal_confidence=float(r.get("temporal_confidence", 0.0) or 0.0),
        )

    # ---- cognitive ------------------------------------------------

    def cognitive_sleep(self, *, apply: bool = False, **kwargs) -> CognitiveReportView:
        body = {"apply": bool(apply), **{k: v for k, v in kwargs.items() if v is not None}}
        resp = self._request("POST", "/api/v1/cognitive/sleep", body)
        return CognitiveReportView.from_dict(resp)

    def audit(self, *, kind: str | None = None, action: str | None = None,
              limit: int = 200) -> list[CognitiveActionView]:
        params: dict[str, Any] = {"limit": int(limit)}
        if kind is not None:
            params["kind"] = kind
        if action is not None:
            params["action"] = action
        qs = urllib.parse.urlencode(params)
        resp = self._request("GET", f"/api/v1/cognitive/audit?{qs}")
        return [CognitiveActionView.from_dict(r) for r in resp.get("rows", [])]

    def revert_audit(self, audit_id: str) -> bool:
        resp = self._request("POST", "/api/v1/cognitive/audit/revert",
                              {"id": audit_id})
        return bool(resp.get("ok", False))

    # ---- export / import / fork -----------------------------------

    def export(self, out_dir: str, *, agent_id: str | None = None,
               user_id: str | None = None, scope: str = "global",
               min_importance: float = 0.0) -> ExportView:
        body = {
            "out_dir": out_dir,
            "agent_id": agent_id,
            "user_id": user_id,
            "scope": scope,
            "min_importance": float(min_importance),
        }
        body = {k: v for k, v in body.items() if v is not None and v != ""}
        resp = self._request("POST", "/api/v1/export", body)
        return ExportView.from_dict(resp)

    def import_bundle(self, in_dir: str, *, agent_id: str | None = None,
                      user_id: str | None = None, dry_run: bool = False) -> ImportView:
        body = {
            "in_dir": in_dir,
            "agent_id": agent_id,
            "user_id": user_id,
            "dry_run": bool(dry_run),
        }
        body = {k: v for k, v in body.items() if v is not None and v != ""}
        resp = self._request("POST", "/api/v1/import", body)
        return ImportView.from_dict(resp)

    def fork(self, branch_tag: str | None = None) -> dict[str, Any]:
        body = {"branch_tag": branch_tag} if branch_tag else {}
        return self._request("POST", "/api/v1/fork", body)


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "Memory",
    "RecallHit",
    "RecallResult",
    "MemoryClient",
    "MemoryClientError",
]

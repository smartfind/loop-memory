"""Extra surface on top of ``loop_memory.sdk.MemoryClient``.

The base ``MemoryClient`` covers the 4-verb contract (remember /
recall / feedback / forget). The Universal Agent Memory
contract adds four more first-class operations an Agent needs:

* **namespace** — `for_user(...)` / `for_agent(...)` sugar that
  returns a proxy whose every call auto-stamps the triple, so
  multi-tenant code reads like English instead of plumbing.

* **graph** — push high-signal relations (Mem0's differentiator),
  query the subgraph for a free-text query, and rebuild the
  entity-mention index.

* **cognitive** — run a single ``cognitive_sleep`` sweep, dry-run
  by default, and read the audit trail.

* **export / import / fork** — write a white-box ``MEMORY.md``
  bundle, re-hydrate it, or snapshot a branch of the wiki.

These are mixed in via the ``MemoryClientExt`` class — the SDK
exposes a single ``MemoryClient`` symbol that inherits from both
``MemoryClient`` and ``MemoryClientExt`` so the call sites stay
unchanged.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable


# Re-export the symbols the caller will need.
__all__ = [
    "MemoryClientExt",
    "MemoryNamespace",
    "GraphHit",
    "SubgraphView",
    "CognitiveActionView",
    "CognitiveReportView",
    "ExportView",
    "ImportView",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GraphHit:
    kind: str
    src: str
    dst: str
    weight: float
    evidence_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GraphHit:
        return cls(
            kind=d.get("kind", "co_occurs_with"),
            src=d.get("src") or d.get("name") or "",
            dst=d.get("dst") or "",
            weight=float(d.get("weight", 0) or 0),
            evidence_id=d.get("evidence_id"),
        )


@dataclass
class SubgraphView:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[GraphHit] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubgraphView:
        edges = [GraphHit.from_dict(e) for e in d.get("edges", [])]
        return cls(
            nodes=list(d.get("nodes", [])),
            edges=edges,
            memory_ids=list(d.get("memory_ids", [])),
            node_count=int(d.get("node_count", len(d.get("nodes", [])))),
            edge_count=int(d.get("edge_count", len(edges))),
        )


@dataclass
class CognitiveActionView:
    kind: str
    target_kind: str
    target_id: str
    target_text: str
    reason: str
    score: float
    action: str
    payload: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CognitiveActionView:
        return cls(
            kind=d.get("kind", ""),
            target_kind=d.get("target_kind", ""),
            target_id=d.get("target_id") or "",
            target_text=d.get("target_text") or "",
            reason=d.get("reason") or "",
            score=float(d.get("score", 0) or 0),
            action=d.get("action", "suggest"),
            payload=dict(d.get("payload") or {}),
        )


@dataclass
class CognitiveReportView:
    actions: list[CognitiveActionView] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    applied: bool = False
    elapsed_ms: float = 0.0
    total: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CognitiveReportView:
        return cls(
            actions=[CognitiveActionView.from_dict(a) for a in d.get("actions", [])],
            counts=dict(d.get("counts", {})),
            applied=bool(d.get("applied", False)),
            elapsed_ms=float(d.get("elapsed_ms", 0) or 0),
            total=int(d.get("total", 0)),
        )


@dataclass
class ExportView:
    out_dir: str
    memory_md_path: str
    pages: list[str] = field(default_factory=list)
    memories: int = 0
    graph_entities: int = 0
    graph_relations: int = 0
    sessions: int = 0
    elapsed_ms: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExportView:
        return cls(
            out_dir=d.get("out_dir", ""),
            memory_md_path=d.get("memory_md_path", ""),
            pages=list(d.get("pages", [])),
            memories=int(d.get("memories", 0) or 0),
            graph_entities=int(d.get("graph_entities", 0) or 0),
            graph_relations=int(d.get("graph_relations", 0) or 0),
            sessions=int(d.get("sessions", 0) or 0),
            elapsed_ms=float(d.get("elapsed_ms", 0) or 0),
        )


@dataclass
class ImportView:
    pages_upserted: int = 0
    memories_upserted: int = 0
    entities_upserted: int = 0
    relations_upserted: int = 0
    sessions_upserted: int = 0
    elapsed_ms: float = 0.0
    bundle_path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImportView:
        return cls(**{k: d.get(k, getattr(cls(), k)) for k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Namespace proxy
# ---------------------------------------------------------------------------


class MemoryNamespace:
    """A thin proxy returned by ``MemoryClient.for_user(...)`` /
    ``for_agent(...)`` that auto-stamps the namespace triple on
    every call.

    Read ``client.remember(...)`` once and then ``ns.remember(...)``
    in every call site — no need to repeat ``user_id=`` and
    ``external_id=`` plumbing. The proxy is itself a context
    manager so ``with client.for_user("alice") as alice_ns:`` is
    a documented pattern even though it doesn't own any
    resources.
    """

    def __init__(self, parent, *, agent_id: str | None = None,
                 user_id: str | None = None) -> None:
        self._parent = parent
        self._agent_id = agent_id
        self._user_id = user_id

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def __enter__(self) -> MemoryNamespace:
        return self

    def __exit__(self, *exc) -> None:
        return None

    # ----- 4-verb contract -----------------------------------------

    def remember(self, text: str, **kwargs: Any) -> Any:
        kwargs.setdefault("agent_id", self._agent_id)
        kwargs.setdefault("user_id", self._user_id)
        return self._parent.remember(text, **kwargs)

    def recall(self, query: str, **kwargs: Any) -> Any:
        kwargs.setdefault("agent_id", self._agent_id)
        kwargs.setdefault("user_id", self._user_id)
        return self._parent.recall(query, **kwargs)

    def feedback(self, **kwargs: Any) -> bool:
        kwargs.setdefault("agent_id", self._agent_id)
        kwargs.setdefault("user_id", self._user_id)
        return self._parent.feedback(**kwargs)

    def forget(self, **kwargs: Any) -> int:
        kwargs.setdefault("agent_id", self._agent_id)
        kwargs.setdefault("user_id", self._user_id)
        return self._parent.forget(**kwargs)

    def list(self, **kwargs: Any) -> list:
        kwargs.setdefault("agent_id", self._agent_id)
        kwargs.setdefault("user_id", self._user_id)
        return self._parent.list(**kwargs)

    # ----- extra ops ----------------------------------------------

    def subgraph(self, query: str, **kwargs: Any) -> SubgraphView:
        return self._parent.subgraph(query, **kwargs)

    def remember_edge(self, src: str, dst: str, *, kind: str = "relates_to",
                      weight: float = 0.5) -> GraphHit:
        return self._parent.remember_edge(src, dst, kind=kind, weight=weight)

    def cognitive_sleep(self, **kwargs: Any) -> CognitiveReportView:
        return self._parent.cognitive_sleep(**kwargs)

    def audit(self, **kwargs: Any) -> list[CognitiveActionView]:
        return self._parent.audit(**kwargs)


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class MemoryClientExt:
    """Mixin that adds namespace / graph / cognitive / export to any
    MemoryClient. The base class is a stub — concrete behaviour
    lives in the in-process and HTTP backends.
    """

    # ---- namespace sugar -------------------------------------------

    def for_user(self, user_id: str, *, agent_id: str | None = None) -> MemoryNamespace:
        a = agent_id if agent_id is not None else getattr(self, "_default_agent_id", None)
        return MemoryNamespace(self, agent_id=a, user_id=user_id)

    def for_agent(self, agent_id: str, *, user_id: str | None = None) -> MemoryNamespace:
        u = user_id if user_id is not None else getattr(self, "_default_user_id", None)
        return MemoryNamespace(self, agent_id=agent_id, user_id=u)

    # ---- graph ----------------------------------------------------

    def remember_edge(self, src: str, dst: str, *,
                      kind: str = "relates_to", weight: float = 0.5,
                      evidence_id: str | None = None) -> GraphHit:
        raise NotImplementedError

    def subgraph(self, query: str, *, max_nodes: int = 32,
                 max_edges: int = 64) -> SubgraphView:
        raise NotImplementedError

    def rebuild_graph(self) -> int:
        """Re-extract entities from every memory and rebuild the
        ``entity_mentions`` table. Returns the number of mentions
        written."""
        raise NotImplementedError

    def recall_adaptive(self, query: str, *, limit: int = 8, **kwargs) -> Any:
        """Recall with the 3D AdaptiveScore + graph boost enabled.

        Falls back to plain ``recall`` on stores that don't support
        adaptive scoring yet, so the call site is forward-compatible.
        """
        raise NotImplementedError

    # ---- cognitive ------------------------------------------------

    def cognitive_sleep(self, *, apply: bool = False, **kwargs) -> CognitiveReportView:
        raise NotImplementedError

    def audit(self, *, kind: str | None = None, action: str | None = None,
              limit: int = 200) -> list[CognitiveActionView]:
        raise NotImplementedError

    def revert_audit(self, audit_id: str) -> bool:
        """Mark an audit row as ``reverted`` (does not actually
        re-insert the deleted memory — it just records the
        decision so a future re-import of a bundle can restore
        it)."""
        raise NotImplementedError

    # ---- export / import / fork -----------------------------------

    def export(self, out_dir: str, *, agent_id: str | None = None,
               user_id: str | None = None, scope: str = "global",
               min_importance: float = 0.0) -> ExportView:
        raise NotImplementedError

    def import_bundle(self, in_dir: str, *, agent_id: str | None = None,
                      user_id: str | None = None, dry_run: bool = False) -> ImportView:
        raise NotImplementedError

    def fork(self, branch_tag: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers shared by both backends
# ---------------------------------------------------------------------------


def http_post_json(url: str, body: dict, *, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        raise MemoryClientError(f"POST {url} → {e.code}: {raw[:300]}") from e
    except urllib.error.URLError as e:
        raise MemoryClientError(f"cannot reach {url}: {e}") from e


def http_get_json(url: str, *, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        raise MemoryClientError(f"GET {url} → {e.code}: {raw[:300]}") from e
    except urllib.error.URLError as e:
        raise MemoryClientError(f"cannot reach {url}: {e}") from e


def http_delete_json(url: str, *, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, method="DELETE",
                                headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        raise MemoryClientError(f"DELETE {url} → {e.code}: {raw[:300]}") from e
    except urllib.error.URLError as e:
        raise MemoryClientError(f"cannot reach {url}: {e}") from e


class MemoryClientError(RuntimeError):
    """Raised when the HTTP backend cannot serve a request."""

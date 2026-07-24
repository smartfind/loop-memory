"""Graph memory + 3D adaptive scoring for the Universal Agent Memory
contract.

This module has three responsibilities, all additive on top of the
existing ``KnowledgeGraph`` in ``graph/build.py``:

1. **Semantic graph edges** — ``upsert_semantic_edge(src, dst,
   kind, weight, evidence_id)`` lets any Agent push high-value
   relationships like ``(User)-[LIVES_IN]->(Hangzhou)`` that
   vector search can never reconstruct on its own. Mem0's
   differentiator; loop-memory now has it.

2. **Graph-aware recall** — ``graph_boost(store, query, memory_ids)``
   returns a per-memory ``[0, 1.5]`` multiplier based on the density
   of the subgraph the memory shares with the query's entities. The
   hybrid recall path multiplies its RRF score by this to push
   connected memories to the top.

3. **3D adaptive scoring** — ``adaptive_score(recall_count, last_recalled,
   importance, graph_degree)`` blends usage / importance / graph
   connectivity into a single [0, 1] number that callers can use to
   rank "what's worth surfacing right now". Article 7 calls this
   "the third dimension" Mem0 added on top of plain vector recall.

The module is zero-dep and only uses ``MemoryStore``; it can be
called from the SDK, the HTTP route, the CLI, or the MCP server
without any extra setup.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..storage.sqlite_store import MemoryStore


# ---------------------------------------------------------------------------
# Semantic graph edges
# ---------------------------------------------------------------------------


# Relation kinds we care about. Kept short so the SQL filter stays
# cheap. ``co_occurs_with`` is the legacy kind the original
# ``KnowledgeGraph`` writes; the new ones below are the "high-signal"
# predicates an Agent pushes explicitly.
HIGH_SIGNAL_KINDS: frozenset[str] = frozenset({
    "lives_in", "works_on", "uses", "prefers", "decided",
    "depends_on", "owns", "manages", "blocks", "replaces",
    "caused", "fixed_by", "documented_in", "person",
    "项目", "决策", "偏好", "工具", "技术栈",
})


def upsert_semantic_edge(
    store: MemoryStore,
    src: str,
    dst: str,
    *,
    kind: str = "relates_to",
    weight: float = 0.5,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Push a high-signal relation between two entities.

    ``src`` and ``dst`` are entity names (not ids). The store upserts
    the underlying ``entities`` rows and the ``relations`` row in
    one call so the graph stays consistent even if the entities
    have never been seen before.

    Returns a small dict describing what was written so the caller
    can log it via ``record_audit``.
    """
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst or src == dst:
        raise ValueError("upsert_semantic_edge needs two distinct non-empty names")
    # Ensure both entities exist
    store.upsert_entity(src, _kind_for(src), bump_weight=weight)
    store.upsert_entity(dst, _kind_for(dst), bump_weight=weight)
    store.upsert_relation(src, dst, kind=kind, weight=weight, evidence_id=evidence_id)
    return {
        "src": src,
        "dst": dst,
        "kind": kind,
        "weight": weight,
        "evidence_id": evidence_id,
    }


def _kind_for(name: str) -> str:
    """Best-effort guess at the entity ``kind`` for a name.

    The legacy ``extract_entities`` only emits a small set of kinds
    (``concept``, ``person``, ``tag``, ``project``, ``tool``,
    ``company``). When the Agent pushes a name it didn't extract
    from text we want it stored under the most useful existing
    kind so the UI can colour it correctly.
    """
    n = (name or "").lower()
    if any(tok in n for tok in ("项目", "project", "repo", "service", "系统", "模块")):
        return "project"
    if any(tok in n for tok in ("工具", "tool", "framework", "库", "lib")):
        return "tool"
    if any(tok in n for tok in ("公司", "company", "inc", "团队", "team")):
        return "company"
    if n[:1].isupper() and n[1:2].islower():
        return "concept"
    return "concept"


# ---------------------------------------------------------------------------
# Graph-aware recall boost
# ---------------------------------------------------------------------------


@dataclass
class GraphBoost:
    """Per-memory graph connectivity score in [0, 1.5]."""

    memory_id: str
    boost: float
    matched_entities: list[str]


def graph_boost(
    store: MemoryStore,
    query: str,
    memory_ids: list[str],
    *,
    max_per_memory: int = 8,
) -> dict[str, GraphBoost]:
    """Return a ``{memory_id: GraphBoost}`` map for the given candidates.

    The boost is the density of the subgraph the memory shares with
    the query's entities. A memory whose entities connect to the
    query's entities (either directly or via one hop) gets a boost;
    isolated memories get 0. The maximum is 1.5 so it can dominate
    the RRF score when the connection is strong.

    The implementation is deliberately simple:

    1. Extract entities from the query text (using the same
       ``extract_entities`` the graph builder uses).
    2. Find every relation (in either direction) where one endpoint
       is a query entity.
    3. Look up which memory ids back each matched relation via the
       ``entity_mentions`` table.
    4. Count matches per memory id; map count → boost with a
       saturating log curve.

    The lookup is bounded to ``max_per_memory`` hits per memory so
    a runaway relation doesn't lock the score to its cap.
    """
    if not memory_ids or not query.strip():
        return {}
    from ..graph.extract import extract_entities
    query_entities = [n for (n, _) in extract_entities(query) if n]
    if not query_entities:
        return {}
    # All related entities (1-hop away from query entities)
    related: dict[str, list[str]] = {n: [] for n in query_entities}
    for ent in query_entities:
        rels = store.search_entities_by_names([ent], limit=64)
        for r in rels:
            # ``search_entities_by_names`` returns canonical name in
            # ``name``; we want the *other* side of each relation.
            others = store.related_entities(r["name"], limit=64)
            for o in others:
                if o != ent:
                    related.setdefault(ent, []).append(o)
    if not any(related.values()):
        return {}
    # Map: memory_id -> set of matched query entities (de-duplicated)
    mem_to_matches: dict[str, set[str]] = {m: set() for m in memory_ids}
    for qent, others in related.items():
        if not others:
            continue
        # Which memory ids back `qent`?
        mems_for_qent = store.memory_ids_for_entity(qent, limit=512)
        # Which memory ids back each of the 1-hop neighbours?
        for nb in others:
            mems_for_nb = store.memory_ids_for_entity(nb, limit=512)
            for mid in set(mems_for_qent) & set(mems_for_nb) & set(memory_ids):
                mem_to_matches[mid].add(qent)
                if len(mem_to_matches[mid]) >= max_per_memory:
                    break
    out: dict[str, GraphBoost] = {}
    for mid, matches in mem_to_matches.items():
        if not matches:
            continue
        # Saturating log so 1 hit = 0.45, 2 = 0.7, 4+ → 1.0+, capped
        # at 1.5.
        n = len(matches)
        boost = min(1.5, 0.45 + 0.25 * math.log1p(n))
        out[mid] = GraphBoost(memory_id=mid, boost=round(boost, 3),
                              matched_entities=sorted(matches))
    return out


# ---------------------------------------------------------------------------
# 3D adaptive scoring
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveScore:
    """The three dimensions Mem0 fuses, plus the blended score.

    All numbers in [0, 1]. ``blended`` is a weighted average with
    weights that bias toward ``importance`` so the highest-quality
    memories still dominate even when their usage is low.
    """

    importance: float
    recency: float
    usage: float
    graph: float
    blended: float

    def to_dict(self) -> dict[str, float]:
        return {
            "importance": round(self.importance, 4),
            "recency": round(self.recency, 4),
            "usage": round(self.usage, 4),
            "graph": round(self.graph, 4),
            "blended": round(self.blended, 4),
        }


def adaptive_score(
    *,
    importance: float,
    created_at: float,
    now: float | None = None,
    recall_count: int = 0,
    last_recalled_at: float | None = None,
    graph_degree: int = 0,
    half_life_days: float = 30.0,
    weights: dict[str, float] | None = None,
) -> AdaptiveScore:
    """Compute the 3D adaptive score.

    The four components are:

    * ``importance`` — the LLM/original importance (already [0, 1]).
    * ``recency`` — half-life decay on the memory's age.
    * ``usage`` — log-saturated recall_count, further discounted by
      the age of the most recent recall (a memory that was hot two
      months ago is not as hot as one that was hot yesterday).
    * ``graph`` — log-saturated graph degree, capped at 1.0 (a
      memory connected to 8+ entities is a "hub" worth promoting).

    ``blended`` = w_i*importance + w_r*recency + w_u*usage + w_g*graph,
    with default weights ``{importance: 0.40, recency: 0.20, usage:
    0.25, graph: 0.15}``. Callers can override via ``weights=``.

    Pure function — no DB access — so the unit tests can hammer it
    with crafted inputs.
    """
    w = weights or {"importance": 0.40, "recency": 0.20, "usage": 0.25, "graph": 0.15}
    now = now if now is not None else time.time()
    age = max(0.0, now - created_at)
    half_life = half_life_days * 86400.0
    recency = (0.5 ** (age / half_life)) if half_life else 1.0
    recency = max(0.0, min(1.0, recency))

    if recall_count > 0:
        log_recall = math.log1p(recall_count) / math.log1p(100)
        log_recall = max(0.0, min(1.0, log_recall))
        if last_recalled_at:
            age_recall = max(0.0, now - last_recalled_at)
            usage_recency = (0.5 ** (age_recall / half_life)) if half_life else 1.0
        else:
            usage_recency = 1.0
        usage = log_recall * (0.25 + 0.75 * usage_recency)
    else:
        usage = 0.0
    usage = max(0.0, min(1.0, usage))

    graph = 0.0
    if graph_degree > 0:
        graph = math.log1p(graph_degree) / math.log1p(8)  # 8 hops = full credit
        graph = max(0.0, min(1.0, graph))

    imp = max(0.0, min(1.0, float(importance or 0)))
    blended = (
        w.get("importance", 0.40) * imp
        + w.get("recency", 0.20) * recency
        + w.get("usage", 0.25) * usage
        + w.get("graph", 0.15) * graph
    )
    blended = max(0.0, min(1.0, blended))
    return AdaptiveScore(
        importance=imp,
        recency=recency,
        usage=usage,
        graph=graph,
        blended=blended,
    )


# ---------------------------------------------------------------------------
# Convenience: small subgraph snapshot
# ---------------------------------------------------------------------------


@dataclass
class Subgraph:
    """A trimmed-down view of the graph for an Agent prompt."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    memory_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "memory_ids": self.memory_ids,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
        }


def subgraph_for(
    store: MemoryStore,
    query: str,
    *,
    max_hops: int = 1,
    max_nodes: int = 32,
    max_edges: int = 64,
) -> Subgraph:
    """Build a small subgraph relevant to ``query``.

    1. Extract entities from the query.
    2. Pull every relation (1 hop) that touches a query entity.
    3. Look up the backing memory ids for each entity.
    4. Trim to ``max_nodes`` / ``max_edges``.

    Returns a :class:`Subgraph` dataclass so callers can render it
    however they like (Mermaid, JSON for the SDK, etc.).
    """
    from ..graph.extract import extract_entities
    ents = [n for (n, _) in extract_entities(query) if n]
    if not ents:
        return Subgraph(nodes=[], edges=[], memory_ids=[])
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    memory_ids: set[str] = set()
    for ent in ents:
        info = store.entity_by_name(ent)
        if info:
            nodes[ent] = {
                "name": ent,
                "kind": info.get("kind") or "concept",
                "weight": float(info.get("weight") or 0),
                "mention_count": int(info.get("mention_count") or 0),
            }
        else:
            nodes[ent] = {"name": ent, "kind": "concept", "weight": 0.5, "mention_count": 0}
        for nb in store.related_entities(ent, limit=32):
            if nb not in nodes:
                nb_info = store.entity_by_name(nb) or {}
                nodes[nb] = {
                    "name": nb,
                    "kind": nb_info.get("kind") or "concept",
                    "weight": float(nb_info.get("weight") or 0),
                    "mention_count": int(nb_info.get("mention_count") or 0),
                }
            edge = {"src": ent, "dst": nb, "kind": "related"}
            if edge not in edges:
                edges.append(edge)
        for mid in store.memory_ids_for_entity(ent, limit=32):
            memory_ids.add(mid)
    # Trim
    n_nodes = list(nodes.values())[:max_nodes]
    n_edges = edges[:max_edges]
    return Subgraph(nodes=n_nodes, edges=n_edges, memory_ids=sorted(memory_ids))


__all__ = [
    "AdaptiveScore",
    "GraphBoost",
    "HIGH_SIGNAL_KINDS",
    "Subgraph",
    "adaptive_score",
    "graph_boost",
    "subgraph_for",
    "upsert_semantic_edge",
]

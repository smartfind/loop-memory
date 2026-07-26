"""Route group: graph.

Knowledge graph + v1/graph + v1/fork.

All routes were extracted from ``serve/app.py`` as part of the O1
refactor to keep the central ``create_app`` small. Each block lives
inside ``register(app, store, scheduler=None)`` so closures over the
three captured variables work unchanged from the original layout.
"""
from __future__ import annotations

from typing import Any, Optional

import re
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ...storage.sqlite_store import MemoryStore
from ._shared import _memory_to_dict, _export_safe_segment


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
    @app.post("/api/v1/graph/edges")
    def v1_graph_edges(body: dict):
        """Upsert a high-signal semantic relation."""
        from ...jobs.graph import upsert_semantic_edge
        src = (body.get("src") or "").strip()
        dst = (body.get("dst") or "").strip()
        if not src or not dst or src == dst:
            raise HTTPException(400, "src and dst must be distinct non-empty names")
        try:
            weight = float(body.get("weight", 0.5))
        except (TypeError, ValueError):
            raise HTTPException(400, f"weight must be a number, got {body.get('weight')!r}")
        info = upsert_semantic_edge(
            store, src, dst,
            kind=(body.get("kind") or "relates_to").strip() or "relates_to",
            weight=max(0.0, min(1.5, weight)),
            evidence_id=body.get("evidence_id"),
        )
        return info


    @app.get("/api/v1/graph/subgraph")
    def v1_graph_subgraph(q: str, max_nodes: int = 32, max_edges: int = 64):
        from ...jobs.graph import subgraph_for
        sg = subgraph_for(store, q, max_nodes=max_nodes, max_edges=max_edges)
        return sg.to_dict()


    @app.post("/api/v1/graph/rebuild")
    def v1_graph_rebuild():
        from ...graph.build import KnowledgeGraph
        KnowledgeGraph(store).rebuild(clear=True)
        n = store.rebuild_entity_mentions()
        return {"entity_mentions": n}


    @app.post("/api/v1/fork")
    def v1_fork(body: dict):
        from ...export import fork_snapshot
        return fork_snapshot(store, branch_tag=(body.get("branch_tag") or None))


    @app.get("/api/graph")
    def graph(limit_entities: int = 200, limit_relations: int = 1000):
        ents = store.list_entities(limit=limit_entities)
        rels = store.list_relations(limit=limit_relations)
        return {
            "entities": [
                {
                    "id": e.id, "name": e.name, "kind": e.kind,
                    "mention_count": e.mention_count, "weight": round(e.weight, 3),
                }
                for e in ents
            ],
            "node_kinds": sorted({e.kind for e in ents}),
            "relations": [
                {
                    "id": r.id, "src": r.src, "dst": r.dst,
                    "kind": r.kind, "weight": round(r.weight, 3),
                    "evidence": r.evidence_ids[:5],
                }
                for r in rels
            ],
            "stats": store.graph_stats(),
        }


    @app.get("/api/graph/entity/{name}/memories")
    def graph_entity_memories(name: str, limit: int = 30):
        # match by LIKE on the entity name appearing in memory text
        rows = store.list_memories(query=name, limit=limit)
        return [_memory_to_dict(m) for m in rows]



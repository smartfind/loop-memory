"""Knowledge graph commands."""

from __future__ import annotations

import json

from .._common import DEFAULT_DB


def run_graph(args) -> int:
    """loop-memory graph [--rebuild] [--limit N] [--clear]"""
    from ...graph.build import KnowledgeGraph
    from ...storage.sqlite_store import MemoryStore
    if "--rebuild" in args or "--clear" in args:
        store = MemoryStore(DEFAULT_DB)
        clear = "--clear" in args
        report = KnowledgeGraph(store).rebuild(clear=clear)
        print(json.dumps(report.__dict__, indent=2))
        return 0
    print(json.dumps(MemoryStore(DEFAULT_DB).graph_stats(), indent=2))
    return 0

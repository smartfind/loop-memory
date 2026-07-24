"""CLI commands for the Universal Agent Memory v7 surface.

* ``loop-memory cognitive-sleep`` — run a sweep, optionally apply.
* ``loop-memory audit``          — read the audit trail.
* ``loop-memory export``         — write a MEMORY.md bundle.
* ``loop-memory import``         — re-hydrate a bundle.
* ``loop-memory fork``           — snapshot the wiki.
* ``loop-memory graph-edge``     — push a semantic relation.
* ``loop-memory subgraph``       — print a small subgraph.

All commands accept ``--db PATH`` to point at a non-default store
(handy in tests). They print JSON so a shell pipeline can consume
the output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ...storage.sqlite_store import MemoryStore
from .._common import DEFAULT_DB, default_db_path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop-memory (v7)", add_help=True)
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"Path to the SQLite store (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    cs = sub.add_parser("cognitive-sleep", help="Run a cognitive sweep")
    cs.add_argument("--apply", action="store_true",
                    help="Actually delete the suggested memories")
    cs.add_argument("--stale-days", type=int, default=90)
    cs.add_argument("--min-score", type=float, default=0.2)
    cs.add_argument("--min-importance", type=float, default=0.3)
    cs.add_argument("--low-value", type=float, default=0.3)
    cs.add_argument("--merge-threshold", type=float, default=0.92)
    cs.add_argument("--limit", type=int, default=1000)

    au = sub.add_parser("audit", help="Read the cognitive audit trail")
    au.add_argument("--kind", default=None)
    au.add_argument("--action", default=None)
    au.add_argument("--limit", type=int, default=200)

    ex = sub.add_parser("export", help="Write a MEMORY.md bundle")
    ex.add_argument("out_dir")
    ex.add_argument("--agent-id", default=None)
    ex.add_argument("--user-id", default=None)
    ex.add_argument("--scope", default="global")
    ex.add_argument("--min-importance", type=float, default=0.0)

    im = sub.add_parser("import", help="Re-hydrate a MEMORY.md bundle")
    im.add_argument("in_dir")
    im.add_argument("--agent-id", default=None)
    im.add_argument("--user-id", default=None)
    im.add_argument("--dry-run", action="store_true")

    fk = sub.add_parser("fork", help="Snapshot every wiki page")
    fk.add_argument("--branch-tag", default=None)

    ge = sub.add_parser("graph-edge", help="Push a semantic relation")
    ge.add_argument("src")
    ge.add_argument("dst")
    ge.add_argument("--kind", default="relates_to")
    ge.add_argument("--weight", type=float, default=0.5)
    ge.add_argument("--evidence-id", default=None)

    sg = sub.add_parser("subgraph", help="Print a small subgraph")
    sg.add_argument("query")
    sg.add_argument("--max-nodes", type=int, default=32)
    sg.add_argument("--max-edges", type=int, default=64)

    sub.add_parser("graph-rebuild", help="Rebuild the entity graph")

    return p


def _emit(d) -> int:
    print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    return 0


def run_cognitive_sleep(args: list) -> int:
    p = _build_parser()
    ns = p.parse_args(["--db", os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB),
                        "cognitive-sleep", *args])
    s = MemoryStore(ns.db)
    from ...jobs.cognitive import cognitive_sleep
    rpt = cognitive_sleep(
        s, apply=ns.apply,
        stale_days=ns.stale_days, min_score=ns.min_score,
        min_importance=ns.min_importance, low_value=ns.low_value,
        merge_threshold=ns.merge_threshold, limit=ns.limit,
    )
    return _emit(rpt.to_dict())


def run_audit(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "audit", *args])
    s = MemoryStore(ns.db)
    return _emit({"rows": s.list_audit(kind=ns.kind, action=ns.action, limit=ns.limit)})


def run_export(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "export", *args])
    s = MemoryStore(ns.db)
    from ...export import export_bundle
    r = export_bundle(s, ns.out_dir, agent_id=ns.agent_id, user_id=ns.user_id,
                      scope=ns.scope, min_importance=ns.min_importance)
    return _emit(r.to_dict())


def run_import(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "import", *args])
    s = MemoryStore(ns.db)
    from ...export import import_bundle
    r = import_bundle(s, ns.in_dir, agent_id=ns.agent_id, user_id=ns.user_id,
                      dry_run=ns.dry_run)
    return _emit(r.to_dict())


def run_fork(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "fork", *args])
    s = MemoryStore(ns.db)
    from ...export import fork_snapshot
    return _emit(fork_snapshot(s, branch_tag=ns.branch_tag))


def run_graph_edge(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "graph-edge", *args])
    s = MemoryStore(ns.db)
    from ...jobs.graph import upsert_semantic_edge
    info = upsert_semantic_edge(
        s, ns.src, ns.dst, kind=ns.kind, weight=ns.weight,
        evidence_id=ns.evidence_id,
    )
    return _emit(info)


def run_subgraph(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "subgraph", *args])
    s = MemoryStore(ns.db)
    from ...jobs.graph import subgraph_for
    sg = subgraph_for(s, ns.query, max_nodes=ns.max_nodes, max_edges=ns.max_edges)
    return _emit(sg.to_dict())


def run_graph_rebuild(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "graph-rebuild", *args])
    s = MemoryStore(ns.db)
    from ...graph.build import KnowledgeGraph
    KnowledgeGraph(s).rebuild(clear=True)
    n = s.rebuild_entity_mentions()
    return _emit({"entity_mentions": n})

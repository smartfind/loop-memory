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

    rc = sub.add_parser(
        "wiki-reclassify-legacy",
        help=("Re-classify legacy wiki pages whose auto_classification is NULL; "
              "downgrade non-universal-security pages to per-source scope."))
    rc.add_argument("--batch", type=int, default=500,
                    help="Maximum rows to scan (default 500)")
    rc.add_argument("--dry-run", action="store_true",
                    help="Report what would change without persisting")

    # Audit 2026-09-06: per-memory lifetime stats
    # (agentmemory v1.3.0 pattern, slimmed down).
    ms = sub.add_parser(
        "memory-stats",
        help="Per-memory lifetime stats (audit 2026-09-06)")
    ms.add_argument("memory_id",
                    help="Memory id (or short prefix) to look up")
    ms.add_argument("--prefix", action="store_true",
                    help="Allow a short prefix match (first 8+ chars)")

    # Audit 2026-09-06: portable SQLite snapshot
    # (codexa-memory v0.2.0 pattern).
    sn = sub.add_parser(
        "snapshot",
        help="Write a portable SQLite snapshot of the live store")
    sn.add_argument("out_path",
                    help="Destination .memory.sqlite file")
    rs = sub.add_parser(
        "restore",
        help="Re-hydrate a portable SQLite snapshot into the live store")
    rs.add_argument("in_path",
                    help="Source .memory.sqlite file")

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


def run_audit_supersede(args: list) -> int:
    """``loop-memory audit-supersede [--target ID] [--limit N] [--by ID]``

    Walks the supersession chain (audit 2026-08-30, Mem0 v2.0.19
    Dream pattern). Without ``--target`` it lists every superseded
    memory (the loser side of every merge), most-recent first. With
    ``--target <id>`` it returns just the chain starting at that
    memory (oldest to newest). With ``--by <id>`` it filters the
    list to memories that were superseded by a specific winner.

    The CLI prints JSON so a shell pipeline can consume the output.
    """
    import argparse as _ap
    p = _ap.ArgumentParser(prog="loop-memory audit-supersede")
    p.add_argument("--target", default=None,
                   help="Walk the supersession chain starting at this memory id")
    p.add_argument("--by", default=None,
                   help="List memories superseded by this specific winner id")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--db", default=os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB))
    ns = p.parse_args(args)
    s = MemoryStore(ns.db)
    if ns.target:
        chain = s.trace_supersession(ns.target)
        return _emit({
            "target": ns.target,
            "chain": chain,
            "chain_length": len(chain),
            "winner": chain[-1] if chain else None,
        })
    rows = s.list_superseded(superseded_by=ns.by, limit=ns.limit)
    return _emit({
        "rows": rows,
        "total": s.supersession_count(),
        "filtered_by": ns.by,
        "limit": ns.limit,
    })


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


def run_wiki_reclassify_legacy(args: list) -> int:
    p = _build_parser()
    db = os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB)
    ns = p.parse_args(["--db", db, "wiki-reclassify-legacy", *args])
    s = MemoryStore(ns.db)
    from ...wiki import reclassify_legacy_pages
    if ns.dry_run:
        pages = s.list_wiki_pages(limit=ns.batch)
        legacy = [
            p for p in pages
            if (p.get("scope") or "global") == "global"
            and p.get("auto_classification") is None
        ]
        return _emit({
            "dry_run": True,
            "scanned": len(pages),
            "legacy_global": len(legacy),
            "items": [{"slug": p["slug"], "title": p.get("title")}
                      for p in legacy],
        })
    summary = reclassify_legacy_pages(s, batch=ns.batch)
    return _emit(summary)


def run_memory_stats(args: list) -> int:
    """``loop-memory memory-stats <id> [--prefix]``

    Per-memory lifetime stats (audit 2026-09-06, agentmemory v1.3.0).
    Returns a flat dict suitable for ``jq`` and the dashboard.

    With ``--prefix`` the caller can pass the first 8+ chars of the id
    (handy in a shell pipeline where the full UUID is awkward).
    """
    import argparse as _ap
    p = _ap.ArgumentParser(prog="loop-memory memory-stats")
    p.add_argument("memory_id")
    p.add_argument("--prefix", action="store_true")
    p.add_argument("--db", default=os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB))
    ns = p.parse_args(args)
    s = MemoryStore(ns.db)
    mid = ns.memory_id.strip()
    if not mid:
        return _emit({"error": "memory_id is required"})
    if ns.prefix:
        # Resolve a prefix to a full id. The store indexes by full
        # id so we walk a list_memories scan (cheap because prefix
        # queries are rare / human-driven).
        candidates = [m.id for m in s.list_memories(limit=10000)
                      if m.id.startswith(mid)]
        if not candidates:
            return _emit({"error": f"no memory matches prefix {mid!r}"})
        if len(candidates) > 1:
            return _emit({
                "error": f"prefix {mid!r} matches {len(candidates)} memories",
                "candidates": candidates[:8],
            })
        mid = candidates[0]
    res = s.memory_stats(mid)
    if res is None:
        return _emit({"error": f"memory {mid!r} not found"})
    return _emit(res)


def run_snapshot(args: list) -> int:
    """``loop-memory snapshot <out_path>``

    Write a portable SQLite snapshot (audit 2026-09-06,
    codexa-memory v0.2.0). Returns a summary dict with size + table
    counts so the caller can confirm the snapshot is sane.
    """
    import argparse as _ap
    p = _ap.ArgumentParser(prog="loop-memory snapshot")
    p.add_argument("out_path")
    p.add_argument("--db", default=os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB))
    ns = p.parse_args(args)
    s = MemoryStore(ns.db)
    from ...storage.snapshot import snapshot as _snapshot
    return _emit(_snapshot(s, ns.out_path))


def run_restore(args: list) -> int:
    """``loop-memory restore <in_path>``

    Re-hydrate a portable SQLite snapshot (audit 2026-09-06,
    codexa-memory v0.2.0). Returns a summary dict with table counts
    so the caller can confirm the restore is sane. Refuses
    wrong-magic files loudly instead of corrupting the live store.
    """
    import argparse as _ap
    p = _ap.ArgumentParser(prog="loop-memory restore")
    p.add_argument("in_path")
    p.add_argument("--db", default=os.environ.get("LOOP_MEMORY_DB", DEFAULT_DB))
    ns = p.parse_args(args)
    s = MemoryStore(ns.db)
    from ...storage.snapshot import restore as _restore
    try:
        return _emit(_restore(s, ns.in_path))
    except (FileNotFoundError, ValueError) as e:
        return _emit({"error": str(e)})

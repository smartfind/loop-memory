"""Mutation commands: ingest, flush, consolidate, rescore, consolidate-now."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .._common import DEFAULT_DB, default_db_path, die, parse_int_flag


def run_ingest(args) -> int:
    """loop-memory ingest <codex|claude|hermes> [path] [--max-facts N] [--limit N]"""
    from pathlib import Path

    from ...backends.embedding import HashingEmbedder
    from ...ingest.loader import default_paths, get_loader
    from ...ingest.pipeline import MemoryPipeline
    from ...storage.sqlite_store import MemoryStore

    args = list(args)
    max_facts, args = parse_int_flag(args, "--max-facts", 3)
    limit, args = parse_int_flag(args, "--limit", 0)
    if not args:
        return die("usage: loop-memory ingest <codex|claude|hermes> [path] [--max-facts N] [--limit N]")
    source = args[0]
    root = Path(args[1]).expanduser() if len(args) > 1 else None

    store = MemoryStore(DEFAULT_DB)
    loader = get_loader(source)
    base = root or default_paths()[source]
    files = list(loader.discover(base))
    if not files:
        print(f"no transcripts under {base}", file=__import__("sys").stderr)
        return 1
    if limit:
        files = files[:limit]
    pipeline = MemoryPipeline(store, embedder=HashingEmbedder(dim=128), max_facts=max_facts)
    ingested = 0
    total_rows = 0
    all_memory_ids: list = []
    run_id = store.start_pipeline_run("ingest")
    try:
        for fp in files:
            session = loader.load_one(fp)
            if session is None:
                continue
            result = pipeline.run(session)
            tag = "summary" if result.summary_items else "no-row"
            print(f"  + {fp.name}: {session.message_count:>4} turns -> {tag} {len(result.summary_items)} rows ({result.facts_count} facts)")
            ingested += 1
            total_rows += len(result.summary_items)
            try:
                for m in result.summary_items:
                    if getattr(m, "id", None):
                        all_memory_ids.append(m.id)
            except Exception:
                pass
    finally:
        store.finish_pipeline_run(
            run_id,
            in_count=len(files),
            out_count=total_rows,
            note=f"ingested {ingested} {source} sessions -> {total_rows} rows",
            stats={"evidence_ids": all_memory_ids[-200:], "source": source, "files": [f.name for f in files[:50]]},
        )
    print(f"ingested {ingested} sessions -> {total_rows} memory rows (avg {total_rows/max(1,ingested):.1f} per session)")
    return 0


def run_flush(_args) -> int:
    """Force-reingest the latest transcript of each source."""
    from ...backends.embedding import HashingEmbedder
    from ...ingest.loader import default_paths, get_loader
    from ...ingest.pipeline import MemoryPipeline
    from ...storage.sqlite_store import MemoryStore
    store = MemoryStore(DEFAULT_DB)
    pipeline = MemoryPipeline(store, embedder=HashingEmbedder(dim=128))
    n = 0
    for src in ("codex", "claude", "hermes"):
        loader = get_loader(src)
        root = default_paths()[src]
        files = list(loader.discover(root))
        if not files:
            continue
        latest = max(files, key=lambda p: p.stat().st_mtime)
        session = loader.load_one(latest)
        if session is None:
            continue
        result = pipeline.run(session)
        print(f"  flushed {src}: {latest.name} -> {len(result.summary_items)} rows")
        n += 1
    return 0


def run_consolidate(_args) -> int:
    from ...backends.embedding import HashingEmbedder
    from ...jobs.consolidate import Consolidator
    from ...storage.sqlite_store import MemoryStore
    store = MemoryStore(DEFAULT_DB)
    report = Consolidator(store, embedder=HashingEmbedder(dim=128)).run()
    print(json.dumps(report.__dict__, indent=2))
    return 0


def run_rescore(args) -> int:
    from ...storage.sqlite_store import MemoryStore
    half_life = 30.0
    if args and args[0] == "--half-life" and len(args) >= 2:
        half_life = float(args[1])
    store = MemoryStore(DEFAULT_DB)
    n = store.rescore_all(half_life)
    print(f"rescored {n} memories with half_life_days={half_life}")
    return 0


def run_dream(args) -> int:
    """loop-memory dream [--apply] [--json] [tuning flags]

    The sleep-time consolidation pass (audit 2026-10-25, loop-memory
    0.4.16). Runs a **dry sweep** by default and prints what it
    *would* do; pass ``--apply`` to actually soft-expire stale rows and
    merge near-duplicates.

    Unlike ``loop-memory consolidate`` (which hard-deletes rows past
    their TTL), the dream pass never deletes: it stamps ``invalid_at``
    so the fact leaves the current view while its history stays
    queryable through ``loop-memory recall --as-of``.
    """
    import sys

    from ...jobs.dream import dream as dream_job
    from ...storage.sqlite_store import MemoryStore

    args = list(args)
    apply_changes = False
    as_json = False
    half_life_days = 30.0
    ttl_days = 90.0
    utility_threshold = 0.30
    similarity = 0.92
    limit = 2000
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--apply":
            apply_changes = True
            i += 1
        elif a == "--json":
            as_json = True
            i += 1
        elif a in ("--half-life", "--ttl-days", "--utility", "--similarity", "--limit"):
            if i + 1 >= len(args):
                return die(f"{a} needs a value")
            raw = args[i + 1]
            try:
                if a == "--limit":
                    limit = int(raw)
                else:
                    value = float(raw)
            except ValueError:
                return die(f"{a} must be a number, got {raw!r}")
            if a == "--half-life":
                half_life_days = value
            elif a == "--ttl-days":
                ttl_days = value
            elif a == "--utility":
                utility_threshold = value
            else:
                similarity = value
            i += 2
        elif a.startswith("--"):
            return die(f"unknown flag: {a}")
        else:
            return die(f"unexpected argument: {a!r}")

    # ``default_db_path()`` (not the import-time constant) so a host
    # that changes $LOOP_MEMORY_DB after import is honoured.
    store = MemoryStore(default_db_path())
    report = dream_job(
        store,
        apply=apply_changes,
        half_life_days=half_life_days,
        ttl_days=ttl_days,
        utility_threshold=utility_threshold,
        similarity=similarity,
        limit=limit,
    )
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0

    mode = "applied" if apply_changes else "dry-run"
    print(
        f"dream ({mode}) — considered {report.considered}, "
        f"expired {report.expired}, kept {report.kept}, "
        f"superseded {report.superseded} in {report.elapsed_ms:.0f} ms"
    )
    if not apply_changes and (report.expired or report.superseded):
        print("  (dry run — re-run with --apply to write these changes)")
    for action in report.actions:
        if action.kind == "keep":
            continue
        marker = "✔" if action.action == "applied" else "•"
        print(f"  {marker} {action.kind}: {action.target_text[:72]}")
        print(f"      {action.reason}")
    return 0


def run_consolidate_now(_args) -> int:
    """Ask the running server to trigger a consolidation pass right now."""
    import json as _json
    port = os.environ.get("LOOP_MEMORY_PORT", "7767")
    url = f"http://127.0.0.1:{port}/api/admin/consolidate-now"
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read().decode())
    except urllib.error.URLError as e:
        print(f"could not reach loop-memory server on :{port}: {e.reason}", file=__import__("sys").stderr)
        print(f"hint: start the server with `loop-memory serve --port {port}`.", file=__import__("sys").stderr)
        return 1
    except Exception as e:
        print(f"request failed: {e}", file=__import__("sys").stderr)
        return 1
    if data.get("queued"):
        print("✅ consolidation run queued — check the dashboard for live progress.")
    else:
        print("✅ consolidation done:", _json.dumps(data.get("result"), indent=2))
    return 0

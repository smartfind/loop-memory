"""Server / hook / mcp commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .._common import DEFAULT_DB, die


def run_serve(args) -> int:
    port = 7767
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
    host = "127.0.0.1"
    if "--host" in args:
        i = args.index("--host")
        host = args[i + 1]
    from ...serve.app import serve as _serve
    print(f"[loop-memory] serving UI on http://{host}:{port}")
    print(f"[loop-memory] db = {DEFAULT_DB}")
    _serve(DEFAULT_DB, host=host, port=port)
    return 0


def run_hook(args) -> int:
    """Install a watcher that ingests new transcripts on change.

    Accepts one or more ``--watch <path>`` flags. Multiple watch paths
    are useful for openclaw, which has both ``agents/main/sessions``
    (clawx transcripts) and ``workspace/memory`` (daily markdown logs).
    """
    from ...backends.embedding import HashingEmbedder
    from ...ingest.loader import get_loader
    from ...ingest.pipeline import MemoryPipeline
    from ...serve.watcher import run_watcher
    from ...storage.sqlite_store import MemoryStore
    if "--source" not in args or "--watch" not in args:
        return die("usage: loop-memory hook --source <codex|claude|hermes> --watch <path> [--watch <path2> ...]")
    s_idx = args.index("--source")
    source = args[s_idx + 1]
    # Collect every --watch <path> pair (in order).
    watches: list[Path] = []
    i = 0
    while i < len(args):
        if args[i] == "--watch" and i + 1 < len(args):
            watches.append(Path(args[i + 1]).expanduser())
            i += 2
        else:
            i += 1
    if not watches:
        return die("--watch requires a path argument")
    store = MemoryStore(DEFAULT_DB)
    pipeline = MemoryPipeline(store, embedder=HashingEmbedder(dim=128))
    loader = get_loader(source)
    if len(watches) == 1:
        run_watcher(loader, watches[0], pipeline)
        return 0
    # Multiple watches: spawn one thread per path so each watcher
    # has its own poll loop and ledger (no cross-talk between
    # directories).
    import threading
    threads = []
    for w in watches:
        t = threading.Thread(
            target=run_watcher,
            args=(loader, w, pipeline),
            daemon=True,
            name=f"loop-memory-watcher-{w.name}",
        )
        t.start()
        threads.append(t)
    # Block forever (or until SIGINT) so the launchd plist keeps the
    # process alive. All work happens on the spawned threads.
    import signal as _sig
    stop = threading.Event()
    def _bye(*_): stop.set()
    _sig.signal(_sig.SIGTERM, _bye)
    _sig.signal(_sig.SIGINT, _bye)
    stop.wait()
    return 0


def run_mcp(_args) -> int:
    """Run the MCP server on stdio (newline-delimited JSON-RPC 2.0)."""
    from ...mcp import serve_stdio
    serve_stdio()
    return 0

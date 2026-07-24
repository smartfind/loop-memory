"""CLI for Loop Memory.

Usage:

    loop-memory chat              # REPL with echo LLM
    loop-memory stats             # counters
    loop-memory recall <text>     # show top memories
    loop-memory ingest codex      # ingest local Codex transcripts
    loop-memory ingest claude     # ingest local Claude Code transcripts
    loop-memory ingest hermes     # ingest Hermes transcripts
    loop-memory consolidate       # rescore + GC + dedupe
    loop-memory rescore [--half-life 30]
    loop-memory serve [--port 7767] # start the local web UI
    loop-memory mcp                # stdio MCP server (for codex/claude/hermes)
    loop-memory inject [query]     # dump long-term context block (for SessionStart hooks)
    loop-memory install-hooks      # auto-write MCP + SessionStart hooks for known clients
    loop-memory consolidate-now    # ask the running server to trigger a pass right now
    loop-memory export             # legacy markdown export (no positional path)
    loop-memory digest [--out PATH] # compact knowledge digest for AGENTS.md (≤ max-chars bytes)
    loop-memory ask "what about…"  # print a paste-ready context block for any LLM client
    loop-memory cognitive-sleep [--apply]  # dry-run / apply cognitive sweep (v7)
    loop-memory audit [--kind X] [--action Y]  # read the cognitive audit trail
    loop-memory export <out_dir>  # write a MEMORY.md bundle (v7)
    loop-memory export-bundle <out_dir>  # explicit v7 bundle alias
    loop-memory import <in_dir>   # re-hydrate a bundle
    loop-memory fork [--branch-tag T]  # snapshot every wiki page
    loop-memory graph-edge <src> <dst> [--kind K] [--weight W]  # push a relation
    loop-memory subgraph <query>  # print a small subgraph
    loop-memory graph-rebuild  # rebuild entities + entity_mentions
"""

from __future__ import annotations

import sys

from .commands import cognitive as cognitive_cmd
from .commands import diag as diag_cmd
from .commands import graph as graph_cmd
from .commands import hooks as hooks_cmd
from .commands import read as read_cmd
from .commands import serve as serve_cmd
from .commands import write as write_cmd

# Backwards-compatible re-exports. Earlier tests imported these
# private symbols from this module; keep the names available so
# external code doesn't break after the 0.3.0 split.
_upsert_block = hooks_cmd._upsert_block
cmd_inject = read_cmd.run_inject


def _run_export(args: list[str]) -> int:
    """Keep the legacy markdown export and expose the v7 bundle export.

    A positional output directory selects the v7 bundle. The historical
    ``--out`` / ``--q`` form remains available so existing scripts keep
    producing a single markdown file.
    """
    if not args or any(flag in args for flag in ("--out", "--q")):
        return read_cmd.run_export(args)
    return cognitive_cmd.run_export(args)


# Dispatch table: command name -> (callable, optional module doc).
# Each module's ``run_<name>`` function takes ``args: list[str]`` and
# returns an integer exit code. Keep the keys matching the docstring
# at the top of this file.
COMMANDS = {
    "chat": read_cmd.run_chat,
    "stats": read_cmd.run_stats,
    "recall": read_cmd.run_recall,
    "ingest": write_cmd.run_ingest,
    "consolidate": write_cmd.run_consolidate,
    "consolidate-now": write_cmd.run_consolidate_now,
    "export": _run_export,
    "digest": read_cmd.run_digest,
    "ask": read_cmd.run_ask,
    "rescore": write_cmd.run_rescore,
    "serve": serve_cmd.run_serve,
    "hook": serve_cmd.run_hook,
    "mcp": serve_cmd.run_mcp,
    "inject": read_cmd.run_inject,
    "install-hooks": hooks_cmd.run_install_hooks,
    "flush": write_cmd.run_flush,
    "graph": graph_cmd.run_graph,
    "doctor": diag_cmd.run_doctor,
    "status": diag_cmd.run_status,
    "openclaw-setup": diag_cmd.run_openclaw_setup,
    # Universal Agent Memory v7 — graph, cognitive, export, fork
    "cognitive-sleep": cognitive_cmd.run_cognitive_sleep,
    "audit": cognitive_cmd.run_audit,
    "export-bundle": cognitive_cmd.run_export,
    "import": cognitive_cmd.run_import,
    "fork": cognitive_cmd.run_fork,
    "graph-edge": cognitive_cmd.run_graph_edge,
    "subgraph": cognitive_cmd.run_subgraph,
    "graph-rebuild": cognitive_cmd.run_graph_rebuild,
}


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())

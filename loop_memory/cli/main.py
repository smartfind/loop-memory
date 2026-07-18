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
    loop-memory export             # write distilled wiki as markdown to ~/loop-memory-export-<date>.md
    loop-memory ask "what about…"  # print a paste-ready context block for any LLM client
"""

from __future__ import annotations

import sys

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
    "export": read_cmd.run_export,
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

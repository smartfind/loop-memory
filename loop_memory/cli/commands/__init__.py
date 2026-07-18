"""CLI command implementations.

Each module in this package exports a small ``run(args) -> int``
function. ``cli.main`` is responsible only for dispatch.

Modules are grouped by concern:

* ``read``   — read-only commands (chat, stats, recall, ask, export, inject)
* ``write``  — mutation commands (ingest, flush, consolidate, rescore)
* ``serve``  — server / hook / mcp commands
* ``hooks``  — install-hooks auto-config
* ``graph``  — knowledge graph commands
"""

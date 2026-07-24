"""White-box export for the Universal Agent Memory contract.

The article the user pointed us at proposes a local-first,
Git-trackable, human-readable ``MEMORY.md`` as the storage format
for the long-term layer. This package brings that idea to
loop-memory without breaking the SQLite store — ``export_bundle``
turns the live state into a directory of Markdown + JSON, and
``import_bundle`` rehydrates it.

The bundle layout (under the chosen ``out_dir``) is:

    out_dir/
        MEMORY.md            # top-level "what we know about you"
        INDEX.md             # file map
        pages/               # one .md per wiki page (slug-named)
        memories.jsonl       # raw memories (one JSON per line)
        graph.json           # entities + relations
        meta.json            # schema version, export time, agent_id, user_id
        sessions.json        # sessions index (titles + timestamps)

Git-friendly by design: every file is small, text-only, and
deterministically sorted so a diff shows exactly what the agent
learned. Re-importing is idempotent (upsert by slug for pages,
``(agent_id, user_id, external_id)`` for memories).
"""

from .memory_md import (
    export_bundle,
    import_bundle,
    fork_snapshot,
    write_memory_md,
)

__all__ = [
    "export_bundle",
    "import_bundle",
    "fork_snapshot",
    "write_memory_md",
]

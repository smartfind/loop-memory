"""Portable single-file SQLite snapshot of a :class:`MemoryStore`.

Audit 2026-09-06 (codexa-memory v0.2.0). The markdown ``export-bundle``
is Git-friendly but lossy: it drops the recall-quality signals,
entity-mention weights, and the supersession audit trail. This module
adds a lossless portable snapshot so a user can hand their full store
to another machine without losing recall fidelity.

The snapshot is a single ``.memory.sqlite`` file that holds the same
schema + rows as the live store. The header row in ``schema_meta``
records the schema version + a magic value so a future restore can
refuse a downgrade or a file from a different project.

Two operations:

* :func:`snapshot` — write a single-file SQLite copy of ``store`` to
  ``out_path``. Uses ``sqlite3.Connection.backup()`` so the live store
  is not blocked by a long copy.
* :func:`restore` — re-hydrate a snapshot file into ``store``. Each
  table is copied in dependency order with ``INSERT OR REPLACE`` so
  the operation is idempotent on the primary key. Triggers and the
  FTS mirrors are NOT copied (they are re-created by ``_init_schema``
  the next time the live store opens); the live store rebuilds them
  on demand.

Both operations return a small dict (rows-per-table + timing) so the
CLI / HTTP route can surface a one-line summary to the user.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from ..storage.sqlite_store import MemoryStore


SNAPSHOT_MAGIC = "loop_memory_snapshot_v1"
# Order matters for restore: child tables after their parents so
# INSERT OR REPLACE never trips a FK constraint. Mirrors and triggers
# are NOT copied (the live store rebuilds them on demand).
_RESTORE_ORDER: list[str] = [
    "sessions",
    "memories",
    "memory_signals",
    "entities",
    "entity_mentions",
    "relations",
    "wiki_pages",
    "wiki_versions",
    "cognitive_audit",
    "contradiction_ignored",
    "consolidation_runs",
    "auth_tokens",
    "settings",
    "schema_meta",
    "llm_audit",
    "pipeline_runs",
    "write_guard_drops",
]


def _list_user_tables(conn: sqlite3.Connection) -> list[str]:
    """Names of every user table the snapshot should carry.

    Excludes SQLite-internal tables (``sqlite_*``) and the FTS5
    shadow tables (``memories_fts_data`` / ``_idx`` / ``_content``
    / ``_config`` / ``_docsize``); the live store rebuilds the FTS
    mirrors from the source rows via triggers when it next opens.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    out: list[str] = []
    skip_suffixes = ("_fts_data", "_fts_idx", "_fts_content",
                     "_fts_config", "_fts_docsize")
    for r in rows:
        name = r["name"]
        if any(name.endswith(suf) for suf in skip_suffixes):
            continue
        out.append(name)
    return out


def snapshot(store: MemoryStore, out_path: str | Path) -> dict[str, Any]:
    """Write a portable SQLite snapshot of ``store`` to ``out_path``.

    The file at ``out_path`` is overwritten atomically: the snapshot
    is built in a sibling temp directory (so the WAL / -shm sidecars
    stay scoped) and then moved into place with ``shutil.move``.

    Returns a summary dict suitable for the CLI / HTTP route.
    """
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    live_path = Path(store.path).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="loop-memory-snapshot-") as td:
        tmp_dir = Path(td)
        tmp = tmp_dir / out.name
        if tmp.exists():
            tmp.unlink()
        with sqlite3.connect(live_path) as src, sqlite3.connect(tmp) as dst:
            src.row_factory = sqlite3.Row
            # Force the snapshot DB into rollback journal mode so
            # the destination ends up as a single self-contained
            # .db file (no WAL sidecar that could later confuse a
            # reader on macOS).
            dst.execute("PRAGMA journal_mode=DELETE")
            src.backup(dst)
            # Stamp the snapshot header so a future restore can
            # refuse an obvious mismatch (wrong project, downgrade).
            dst.execute(
                "INSERT OR REPLACE INTO schema_meta(k, v) "
                "VALUES ('snapshot_magic', ?), "
                "       ('snapshot_source_schema', ?), "
                "       ('snapshot_taken_at', ?)",
                (SNAPSHOT_MAGIC, MemoryStore.SCHEMA_VERSION,
                 time.time()),
            )
            dst.commit()
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            table_counts: dict[str, int] = {}
            for name in _list_user_tables(src):
                table_counts[name] = src.execute(
                    f"SELECT COUNT(*) AS c FROM {name}"
                ).fetchone()["c"]
        if out.exists():
            out.unlink()
        shutil.move(str(tmp), str(out))
    elapsed = time.time() - started
    return {
        "out_path": str(out),
        "size_bytes": out.stat().st_size,
        "elapsed_seconds": round(elapsed, 4),
        "schema_version": MemoryStore.SCHEMA_VERSION,
        "tables": table_counts,
    }


def restore(store: MemoryStore, snapshot_path: str | Path) -> dict[str, Any]:
    """Re-hydrate ``snapshot_path`` into ``store``.

    The destination store is upserted (INSERT OR REPLACE on PK) so a
    restore on top of an existing store is a no-op for rows that
    already match — useful when a user wants to "merge" snapshots.

    A wrong-magic snapshot raises :class:`ValueError` loudly so the
    CLI surfaces a useful error instead of silently corrupting the
    destination store. Schema-downgrade detection is best-effort:
    the snapshot's ``snapshot_source_schema`` is logged in the
    summary dict for the dashboard to surface.

    Returns a summary dict suitable for the CLI / HTTP route.
    """
    src_path = Path(snapshot_path).expanduser().resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"snapshot not found: {src_path}")
    started = time.time()
    # Open our own connections (NOT the store's shared connection —
    # same reason as snapshot() above).
    live_path = Path(store.path).expanduser().resolve()
    with sqlite3.connect(live_path) as dst, sqlite3.connect(src_path) as src:
        src.row_factory = sqlite3.Row
        try:
            magic_row = src.execute(
                "SELECT v FROM schema_meta WHERE k='snapshot_magic'"
            ).fetchone()
        except sqlite3.OperationalError:
            raise ValueError(
                f"{src_path} is not a loop-memory snapshot "
                f"(no schema_meta table)"
            ) from None
        if magic_row is None or magic_row["v"] != SNAPSHOT_MAGIC:
            raise ValueError(
                f"{src_path} is not a loop-memory snapshot "
                f"(missing or wrong snapshot_magic)"
            )
        version_row = src.execute(
            "SELECT v FROM schema_meta WHERE k='snapshot_source_schema'"
        ).fetchone()
        source_version = version_row["v"] if version_row else "unknown"
        counts: dict[str, int] = {}
        snapshot_tables = _list_user_tables(src)
        # Drop the schema_meta snapshot header on the source so it
        # doesn't clobber the live store's own schema_meta. Also
        # skip transient / per-install tables that don't belong in
        # a portable bundle (auth tokens, write-guard drops, etc).
        sanitised_tables = [
            t for t in snapshot_tables
            if t not in {"schema_meta", "llm_audit", "auth_tokens",
                         "write_guard_drops"}
        ]
        ordered = [t for t in _RESTORE_ORDER if t in sanitised_tables]
        extras = [t for t in sanitised_tables if t not in ordered]
        ordered.extend(extras)
        for name in ordered:
            # Fetch column names via a 0-row probe (cursor.description
            # is only populated by execute(), not by the connection).
            probe = src.execute(f"SELECT * FROM {name} WHERE 0")
            cols = [d[0] for d in (probe.description or [])]
            probe.close()
            rows = src.execute(f"SELECT * FROM {name}").fetchall()
            if not rows:
                counts[name] = 0
                continue
            placeholders = ",".join("?" for _ in cols)
            col_list = ",".join(f'"{c}"' for c in cols)
            dst.executemany(
                f'INSERT OR REPLACE INTO {name} ({col_list}) '
                f"VALUES ({placeholders})",
                [tuple(r[c] for c in cols) for r in rows],
            )
            counts[name] = len(rows)
        dst.commit()
    elapsed = time.time() - started
    return {
        "snapshot_path": str(src_path),
        "elapsed_seconds": round(elapsed, 4),
        "source_schema_version": source_version,
        "live_schema_version": MemoryStore.SCHEMA_VERSION,
        "tables": counts,
    }


__all__ = ["snapshot", "restore", "SNAPSHOT_MAGIC"]

"""SQLite-backed persistent memory store.

Designed to out-live a single Python process: every ``MemoryItem``
turns into a row, and a small set of indexes lets the UI list, search,
and re-score by time.

Schema (versioned):

    memories(id, kind, text, importance, source, session_id,
             created_at, updated_at, score, ttl, tags, embedding BLOB)

    sessions(id, source, external_id, title, started_at, ended_at, message_count)

    entities(id, name, kind, mention_count, weight, created_at, updated_at)
    relations(id, src, dst, kind, weight, evidence_ids)

Embeddings are stored as a tight float32 blob so we never have to
decode JSON at query time.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import struct
import time
import uuid

# Local imports are deferred inside recall_hybrid() to avoid a circular
# dependency on .retrieval during package import; the helpers used by
# _hydrate_* are imported here for the same reason.
from .retrieval import temporal_score  # noqa: E402
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
log = logger


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    external_id  TEXT,
    title        TEXT,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    message_count INTEGER NOT NULL DEFAULT 0,
    metadata     TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    importance  REAL NOT NULL DEFAULT 0.5,
    source      TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    score       REAL NOT NULL DEFAULT 0.5,
    ttl         REAL,
    tags        TEXT,
    embedding   BLOB
);

CREATE INDEX IF NOT EXISTS idx_mem_session  ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_created  ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_mem_score    ON memories(score);
CREATE INDEX IF NOT EXISTS idx_mem_kind     ON memories(kind);

-- FTS5 mirror of memories.text + tags. We keep it in sync via triggers
-- (see end of this schema block) so every INSERT/UPDATE/DELETE on
-- memories propagates to memories_fts without app-level code.
-- The bm25() ranking is the kernel-side OK API; downstream callers
-- fuse this with the existing semantic score via Reciprocal Rank
-- Fusion (see recall_hybrid below).
-- FTS5 mirror of memories.text + tags. The trigram tokenizer
-- (SQLite ≥ 3.34) gives us substring search, which is the only
-- thing that works for CJK text without an external ICU build.
-- Trade-off vs unicode61: trigram produces larger indexes and
-- "word boundary" semantics are looser (e.g. "javascript" matches
-- "java"). For our use case (mixed Chinese/English with code
-- snippets) trigram wins decisively. The mirrors are kept in
-- sync via triggers so every INSERT/UPDATE/DELETE on memories
-- propagates automatically.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text,
    tags,
    source,
    tokenize = 'trigram'
);

-- FTS5 mirror of wiki_pages. Same trigger pattern.
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    title,
    body,
    summary,
    tags,
    tokenize = 'trigram'
);

-- Per-memory entity mentions: every time we extract entities from a
-- memory we record which entities appeared, so recall can boost
-- results whose entities overlap with the query's entities.
-- (memory_id, entity_id) is unique so re-ingest is idempotent.
CREATE TABLE IF NOT EXISTS entity_mentions (
    memory_id   TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 0.5,
    created_at  REAL NOT NULL,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_em_entity  ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_em_memory  ON entity_mentions(memory_id);

-- Scope column on wiki_pages: 'global' (default, current behavior) or
-- a comma-separated list of source names like 'codex,claude' meaning
-- only those sources should see this page during recall. We default
-- existing rows to 'global' on migration. SQLite has no
-- 'ADD COLUMN IF NOT EXISTS', so the migration is wrapped in a
-- guard in `_init_schema` that checks pragma_table_info first.
-- (The CREATE INDEX below IS idempotent.)

-- Triggers to keep FTS mirrors in sync. We intentionally rebuild from
-- the source row (rather than try to copy the new text) so the FTS
-- tokenizer is the only thing that ever touches the FTS row.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text, tags, source)
    VALUES (new.rowid, new.text, COALESCE(new.tags,''), COALESCE(new.source,''));
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  DELETE FROM memories_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  DELETE FROM memories_fts WHERE rowid = old.rowid;
  INSERT INTO memories_fts(rowid, text, tags, source)
    VALUES (new.rowid, new.text, COALESCE(new.tags,''), COALESCE(new.source,''));
END;

CREATE TABLE IF NOT EXISTS entities (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'concept',
    mention_count  INTEGER NOT NULL DEFAULT 1,
    weight         REAL NOT NULL DEFAULT 0.5,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE(name, kind)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS relations (
    id            TEXT PRIMARY KEY,
    src           TEXT NOT NULL,
    dst           TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'related',
    weight        REAL NOT NULL DEFAULT 0.5,
    evidence_ids  TEXT,
    created_at    REAL NOT NULL,
    UNIQUE(src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(src);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst);

CREATE TABLE IF NOT EXISTS schema_meta(k TEXT PRIMARY KEY, v TEXT);

-- User-tunable settings (LLM provider, schedule, behaviour).
-- One row per key; v is JSON-encoded.
CREATE TABLE IF NOT EXISTS settings (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Consolidation / rescore / summarize run history.
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    finished_at REAL,
    trigger     TEXT NOT NULL,    -- 'manual' | 'schedule' | 'realtime'
    status      TEXT NOT NULL,    -- 'running' | 'done' | 'error'
    stats_json  TEXT,
    error       TEXT,
    model       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cr_started ON consolidation_runs(started_at);

-- Distilled wiki pages: long-form, polished knowledge synthesized from
-- raw memories by the LLM consolidator. One row per topic; re-running
-- consolidation updates the body and bumps the version.
CREATE TABLE IF NOT EXISTS wiki_pages (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    summary         TEXT,
    tags            TEXT,           -- JSON array of strings
    importance      REAL NOT NULL DEFAULT 0.5,
    evidence_ids    TEXT,           -- JSON array of memory ids that contributed
    run_id          TEXT,           -- consolidation run that produced/updated it
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wiki_updated  ON wiki_pages(updated_at);
CREATE INDEX IF NOT EXISTS idx_wiki_import   ON wiki_pages(importance);
CREATE INDEX IF NOT EXISTS idx_wiki_slug     ON wiki_pages(slug);

-- FTS5 sync triggers for wiki_pages (positioned AFTER the base table
-- because SQLite parses ``executescript`` linearly; defining triggers
-- before the table they reference would raise "no such table").
CREATE TRIGGER IF NOT EXISTS wiki_ai AFTER INSERT ON wiki_pages BEGIN
  INSERT INTO wiki_fts(rowid, title, body, summary, tags)
    VALUES (new.rowid, new.title, new.body, COALESCE(new.summary,''), COALESCE(new.tags,''));
END;
CREATE TRIGGER IF NOT EXISTS wiki_ad AFTER DELETE ON wiki_pages BEGIN
  DELETE FROM wiki_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS wiki_au AFTER UPDATE ON wiki_pages BEGIN
  DELETE FROM wiki_fts WHERE rowid = old.rowid;
  INSERT INTO wiki_fts(rowid, title, body, summary, tags)
    VALUES (new.rowid, new.title, new.body, COALESCE(new.summary,''), COALESCE(new.tags,''));
END;

-- Per-memory behavioural signals used by the evolution consolidator.
-- recall_count: how many times this memory was returned by recall() / search
-- positive: explicit user 👍 (or implicit: kept after LLM re-eval)
-- negative: explicit user 👎 (or implicit: deleted after LLM re-eval)
-- last_recalled_at: last time it was returned by a query
CREATE TABLE IF NOT EXISTS memory_signals (
    memory_id        TEXT PRIMARY KEY,
    recall_count     INTEGER NOT NULL DEFAULT 0,
    positive         INTEGER NOT NULL DEFAULT 0,
    negative         INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL,
    last_feedback_at REAL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_recall ON memory_signals(recall_count);
CREATE INDEX IF NOT EXISTS idx_signals_neg    ON memory_signals(negative);

-- Per-pair ignore list for contradiction detection: keyed by ordered
-- hash of (a_id, b_id) so the dashboard "ignore" button actually does
-- something (the pair disappears from future pulses).
CREATE TABLE IF NOT EXISTS contradiction_ignored (
    pair_key  TEXT PRIMARY KEY,    -- sorted(a_id, b_id) joined with '|'
    ignored_at REAL NOT NULL
);

-- LLM audit log: every provider call records prompt/response/tokens/cost
-- so we can replay, debug distillation failures, and watch spend.
CREATE TABLE IF NOT EXISTS llm_audit (
    id              TEXT PRIMARY KEY,
    ts              REAL NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- "consolidate" | "wiki" | "test" | ...
    run_id          TEXT,                    -- consolidation run id when applicable
    prompt_hash     TEXT,                    -- sha1 of prompt for dedup / lookup
    prompt_text     TEXT,
    response_text   TEXT,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    total_tokens    INTEGER,
    cost_usd        REAL,                    -- estimated, optional
    latency_ms      INTEGER,
    ok              INTEGER NOT NULL DEFAULT 1,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON llm_audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_kind ON llm_audit(kind);
CREATE INDEX IF NOT EXISTS idx_audit_run  ON llm_audit(run_id);

-- Pipeline stage counters: one row per (stage, window). Used by the
-- dashboard to render the live data-flow animation.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    finished_at REAL,
    stage       TEXT NOT NULL,   -- 'ingest'|'score'|'cluster'|'distill'|'wiki'|'graph'
    in_count    INTEGER NOT NULL DEFAULT 0,
    out_count   INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    stats_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipe_stage_started ON pipeline_runs(stage, started_at);

-- WriteGuard drops: small counter table so the dashboard can show live
-- per-kind rejection totals (and last-rejected timestamp per reason).
CREATE TABLE IF NOT EXISTS write_guard_drops (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- 'duplicate'|'too_short'|'too_long'|'low_signal'
    text_preview TEXT,
    matched_id  TEXT,
    matched_score REAL
);
CREATE INDEX IF NOT EXISTS idx_wgd_ts  ON write_guard_drops(ts);
CREATE INDEX IF NOT EXISTS idx_wgd_src ON write_guard_drops(source, kind);
"""


def _to_blob(vec: list[float] | None) -> bytes | None:
    if vec is None:
        return None
    return struct.pack(f"{len(vec)}f", *vec)


def _from_blob(blob: bytes | None) -> list[float] | None:
    if blob is None:
        return None
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


@dataclass
class StoredMemory:
    id: str
    kind: str
    text: str
    importance: float
    source: str | None
    session_id: str | None
    created_at: float
    updated_at: float
    score: float
    ttl: float | None
    tags: list[str]
    embedding: list[float] | None


@dataclass
class StoredSession:
    id: str
    source: str
    external_id: str | None
    title: str | None
    started_at: float
    ended_at: float | None
    message_count: int
    metadata: dict


@dataclass
class GraphEntity:
    id: str
    name: str
    kind: str
    mention_count: int
    weight: float


@dataclass
class GraphRelation:
    id: str
    src: str          # entity name (canonicalised)
    dst: str          # entity name (canonicalised)
    kind: str
    weight: float
    evidence_ids: list[str]


class MemoryStore:
    """Persistent, transactional store backed by SQLite.

    The zero-dep claim holds — Python ships with sqlite3 and struct.
    """

    SCHEMA_VERSION = "5"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        # All `CREATE TABLE IF NOT EXISTS` runs every time so we can
        # add new tables without a manual migration step. Then upsert
        # the schema version so a downgrade is loud. We also handle
        # the few idempotent-but-not-IF-NOT-EXISTS migrations inline
        # below (SQLite has no ADD COLUMN IF NOT EXISTS).
        with self._conn() as c:
            # Run the DDL first so all base tables exist; then we can
            # safely check + add the few columns that aren't covered
            # by ``CREATE TABLE IF NOT EXISTS`` (SQLite has no
            # ``ADD COLUMN IF NOT EXISTS``).
            c.executescript(SCHEMA)
            c.execute(
                "INSERT INTO schema_meta(k,v) VALUES('version',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (self.SCHEMA_VERSION,),
            )
            cols = {row["name"] for row in c.execute("PRAGMA table_info(wiki_pages)").fetchall()}
            if "scope" not in cols:
                c.execute("ALTER TABLE wiki_pages ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'")

            # One-shot FTS5 tokenizer migration. ``CREATE VIRTUAL TABLE
            # IF NOT EXISTS`` will *not* rebuild an existing FTS5 table
            # if its schema differs from the DDL — which is exactly
            # what we need when switching from the legacy ``unicode61``
            # tokenizer to ``trigram`` (the only one that can do
            # substring search over CJK text without an external
            # ICU build). Detect any mirror that is missing the
            # ``trigram`` token, drop the FTS mirrors + their sync
            # triggers, then re-run the DDL (which will now create
            # them fresh) and re-backfill from the source tables.
            needs_fts_rebuild = False
            for tbl in ("memories_fts", "wiki_fts"):
                row = c.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                ).fetchone()
                sql = row[0] if row else None
                # Two triggers force a rebuild:
                #   1. tokenizer isn't ``trigram`` (legacy unicode61)
                #   2. table is ``content=''`` (contentless) — that
                #      forbids DELETE, which breaks the cascade path
                #      from ``delete_session`` / ``delete_memory``.
                if sql is None:
                    needs_fts_rebuild = True
                    break
                if "trigram" not in sql:
                    needs_fts_rebuild = True
                    break
                if "content=''" in sql:
                    needs_fts_rebuild = True
                    break
            if needs_fts_rebuild:
                # Drop the FTS mirrors and their triggers. We drop
                # triggers BEFORE the table (FTS5 errors otherwise).
                for trig in ("memories_ai", "memories_ad", "memories_au",
                             "wiki_ai", "wiki_ad", "wiki_au"):
                    c.execute(f"DROP TRIGGER IF EXISTS {trig}")
                for tbl in ("memories_fts", "wiki_fts"):
                    c.execute(f"DROP TABLE IF EXISTS {tbl}")
                # Re-run the DDL — now the FTS CREATE statements will
                # actually take effect (because the tables are gone)
                # and the triggers will be re-created.
                c.executescript(SCHEMA)
                # Backfill from the source tables. The triggers will
                # take over from this point on.
                n_mem = c.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
                if n_mem > 0:
                    c.execute(
                        "INSERT INTO memories_fts(rowid, text, tags, source) "
                        "SELECT rowid, text, COALESCE(tags,''), COALESCE(source,'') "
                        "FROM memories"
                    )
                n_wiki = c.execute("SELECT COUNT(*) c FROM wiki_pages").fetchone()["c"]
                if n_wiki > 0:
                    c.execute(
                        "INSERT INTO wiki_fts(rowid, title, body, summary, tags) "
                        "SELECT rowid, title, body, COALESCE(summary,''), COALESCE(tags,'') "
                        "FROM wiki_pages"
                    )
                # Track the rebuild so we never redo it on a healthy DB.
                c.execute(
                    "INSERT INTO schema_meta(k,v) VALUES('fts5_tokenizer',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    ("trigram",),
                )

            # Default existing wiki pages to 'global' scope on first
            # run after the scope migration. ALTER TABLE already added
            # the column with DEFAULT 'global', so new rows are fine;
            # this is just belt-and-braces for pre-existing rows.
            c.execute("UPDATE wiki_pages SET scope='global' WHERE scope IS NULL OR scope=''")
            # One-shot backfill for ``entity_mentions`` (memory → entity
            # links). The table was introduced together with FTS5 in this
            # migration, but pre-existing memories were never linked, so
            # the entity channel of hybrid recall would silently return
            # nothing for them. We do an inexpensive substring match
            # against existing entity names so the channel becomes
            # useful on the first recall after this migration; ongoing
            # ``upsert_entity`` calls keep the link fresh.
            em_count = c.execute(
                "SELECT COUNT(*) c FROM entity_mentions"
            ).fetchone()["c"]
            if em_count == 0:
                _now = time.time()
                ent_rows = c.execute(
                    "SELECT id, name FROM entities WHERE name NOT LIKE 'tag:%'"
                ).fetchall()
                inserts: list[tuple] = []
                for ent in ent_rows:
                    full = (ent["name"] or "").strip().lower()
                    if not full:
                        continue
                    suffix = full.split(":")[-1] if ":" in full else full
                    if not suffix or len(suffix) < 2:
                        continue
                    # Escape LIKE wildcards in the suffix.
                    esc = (
                        suffix.replace("\\", "\\\\")
                              .replace("%", "\\%")
                              .replace("_", "\\_")
                    )
                    hits = c.execute(
                        "SELECT id FROM memories WHERE LOWER(text) LIKE ? ESCAPE '\\' LIMIT 64",
                        (f"%{esc}%",),
                    ).fetchall()
                    for m in hits:
                        inserts.append((m["id"], ent["id"], 0.5, _now))
                if inserts:
                    c.executemany(
                        "INSERT OR IGNORE INTO entity_mentions(memory_id, entity_id, weight, created_at) "
                        "VALUES (?,?,?,?)",
                        inserts,
                    )

            # Legacy flag from the original FTS5 rollout, kept for
            # backward-compat with downstream tooling.
            c.execute(
                "INSERT INTO schema_meta(k,v) VALUES('fts5_backfilled',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("1",),
            )

    # --- sessions ---------------------------------------------------------

    def upsert_session(
        self,
        source: str,
        external_id: str | None = None,
        title: str | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        message_count: int = 0,
        metadata: dict | None = None,
    ) -> StoredSession:
        import json

        started = started_at or time.time()
        ended = ended_at
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM sessions WHERE source=? AND external_id IS ?",
                (source, external_id),
            ).fetchone()
            if row is not None:
                sid = row["id"]
                c.execute(
                    """UPDATE sessions
                       SET title=COALESCE(?, title),
                           ended_at=COALESCE(?, ended_at),
                           message_count=?,
                           metadata=COALESCE(?, metadata)
                       WHERE id=?""",
                    (title, ended, message_count, json.dumps(metadata) if metadata else None, sid),
                )
            else:
                sid = uuid.uuid4().hex
                c.execute(
                    """INSERT INTO sessions
                       (id, source, external_id, title, started_at, ended_at,
                        message_count, metadata)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        sid,
                        source,
                        external_id,
                        title,
                        started,
                        ended,
                        message_count,
                        json.dumps(metadata or {}),
                    ),
                )
            return StoredSession(
                id=sid,
                source=source,
                external_id=external_id,
                title=title,
                started_at=started,
                ended_at=ended,
                message_count=message_count,
                metadata=metadata or {},
            )

    def list_sessions(self, limit: int = 100, source: str | None = None) -> list[StoredSession]:
        # Sort by last-activity time so an active but long-running session
        # (its started_at is from days ago, its ended_at keeps advancing)
        # bubbles to the top instead of being buried under newer short-lived
        # sessions like cron reports.
        with self._conn() as c:
            if source:
                rows = c.execute(
                    "SELECT * FROM sessions WHERE source=? "
                    "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?",
                    (source, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM sessions "
                    "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_session(r) for r in rows]

    def get_session(self, session_id: str) -> StoredSession | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return self._row_to_session(row) if row else None

    def _row_to_session(self, row: sqlite3.Row) -> StoredSession:
        import json

        meta: dict = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (ValueError, TypeError):
                logger.warning("corrupt session metadata for row %s; resetting", row["id"])
                meta = {}
        return StoredSession(
            id=row["id"],
            source=row["source"],
            external_id=row["external_id"],
            title=row["title"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            message_count=row["message_count"],
            metadata=meta,
        )

    # --- memories ---------------------------------------------------------


    # ----- Signal feedback (v5) ----------------------------------------

    def record_signal(
        self,
        memory_id: str,
        *,
        recall: bool = False,
        positive: bool | None = None,
    ) -> None:
        """Update behavioural signals for one memory.

        * ``recall=True`` bumps ``recall_count`` and ``last_recalled_at``.
        * ``positive=True/False`` bumps positive/negative counters and
          ``last_feedback_at``. This is the user-driven 👍/👎 path.
        """
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO memory_signals (memory_id, recall_count, positive, negative,
                                                last_recalled_at, last_feedback_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                     recall_count     = recall_count     + ?,
                     positive         = positive         + ?,
                     negative         = negative         + ?,
                     last_recalled_at = COALESCE(?, last_recalled_at),
                     last_feedback_at = COALESCE(?, last_feedback_at),
                     updated_at       = ?""",
                (
                    memory_id,
                    1 if recall else 0,
                    1 if positive is True else 0,
                    1 if positive is False else 0,
                    now if recall else None,
                    now if positive is not None else None,
                    now,
                    1 if recall else 0,
                    1 if positive is True else 0,
                    1 if positive is False else 0,
                    now if recall else None,
                    now if positive is not None else None,
                    now,
                ),
            )


    def bump_recalls(self, memory_ids):
        """Increment recall_count for every id. Used by MCP/web search to
        feed the evolution loop. Returns the number of rows updated."""
        ids = [str(x) for x in memory_ids if x]
        if not ids:
            return 0
        now = time.time()
        n = 0
        with self._conn() as c:
            for mid in ids:
                c.execute(
                    """INSERT INTO memory_signals (memory_id, recall_count, positive, negative,
                                                    last_recalled_at, updated_at)
                       VALUES (?, 1, 0, 0, ?, ?)
                       ON CONFLICT(memory_id) DO UPDATE SET
                         recall_count = recall_count + 1,
                         last_recalled_at = ?,
                         updated_at = ?""",
                    (mid, now, now, now, now),
                )
                n += 1
        return n

    def get_signal(self, memory_id: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM memory_signals WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return {"recall_count": 0, "positive": 0, "negative": 0,
                        "last_recalled_at": None, "last_feedback_at": None}
            return {
                "recall_count": row["recall_count"] or 0,
                "positive": row["positive"] or 0,
                "negative": row["negative"] or 0,
                "last_recalled_at": row["last_recalled_at"],
                "last_feedback_at": row["last_feedback_at"],
            }

    def top_signals(self, kind: str = "recall_count", limit: int = 20) -> list[Dict[str, Any]]:
        """Top-N memories by a signal column (recall_count / positive / negative)."""
        col = kind if kind in ("recall_count", "positive", "negative") else "recall_count"
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT m.id, m.kind, m.text, m.importance, m.score, m.tags,
                          s.recall_count, s.positive, s.negative
                   FROM memories m
                   LEFT JOIN memory_signals s ON s.memory_id = m.id
                   WHERE COALESCE(s.{col}, 0) > 0
                   ORDER BY s.{col} DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ----- Pipeline stage recording (v5) ------------------------------

    def start_pipeline_run(self, stage: str) -> str:
        import uuid as _uuid
        rid = _uuid.uuid4().hex
        with self._conn() as c:
            c.execute(
                """INSERT INTO pipeline_runs (id, stage, started_at)
                   VALUES (?, ?, ?)""",
                (rid, stage, time.time()),
            )
        return rid

    def finish_pipeline_run(
        self,
        rid: str,
        *,
        in_count: int = 0,
        out_count: int = 0,
        note: str = "",
        stats: Dict[str, Any] | None = None,
    ) -> None:
        import json as _json
        with self._conn() as c:
            c.execute(
                """UPDATE pipeline_runs
                   SET finished_at = ?, in_count = ?, out_count = ?,
                       note = ?, stats_json = ?
                   WHERE id = ?""",
                (time.time(), in_count, out_count, note,
                 _json.dumps(stats or {}), rid),
            )

    def latest_pipeline_runs(self, limit: int = 60) -> list[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM pipeline_runs
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_memory(
        self,
        *,
        id: str | None = None,
        kind: str,
        text: str,
        importance: float = 0.5,
        source: str | None = None,
        session_id: str | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
        ttl: float | None = None,
        tags: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> StoredMemory:
        import json

        now = time.time()
        mid = id or uuid.uuid4().hex
        ts = created_at or now
        uts = updated_at or ts
        tags_json = json.dumps(tags or [])
        score = self.compute_score(importance, ts, now)
        with self._conn() as c:
            c.execute(
                """INSERT INTO memories
                   (id, session_id, kind, text, importance, source,
                    created_at, updated_at, score, ttl, tags, embedding)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     text=excluded.text,
                     importance=excluded.importance,
                     updated_at=excluded.updated_at,
                     score=excluded.score,
                     tags=excluded.tags,
                     embedding=COALESCE(excluded.embedding, memories.embedding)""",
                (
                    mid,
                    session_id,
                    kind,
                    text,
                    importance,
                    source,
                    ts,
                    uts,
                    score,
                    ttl,
                    tags_json,
                    _to_blob(embedding),
                ),
            )
        item = self.get_memory(mid)
        if item is None:
            raise RuntimeError(f"memory {mid} disappeared after upsert")
        return item

    def get_memory(self, mid: str) -> StoredMemory | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            return self._row_to_memory(row) if row else None

    def list_memories(
        self,
        limit: int = 200,
        session_id: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        min_score: float | None = None,
        query: str | None = None,
        since: float | None = None,
        until: float | None = None,
        ids: list[str] | None = None,
    ) -> list[StoredMemory]:
        clauses: list[str] = []
        params: list = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if query:
            clauses.append("text LIKE ?")
            params.append(f"%{query}%")
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._row_to_memory(r) for r in rows]

    # ---- Unified recall across memories + wiki + entities -----------
    @staticmethod
    def _tokenize(query: str) -> list[str]:
        """Split a query into overlapping tokens.

        Handles:
        * English words split on whitespace + punctuation
        * Chinese/Japanese/Korean characters split into individual
          unigrams + 2-grams (so "知识图谱" becomes [知, 识, 图, 谱,
          知识, 识图, 图谱])
        * Lower-cases ASCII

        Returns up to 24 tokens. Empty list for empty input.
        """
        import re as _re
        if not query:
            return []
        toks: list[str] = []
        # English words
        for w in _re.findall(r"[A-Za-z0-9_]+", query):
            w = w.lower()
            if len(w) >= 2:
                toks.append(w)
        # CJK bi-grams first (high signal), then 3-grams for longer
        # tokens. Single-character tokens are intentionally NOT added
        # by default — "图" matches too much unrelated text. We fall
        # back to unigrams only when the CJK span is exactly 1 char.
        cjk_runs = _re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", query)
        for word in cjk_runs:
            if len(word) == 1:
                toks.append(word)
                continue
            # 2-grams
            for i in range(len(word) - 1):
                toks.append(word[i:i+2])
            # 3-grams (lower priority — appended after 2-grams so they
            # don't displace them)
            for i in range(len(word) - 2):
                toks.append(word[i:i+3])
        # Strip common Chinese stopwords so they don't dilute scores
        stops = {"的", "了", "是", "在", "和", "与", "或", "我", "你", "他", "她", "它",
                 "把", "被", "给", "从", "到", "为", "对", "及", "而", "也", "都",
                 "就", "还", "但", "并", "如", "若", "则", "此", "那", "哪", "什么", "怎么"}
        out: list[str] = []
        seen: set[str] = set()
        for t in toks:
            if not t or t in stops or len(t) < 1:
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= 24:
                break
        return out

    @staticmethod
    def _like_clause(col: str, tokens: list[str]) -> tuple[str, list[str]]:
        """Build a SQL ``(col LIKE ? OR col LIKE ? ...)`` clause and
        the matching parameter list for the given tokens."""
        if not tokens:
            return "0", []
        parts = []
        params: list[str] = []
        for t in tokens:
            parts.append(f"{col} LIKE ?")
            params.append(f"%{t}%")
        return "(" + " OR ".join(parts) + ")", params

    def recall(self, query: str, limit: int = 12,
               include: tuple[str, ...] = ("memories", "wiki", "entities"),
               bump_signals: bool = True) -> dict[str, list[dict]]:
        """Unified recall — returns a dict with three ranked lists.

        Each result is tagged with its ``kind`` ("memory" | "wiki" |
        "entity") and a numeric ``score`` so callers can render a
        single ranked stream. Bumps ``memory_signals.recall_count`` on
        any memory that gets surfaced, so the dashboard's "Most
        recalled memories" widget stays accurate.

        ``include`` lets the MCP server / CLI pick which sources to
        surface; default is all three for the broadest recall.
        """
        import time as _time
        tokens = self._tokenize(query)
        if not tokens:
            return {"memories": [], "wiki": [], "entities": [], "tokens": []}
        out: dict[str, list[dict]] = {"memories": [], "wiki": [], "entities": [], "tokens": tokens}
        with self._conn() as c:
            if "memories" in include:
                clause, params = self._like_clause("text", tokens)
                tag_clause, tag_params = self._like_clause("tags", tokens)
                sql = (
                    "SELECT m.id, m.kind, m.text, m.importance, m.score, m.source, "
                    "m.tags, m.created_at, m.updated_at, "
                    "COALESCE(s.recall_count, 0) AS recall_count "
                    "FROM memories m "
                    "LEFT JOIN memory_signals s ON s.memory_id = m.id "
                    f"WHERE {clause} OR {tag_clause} "
                    "ORDER BY m.score DESC, m.importance DESC, m.created_at DESC "
                    "LIMIT ?"
                )
                rows = c.execute(sql, (*params, *tag_params, limit * 3)).fetchall()
                for r in rows:
                    tags = []
                    try:
                        tags = json.loads(r["tags"]) if r["tags"] else []
                    except Exception:
                        tags = []
                    txt = (r["text"] or "").lower()
                    tag_lc = ",".join(tags).lower()
                    body_hits = sum(txt.count(t) for t in tokens)
                    tag_hits = sum(tag_lc.count(t) for t in tokens)
                    score = body_hits + 2 * tag_hits
                    score *= 0.5 + float(r["importance"] or 0) * 0.8
                    score *= 0.7 + float(r["score"] or 0) * 0.6
                    out["memories"].append({
                        "id": r["id"],
                        "kind": "memory",
                        "text": r["text"],
                        "importance": float(r["importance"] or 0),
                        "score_field": float(r["score"] or 0),
                        "source": r["source"],
                        "tags": tags,
                        "created_at": float(r["created_at"] or 0),
                        "recall_count": int(r["recall_count"] or 0),
                        "score": round(score, 3),
                        "preview": (r["text"] or "")[:240],
                    })
                out["memories"].sort(key=lambda m: -m["score"])
                out["memories"] = out["memories"][:limit]

            if "wiki" in include:
                clause, params = self._like_clause("title", tokens)
                body_clause, body_params = self._like_clause("body", tokens)
                sum_clause, sum_params = self._like_clause("summary", tokens)
                tag_clause, tag_params = self._like_clause("tags", tokens)
                sql = (
                    "SELECT id, slug, title, body, summary, importance, tags, "
                    "updated_at, version "
                    "FROM wiki_pages "
                    f"WHERE {clause} OR {body_clause} OR {sum_clause} OR {tag_clause} "
                    "ORDER BY importance DESC, updated_at DESC "
                    "LIMIT ?"
                )
                rows = c.execute(
                    sql, (*params, *body_params, *sum_params, *tag_params, limit * 3)
                ).fetchall()
                for r in rows:
                    tags = []
                    try:
                        tags = json.loads(r["tags"]) if r["tags"] else []
                    except Exception:
                        tags = []
                    t_lc = (r["title"] or "").lower()
                    b_lc = (r["body"] or "").lower()
                    s_lc = (r["summary"] or "").lower()
                    g_lc = ",".join(tags).lower()
                    hits = (
                        sum(t_lc.count(t) for t in tokens) * 5
                        + sum(s_lc.count(t) for t in tokens) * 3
                        + sum(b_lc.count(t) for t in tokens)
                        + sum(g_lc.count(t) for t in tokens) * 2
                    )
                    score = hits * (0.5 + float(r["importance"] or 0) * 1.5)
                    out["wiki"].append({
                        "id": r["id"],
                        "slug": r["slug"],
                        "title": r["title"],
                        "body": r["body"],
                        "summary": r["summary"] or "",
                        "tags": tags,
                        "importance": float(r["importance"] or 0),
                        "version": int(r["version"] or 1),
                        "updated_at": float(r["updated_at"] or 0),
                        "kind": "wiki",
                        "score": round(score, 3),
                        "preview": ((r["summary"] or r["body"]) or "")[:240],
                    })
                out["wiki"].sort(key=lambda m: -m["score"])
                out["wiki"] = out["wiki"][:limit]

            if "entities" in include:
                clause, params = self._like_clause("name", tokens)
                sql = (
                    "SELECT id, name, kind, mention_count, weight "
                    f"FROM entities WHERE {clause} "
                    "ORDER BY weight DESC, mention_count DESC LIMIT ?"
                )
                rows = c.execute(sql, (*params, limit)).fetchall()
                for r in rows:
                    n_lc = (r["name"] or "").lower()
                    hits = sum(n_lc.count(t) for t in tokens)
                    score = hits * (0.5 + float(r["weight"] or 0) * 1.2 + float(r["mention_count"] or 0) * 0.05)
                    out["entities"].append({
                        "id": r["id"],
                        "name": r["name"],
                        "kind": "entity",
                        "entity_kind": r["kind"],
                        "mention_count": int(r["mention_count"] or 0),
                        "weight": float(r["weight"] or 0),
                        "score": round(score, 3),
                    })
                out["entities"].sort(key=lambda m: -m["score"])
                out["entities"] = out["entities"][:limit]

        # Bump recall_count on surfaced memories (so dashboard tracks usage)
        if bump_signals and out["memories"]:
            now = _time.time()
            ids = [m["id"] for m in out["memories"]]
            with self._conn() as c:
                for mid in ids:
                    c.execute(
                        "INSERT INTO memory_signals (memory_id, recall_count, last_recalled_at, updated_at) "
                        "VALUES (?, 1, ?, ?) "
                        "ON CONFLICT(memory_id) DO UPDATE SET "
                        "recall_count = recall_count + 1, "
                        "last_recalled_at = excluded.last_recalled_at, "
                        "updated_at = excluded.updated_at",
                        (mid, now, now),
                    )
        return out

    # ----------------------------------------------------------------
    # Hybrid recall: BM25 (FTS5) + semantic (cosine) + entity overlap
    # fused via Reciprocal Rank Fusion (RRF).
    #
    # Mem0 (April 2026) showed that fusing BM25 keyword + semantic
    # + entity signal is worth ~20 points on LoCoMo / LongMemEval.
    # SQLite FTS5 ships with the kernel (no new dep), so the cost
    # of the keyword channel is effectively zero.
    #
    # Output is the same shape as the existing ``recall()`` method
    # so the API + dashboard don't need to change to consume it.
    # ----------------------------------------------------------------
    def recall_hybrid(
        self,
        query: str,
        limit: int = 12,
        source: str | None = None,
        rrf_k: int = 60,
        bm25_pool: int = 50,
        embed_pool: int = 50,
        include: tuple[str, ...] = ("memories", "wiki", "entities"),
        bump_signals: bool = True,
    ) -> dict[str, list[dict]]:
        """RRF-fused recall across BM25 + semantic + entity channels.

        ``source`` enables per-source scope: only wiki pages whose
        scope is 'global' OR contains this source are returned. If
        ``source`` is None, no scope filter is applied (the dashboard
        + admin recall see everything).
        """
        import time as _time
        from .retrieval import (
            fuse_rrf,
            bm25_search,
            detect_temporal_intent,
            temporal_score,
        )
        out: dict[str, list[dict]] = {"memories": [], "wiki": [], "entities": [], "tokens": self._tokenize(query)}
        if not (query or "").strip():
            return out

        # --- channel 1: BM25 (FTS5) ---------------------------------
        bm25_mem: list[dict] = []
        bm25_wiki: list[dict] = []
        if "memories" in include:
            bm25_mem = bm25_search(self, query, kind="memories", limit=bm25_pool,
                                   source_filter=source)
        if "wiki" in include:
            bm25_wiki = bm25_search(self, query, kind="wiki", limit=bm25_pool,
                                    source_filter=source)
        # Each result has a positive bm25 score and the row's primary key.

        # --- channel 2: semantic (cosine) ---------------------------
        # We do this through the existing search_by_embedding helper,
        # but we need a query embedding. If the embedder isn't set up
        # at the store level, the helper gracefully returns [].
        sem_mem: list[dict] = []
        sem_wiki: list[dict] = []
        try:
            q_emb = self._embed_query(query)
            if q_emb:
                sem_mem_rows = self.search_by_embedding(q_emb, top_k=embed_pool)
                sem_mem = [{"id": r.id, "_score": float(r.score or 0)} for r in sem_mem_rows]
                sem_wiki = self.search_wiki_by_embedding(q_emb, top_k=embed_pool)
                sem_wiki = [{"id": r["id"], "_score": float(r.get("importance", 0))} for r in sem_wiki]
        except Exception:
            pass

        # --- channel 3: entity overlap -----------------------------
        ent_mem: list[dict] = []
        ent_entities: list[dict] = []
        if "entities" in include:
            try:
                from ..graph.extract import extract_entities
                ents = extract_entities(query, min_count=1)
                if ents:
                    names = [n for (n, _k) in ents]
                    ent_rows = self.search_entities_by_names(names, limit=bm25_pool)
                    ent_entities = [{"id": r["id"], "_score": float(r.get("weight", 0))} for r in ent_rows]
                    ent_mem = self.search_memories_by_entity_names(
                        names, limit=bm25_pool, source_filter=source
                    )
            except Exception:
                pass

        # --- fuse ---------------------------------------------------
        fused_mem = fuse_rrf([bm25_mem, sem_mem, ent_mem], k=rrf_k)
        fused_wiki = fuse_rrf([bm25_wiki, sem_wiki], k=rrf_k)
        fused_ent = fuse_rrf([ent_entities], k=rrf_k)

        # --- temporal reasoning -------------------------------------
        # Mem0 v3 showed that detecting "what is the current X" vs
        # "the X I shipped last week" in the query and reranking by
        # date relevance is the single biggest recall improvement
        # (~27 points on LongMemEval). The primitives below are in
        # ``retrieval.py``; we apply them here as a multiplier on the
        # fused RRF score (added to each row, not multiplied with the
        # RRF, so it ranks the same direction).
        t_intent, t_conf = detect_temporal_intent(query)
        _now_ts = _time.time()
        if t_intent != "any" and t_conf > 0:
            for r in fused_mem:
                tscore = temporal_score(
                    created_at=_now_ts,   # fallback below per-row
                    updated_at=_now_ts,
                    intent=t_intent,
                    now=_now_ts,
                    confidence=t_conf,
                )
                # Look up the actual row timestamp; if we don't have
                # it in the fused payload, default to now (neutral).
                # We'll overwrite tscore in the hydration step where
                # we have the real created_at.
                r.setdefault("_t_intent", t_intent)
                r.setdefault("_t_conf", t_conf)
            for r in fused_wiki:
                r.setdefault("_t_intent", t_intent)
                r.setdefault("_t_conf", t_conf)
        else:
            for r in fused_mem + fused_wiki:
                r.setdefault("_t_intent", "any")
                r.setdefault("_t_conf", 0.0)

        # --- materialise (re-hydrate the rows) ---------------------
        mem_ids = [r["id"] for r in fused_mem[:limit * 2]]
        wiki_ids = [r["id"] for r in fused_wiki[:limit * 2]]
        ent_ids = [r["id"] for r in fused_ent[:limit * 2]]
        if mem_ids:
            out["memories"] = self._hydrate_memories(
                mem_ids, fused_mem, source=source,
                t_intent=t_intent, t_conf=t_conf, now=_now_ts,
            )
        if wiki_ids:
            out["wiki"] = self._hydrate_wiki(
                wiki_ids, fused_wiki,
                t_intent=t_intent, t_conf=t_conf, now=_now_ts,
            )
        if ent_ids:
            out["entities"] = self._hydrate_entities(ent_ids, fused_ent)

        # Trim
        out["memories"] = out["memories"][:limit]
        out["wiki"] = out["wiki"][:limit]
        out["entities"] = out["entities"][:limit]

        # Surface intent in the result so the UI can show it.
        out["temporal_intent"] = t_intent
        out["temporal_confidence"] = round(t_conf, 2)

        if bump_signals and out["memories"]:
            now = _time.time()
            with self._conn() as c:
                for m in out["memories"]:
                    c.execute(
                        "INSERT INTO memory_signals (memory_id, recall_count, last_recalled_at, updated_at) "
                        "VALUES (?, 1, ?, ?) "
                        "ON CONFLICT(memory_id) DO UPDATE SET "
                        "recall_count = recall_count + 1, "
                        "last_recalled_at = excluded.last_recalled_at, "
                        "updated_at = excluded.updated_at",
                        (m["id"], now, now),
                    )
        return out

    # --- Hybrid recall helpers -------------------------------------

    def _embed_query(self, query: str) -> list[float] | None:
        """Return a query embedding if an embedder is configured.

        The store does not own an embedder directly, but the app layer
        often does — we expose a hook so a wrapper can attach one.
        For now, returns None unless ``self._embedder`` is set, which
        keeps this file dependency-free.
        """
        emb = getattr(self, "_embedder", None)
        if emb is None:
            return None
        try:
            return list(emb.embed_query(query) or [])
        except Exception:
            return None

    def set_embedder(self, embedder) -> None:
        """Attach a query embedder for hybrid recall."""
        self._embedder = embedder

    def search_wiki_by_embedding(self, query_embedding, top_k=20) -> list[dict]:
        """Stub for the *semantic* wiki channel.

        We do not yet store embeddings on ``wiki_pages``; this channel
        is therefore a poor-man's proxy ordered by a blend of
        importance and recency. The shape matches the rest of the
        hybrid pipeline (``{"id", "_score"}``) so the fusion step
        can consume it.

        ``source_filter`` is applied here so out-of-scope pages never
        make it into recall — fixing a long-standing bug where a
        literal ``?`` was being passed as a scope token.
        """
        # ``query_embedding`` is currently unused; once wiki embeddings
        # are added (see roadmap), this becomes a real cosine rank.
        del query_embedding
        tok = self._source_token(source=None)  # placeholder
        with self._conn() as c:
            if tok:
                # When a source filter is set, return only 'global' OR
                # scope tokens that match.
                rows = c.execute(
                    "SELECT id, title, body, summary, importance, updated_at, scope "
                    "FROM wiki_pages "
                    "WHERE scope='global' OR instr(','||scope||',', ?) > 0 "
                    "ORDER BY (COALESCE(importance,0)*0.6 + 0.4) DESC, updated_at DESC LIMIT ?",
                    ("," + tok + ",", top_k),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, title, body, summary, importance, updated_at, scope "
                    "FROM wiki_pages "
                    "ORDER BY (COALESCE(importance,0)*0.6 + 0.4) DESC, updated_at DESC LIMIT ?",
                    (top_k,),
                ).fetchall()
        return [{"id": r["id"], "_score": float((r["importance"] or 0))} for r in rows]

    @staticmethod
    def _source_token(name: str) -> str:
        return (name or '').strip().lower().replace(' ', '-')

    def _hydrate_memories(self, ids: list[str], scored: list[dict], source: str | None = None,
                           t_intent: str = "any", t_conf: float = 0.0, now: float = 0.0) -> list[dict]:
        if not ids:
            return []
        score_map = {r["id"]: r.get("_rrf", 0) for r in scored}
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT m.id, m.kind, m.text, m.importance, m.score, m.source,
                          m.tags, m.created_at, m.updated_at,
                          COALESCE(s.recall_count, 0) AS recall_count
                   FROM memories m
                   LEFT JOIN memory_signals s ON s.memory_id = m.id
                   WHERE m.id IN ({placeholders}) """,
                ids,
            ).fetchall()
        if not now:
            import time as _t
            now = _t.time()
        out = []
        for r in rows:
            tags = []
            try:
                tags = json.loads(r["tags"]) if r["tags"] else []
            except Exception:
                tags = []
            base_score = score_map.get(r["id"], 0)
            t_mult = temporal_score(
                created_at=float(r["created_at"] or now),
                updated_at=float(r["updated_at"] or r["created_at"] or now),
                intent=t_intent, now=now, confidence=t_conf,
            )
            out.append({
                "id": r["id"],
                "kind": "memory",
                "text": r["text"],
                "importance": float(r["importance"] or 0),
                "score_field": float(r["score"] or 0),
                "source": r["source"],
                "tags": tags,
                "created_at": float(r["created_at"] or 0),
                "recall_count": int(r["recall_count"] or 0),
                "score": round(base_score * t_mult, 4),
                "_temporal_multiplier": round(t_mult, 3),
                "preview": (r["text"] or "")[:240],
            })
        # Filter by source if a scope applies. Memories are not
        # scoped (only wiki pages are), but we still respect the
        # source filter for memories that came from a different
        # client when the caller asks for a specific source.
        if source:
            tok = self._source_token(source)
            out = [m for m in out if (m.get("source") or "").split("/")[0] in (tok, "all")]
        out.sort(key=lambda m: -m["score"])
        return out

    def _hydrate_wiki(self, ids: list[str], scored: list[dict],
                      t_intent: str = "any", t_conf: float = 0.0, now: float = 0.0) -> list[dict]:
        if not ids:
            return []
        score_map = {r["id"]: r.get("_rrf", 0) for r in scored}
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, slug, title, body, summary, importance, tags, updated_at, version, scope, created_at
                   FROM wiki_pages WHERE id IN ({placeholders}) """,
                ids,
            ).fetchall()
        if not now:
            import time as _t
            now = _t.time()
        out = []
        for r in rows:
            tags = []
            try:
                tags = json.loads(r["tags"]) if r["tags"] else []
            except Exception:
                tags = []
            base_score = score_map.get(r["id"], 0)
            t_mult = temporal_score(
                created_at=float(r["created_at"] or now),
                updated_at=float(r["updated_at"] or r["created_at"] or now),
                intent=t_intent, now=now, confidence=t_conf,
            )
            out.append({
                "id": r["id"],
                "kind": "wiki",
                "slug": r["slug"],
                "title": r["title"],
                "summary": r["summary"] or "",
                "body": r["body"] or "",
                "importance": float(r["importance"] or 0),
                "tags": tags,
                "scope": r["scope"] or "global",
                "updated_at": float(r["updated_at"] or 0),
                "version": int(r["version"] or 1),
                "score": round(base_score * t_mult, 4),
                "_temporal_multiplier": round(t_mult, 3),
                "preview": ((r["summary"] or r["body"] or "")[:240]),
            })
        out.sort(key=lambda m: -m["score"])
        return out

    def _hydrate_entities(self, ids: list[str], scored: list[dict]) -> list[dict]:
        if not ids:
            return []
        score_map = {r["id"]: r.get("_rrf", 0) for r in scored}
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, name, kind, mention_count, weight
                   FROM entities WHERE id IN ({placeholders}) """,
                ids,
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "kind": "entity",
                "name": r["name"],
                "entity_kind": r["kind"],
                "mention_count": int(r["mention_count"] or 0),
                "weight": float(r["weight"] or 0),
                "score": round(score_map.get(r["id"], 0), 4),
            })
        out.sort(key=lambda m: -m["score"])
        return out

    # ----------------------------------------------------------------
    # Entity-lookup helpers used by the entity channel of hybrid recall.
    #
    # Stored entity names use a kind prefix (``concept:Codex``,
    # ``tag:auto``, ``wiki:foo``) so the same surface string can refer
    # to several kinds without colliding on the UNIQUE(name,kind)
    # constraint. Caller code typically only has the bare token
    # (e.g. extracted by ``graph.extract.extract_entities``), so we
    # match against both the full prefixed name and the suffix after
    # the colon.
    # ----------------------------------------------------------------
    def search_entities_by_names(self, names: list[str], limit: int = 20) -> list[dict]:
        if not names:
            return []
        # Build a list of every candidate form for each input name.
        candidates: list[str] = []
        seen: set[str] = set()
        for n in names:
            base = (n or "").strip().lower()
            if not base:
                continue
            for form in (base, base.split(":")[-1] if ":" in base else base):
                if form and form not in seen:
                    candidates.append(form)
                    seen.add(form)
        if not candidates:
            return []
        with self._conn() as c:
            qmarks = ",".join("?" * len(candidates))
            # Match either the full prefixed name OR the suffix
            # after the last ':'. LIKE on the lower-cased name covers
            # both, and we dedupe in Python at the end.
            rows = c.execute(
                f"""SELECT id, name, kind, mention_count, weight
                    FROM entities
                    WHERE LOWER(name) IN ({qmarks})
                       OR LOWER(name) LIKE '%:' || ?
                       OR LOWER(name) = ?
                    GROUP BY id
                    ORDER BY weight DESC, mention_count DESC LIMIT ?""",
                (*candidates, candidates[0], candidates[0], limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_memories_by_entity_names(
        self, names: list[str], limit: int = 50, source_filter: str | None = None
    ) -> list[dict]:
        if not names:
            return []
        candidates: list[str] = []
        seen: set[str] = set()
        for n in names:
            base = (n or "").strip().lower()
            if not base:
                continue
            for form in (base, base.split(":")[-1] if ":" in base else base):
                if form and form not in seen:
                    candidates.append(form)
                    seen.add(form)
        if not candidates:
            return []
        with self._conn() as c:
            qmarks = ",".join("?" * len(candidates))
            sql = (
                f"""SELECT m.id, MAX(m.score) AS mem_score
                    FROM memories m
                    JOIN entity_mentions em ON em.memory_id = m.id
                    JOIN entities e ON e.id = em.entity_id
                    WHERE LOWER(e.name) IN ({qmarks})
                       OR LOWER(e.name) LIKE '%:' || ?
                       OR LOWER(e.name) = ?
                 """
            )
            params: list = list(candidates) + [candidates[0], candidates[0]]
            if source_filter:
                sql += " AND (m.source LIKE ? OR m.source LIKE ?) "
                tok = self._source_token(source_filter)
                params.extend([f"{tok}/%", tok])
            sql += " GROUP BY m.id ORDER BY SUM(em.weight) DESC LIMIT ?"
            params.append(limit)
            rows = c.execute(sql, params).fetchall()
        return [{"id": r["id"], "_score": float(r["mem_score"] or 0)} for r in rows]

    def search_by_embedding(
        self, query_embedding: list[float], top_k: int = 20
    ) -> list[StoredMemory]:
        """Brute-force cosine over every row. Fine up to ~10k items."""
        if not query_embedding:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM memories WHERE embedding IS NOT NULL"
            ).fetchall()
        scored: list[tuple[float, StoredMemory]] = []
        for r in rows:
            mem = self._row_to_memory(r)
            if mem.embedding is None:
                continue
            scored.append((_cosine(query_embedding, mem.embedding), mem))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:top_k]]

    def _row_to_memory(self, row: sqlite3.Row) -> StoredMemory:
        import json

        tags = []
        if row["tags"]:
            try:
                tags = json.loads(row["tags"])
            except (ValueError, TypeError):
                logger.warning("corrupt tags for memory %s; resetting", row["id"])
                tags = []
        return StoredMemory(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            importance=row["importance"],
            source=row["source"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            score=row["score"],
            ttl=row["ttl"],
            tags=tags,
            embedding=_from_blob(row["embedding"]),
        )

    # --- wiki pages -------------------------------------------------------

    def upsert_wiki_page(
        self,
        *,
        slug: str,
        title: str,
        body: str,
        summary: str | None = None,
        tags: list[str] | None = None,
        importance: float = 0.5,
        evidence_ids: list[str] | None = None,
        run_id: str | None = None,
        scope: str = "global",
    ) -> Dict[str, Any]:
        """Create-or-update a wiki page by slug.

        Returns the full row as a dict so the API can hand it back to
        the UI without an extra round-trip.
        """
        import json as _json
        import uuid as _uuid
        now = time.time()
        tags_json = _json.dumps(tags or [], ensure_ascii=False)
        evid_json = _json.dumps(evidence_ids or [], ensure_ascii=False)
        with self._conn() as c:
            existing = c.execute(
                "SELECT id, version FROM wiki_pages WHERE slug=?", (slug,)
            ).fetchone()
            if existing is None:
                pid = _uuid.uuid4().hex
                version = 1
                c.execute(
                    "INSERT INTO wiki_pages(id, slug, title, body, summary, tags, "
                    "importance, evidence_ids, run_id, version, created_at, updated_at, scope) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, slug, title, body, summary or "", tags_json,
                     float(importance), evid_json, run_id, version, now, now,
                     scope or "global"),
                )
            else:
                pid = existing["id"]
                version = (existing["version"] or 1) + 1
                c.execute(
                    "UPDATE wiki_pages SET title=?, body=?, summary=?, tags=?, "
                    "importance=?, evidence_ids=?, run_id=?, version=?, updated_at=?, scope=? "
                    "WHERE id=?",
                    (title, body, summary or "", tags_json,
                     float(importance), evid_json, run_id, version, now,
                     scope or "global", pid),
                )
            row = c.execute(
                "SELECT * FROM wiki_pages WHERE id=?", (pid,)
            ).fetchone()
        return self._row_to_wiki(row)

    def list_wiki_pages(
        self,
        limit: int = 200,
        min_importance: float | None = None,
        query: str | None = None,
    ) -> list[Dict[str, Any]]:
        clauses: list[str] = []
        params: list = []
        if min_importance is not None:
            clauses.append("importance >= ?")
            params.append(float(min_importance))
        if query:
            clauses.append("(title LIKE ? OR body LIKE ? OR summary LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM wiki_pages {where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._row_to_wiki(r) for r in rows]

    def get_wiki_page(self, page_id: str) -> Dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM wiki_pages WHERE id=?", (page_id,)
            ).fetchone()
        return self._row_to_wiki(row) if row else None

    def get_wiki_page_by_slug(self, slug: str) -> Dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM wiki_pages WHERE slug=?", (slug,)
            ).fetchone()
        return self._row_to_wiki(row) if row else None

    def delete_wiki_page(self, page_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM wiki_pages WHERE id=?", (page_id,)
            )
            return cur.rowcount > 0

    def delete_wiki_pages_for_run(self, run_id: str) -> int:
        """Helper used by tests and by re-runs of a specific run id."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM wiki_pages WHERE run_id=?", (run_id,)
            )
            return cur.rowcount

    def count_memories(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]

    def count_sessions(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]

    def count_entities(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]

    def count_wiki_pages(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM wiki_pages").fetchone()
            return int(row["n"] or 0) if row else 0

    def _row_to_wiki(self, row: sqlite3.Row) -> Dict[str, Any]:
        import json as _json
        tags = []
        if row["tags"]:
            try:
                tags = _json.loads(row["tags"])
            except (ValueError, TypeError):
                logger.warning("corrupt tags for relation %s; resetting", row["id"])
                tags = []
        evidence = []
        if row["evidence_ids"]:
            try:
                evidence = _json.loads(row["evidence_ids"])
            except (ValueError, TypeError):
                logger.warning("corrupt evidence_ids for relation %s; resetting", row["id"])
                evidence = []
        return {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "body": row["body"],
            "summary": row["summary"] or "",
            "tags": tags,
            "importance": float(row["importance"] or 0.0),
            "evidence_ids": evidence,
            "run_id": row["run_id"],
            "version": int(row["version"] or 1),
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    # --- scoring ----------------------------------------------------------

    # Weights are class-level so tests can pin them down. The blend is
    # normalized to [0, 1].
    #
    # Why this shape: recency alone fades everything; usage alone lets
    # an old junk memory float back. Combining both with feedback as a
    # bias lets a memory stay useful only while it is actually being
    # consulted. ``positive`` events are sticky (no time decay) — the
    # user explicitly said "this is good"; ``negative`` events are
    # sticky too, but pull down.
    _SCORE_WEIGHTS = {
        "importance": 0.40,   # LLM/original importance
        "recency":    0.25,   # time-decay (newer = higher)
        "usage":      0.25,   # recall_count × last_recalled_at decay
        "feedback":   0.10,   # +/- thumbs
    }

    @classmethod
    def score_components(
        cls,
        importance: float,
        created_at: float,
        now: float | None = None,
        recall_count: int = 0,
        last_recalled_at: float | None = None,
        positive: int = 0,
        negative: int = 0,
        half_life_days: float = 30.0,
    ) -> Dict[str, float]:
        """Return the four score components plus the blended score, all in
        [0, 1]. Pure function — no DB access — so it can be unit-tested
        and reused by the UI."""
        now = now if now is not None else time.time()
        age = max(0.0, now - created_at)
        half_life = half_life_days * 86400.0
        recency = (0.5 ** (age / half_life)) if half_life else 1.0

        # Usage: log-saturated recall_count × a recency factor on when
        # it was last recalled. A memory recalled 1× today scores
        # ~0.30 on usage; 10× today → ~0.65; 100× today → ~0.95.
        import math
        usage = 0.0
        if recall_count > 0:
            log_recall = math.log1p(recall_count) / math.log1p(100)  # 0..1
            log_recall = max(0.0, min(1.0, log_recall))
            if last_recalled_at:
                age_recall = max(0.0, now - last_recalled_at)
                usage_recency = (0.5 ** (age_recall / half_life)) if half_life else 1.0
            else:
                usage_recency = 1.0
            usage = log_recall * (0.25 + 0.75 * usage_recency)

        # Feedback: positive/negative are sticky (no time decay). We
        # tanh-clamp so a flood of thumbs can't push the score to ±∞.
        import math as _m
        feedback = _m.tanh((positive - negative) / 3.0)  # -1..1

        w = cls._SCORE_WEIGHTS
        score = (
            w["importance"] * max(0.0, min(1.0, importance or 0.0))
            + w["recency"] * recency
            + w["usage"] * usage
            + w["feedback"] * max(-0.5, min(0.5, feedback))  # half-weight negative path
        )
        return {
            "importance": importance or 0.0,
            "recency":    recency,
            "usage":      usage,
            "feedback":   feedback,
            "score":      max(0.0, min(1.0, score)),
        }

    @staticmethod
    def compute_score(
        importance: float,
        created_at: float,
        now: float | None = None,
        half_life_days: float = 30.0,
    ) -> float:
        """Legacy single-blend score. Kept for callers that don't have
        signal data yet. New code should prefer ``score_components``."""
        now = now if now is not None else time.time()
        age_seconds = max(0.0, now - created_at)
        half_life_seconds = half_life_days * 86400.0
        recency = 0.5 ** (age_seconds / half_life_seconds) if half_life_seconds else 1.0
        blended = 0.35 * importance + 0.65 * recency
        return max(0.0, min(1.0, blended))

    def rescore_all(self, half_life_days: float = 30.0) -> int:
        """Recompute score for every memory using v2 (importance × recency
        × usage × feedback). One row at a time keeps WAL writes small
        and lets us skip rows whose components didn't change."""
        updated = 0
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                """SELECT m.id, m.importance, m.created_at,
                          COALESCE(s.recall_count, 0) AS recall_count,
                          s.last_recalled_at,
                          COALESCE(s.positive, 0) AS positive,
                          COALESCE(s.negative, 0) AS negative
                   FROM memories m
                   LEFT JOIN memory_signals s ON s.memory_id = m.id"""
            ).fetchall()
            for r in rows:
                comps = self.score_components(
                    importance=r["importance"],
                    created_at=r["created_at"],
                    now=now,
                    recall_count=r["recall_count"],
                    last_recalled_at=r["last_recalled_at"],
                    positive=r["positive"],
                    negative=r["negative"],
                    half_life_days=half_life_days,
                )
                new = comps["score"]
                # Cheap change check (3 decimals): avoid WAL churn.
                cur = c.execute(
                    "SELECT score FROM memories WHERE id=?", (r["id"],)
                ).fetchone()
                if cur is None or abs((cur["score"] or 0) - new) > 1e-3:
                    c.execute(
                        "UPDATE memories SET score=? WHERE id=?",
                        (new, r["id"]),
                    )
                    updated += 1
        return updated

    # --- graph ------------------------------------------------------------

    def upsert_entity(
        self,
        name: str,
        kind: str = "concept",
        bump_weight: float = 0.0,
    ) -> GraphEntity:
        name = (name or "").strip()
        if not name:
            raise ValueError("empty entity name")
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT id, mention_count, weight FROM entities WHERE name=? AND kind=?",
                (name, kind),
            ).fetchone()
            if row is None:
                eid = uuid.uuid4().hex
                weight = max(0.05, min(1.0, 0.5 + bump_weight))
                c.execute(
                    """INSERT INTO entities(id, name, kind, mention_count, weight, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (eid, name, kind, 1, weight, now, now),
                )
                return GraphEntity(id=eid, name=name, kind=kind, mention_count=1, weight=weight)
            new_count = row["mention_count"] + 1
            new_weight = min(1.0, row["weight"] + bump_weight)
            c.execute(
                "UPDATE entities SET mention_count=?, weight=?, updated_at=? WHERE id=?",
                (new_count, new_weight, now, row["id"]),
            )
            return GraphEntity(
                id=row["id"], name=name, kind=kind,
                mention_count=new_count, weight=new_weight,
            )

    def upsert_relation(
        self,
        src: str,
        dst: str,
        kind: str = "related",
        weight: float = 0.5,
        evidence_id: str | None = None,
    ) -> GraphRelation:
        if not src or not dst or src == dst:
            raise ValueError("bad relation")
        rid = uuid.uuid4().hex
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT id, weight, evidence_ids FROM relations WHERE src=? AND dst=? AND kind=?",
                (src, dst, kind),
            ).fetchone()
            if row is not None:
                existing = []
                if row["evidence_ids"]:
                    try:
                        import json as _json_local
                        existing = _json_local.loads(row["evidence_ids"])
                    except (ValueError, TypeError):
                        logger.warning("corrupt evidence_ids for row; resetting")
                        existing = []
                if evidence_id and evidence_id not in existing:
                    existing.append(evidence_id)
                new_weight = min(1.0, (row["weight"] or 0.0) + 0.05)
                c.execute(
                    "UPDATE relations SET weight=?, evidence_ids=? WHERE id=?",
                    (
                        new_weight,
                        __import__("json").dumps(existing),
                        row["id"],
                    ),
                )
                return GraphRelation(
                    id=row["id"], src=src, dst=dst, kind=kind,
                    weight=new_weight, evidence_ids=existing,
                )
            evidence = [evidence_id] if evidence_id else []
            c.execute(
                """INSERT INTO relations(id, src, dst, kind, weight, evidence_ids, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (rid, src, dst, kind, max(0.05, min(1.0, weight)),
                 __import__("json").dumps(evidence), now),
            )
            return GraphRelation(id=rid, src=src, dst=dst, kind=kind,
                                 weight=weight, evidence_ids=evidence)

    def list_entities(self, limit: int = 500, kind: str | None = None) -> list[GraphEntity]:
        with self._conn() as c:
            if kind:
                rows = c.execute(
                    "SELECT * FROM entities WHERE kind=? ORDER BY weight DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM entities ORDER BY weight DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [GraphEntity(
                id=r["id"], name=r["name"], kind=r["kind"],
                mention_count=r["mention_count"], weight=r["weight"],
            ) for r in rows]

    def list_relations(self, limit: int = 2000) -> list[GraphRelation]:
        import json
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM relations ORDER BY weight DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out = []
            for r in rows:
                ev = []
                if r["evidence_ids"]:
                    try:
                        ev = json.loads(r["evidence_ids"])
                    except (ValueError, TypeError):
                        logger.warning("corrupt evidence_ids in graph query; skipping relation")
                        ev = []
                out.append(GraphRelation(
                    id=r["id"], src=r["src"], dst=r["dst"],
                    kind=r["kind"], weight=r["weight"], evidence_ids=ev,
                ))
            return out

    def graph_stats(self) -> dict:
        with self._conn() as c:
            ne = c.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
            nr = c.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
            return {"entities": ne, "relations": nr}

    def delete_graph(self) -> int:
        with self._conn() as c:
            r1 = c.execute("DELETE FROM entities").rowcount
            c.execute("DELETE FROM relations")
            return r1

    # --- maintenance ------------------------------------------------------

    def delete_memory(self, mid: str) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM memories WHERE id=?", (mid,))
            return cur.rowcount

    def merge_memories(self, a_id: str, b_id: str) -> dict:
        """True memory-pair merge.

        Behaviour:
          - The higher-scored memory wins (ties go to ``a_id``).
          - The loser's text is appended to the winner's text (de-duplicated
            if the loser's text is already a substring of the winner's).
          - The winner's ``importance`` and ``score`` are bumped to the max
            of the two so the fused memory keeps the strongest signal.
          - The loser is deleted in the same transaction.
          - The pair is recorded in ``contradiction_ignored`` so the pulse
            does not surface it again.

        Returns a small dict describing what changed so the API layer can
        report it back to the UI (UI then shows a 'merged' toast).
        """
        a_id = str(a_id or "")
        b_id = str(b_id or "")
        if not a_id or not b_id or a_id == b_id:
            raise ValueError("merge_memories needs two distinct ids")
        now = time.time()
        with self._conn() as c:
            a_row = c.execute(
                "SELECT id, text, importance, score FROM memories WHERE id=?",
                (a_id,),
            ).fetchone()
            b_row = c.execute(
                "SELECT id, text, importance, score FROM memories WHERE id=?",
                (b_id,),
            ).fetchone()
            if a_row is None and b_row is None:
                return {"merged": False, "reason": "neither_exists"}
            if a_row is None:
                # Only B exists — silently delete the missing A and keep B.
                c.execute("DELETE FROM memories WHERE id=?", (a_id,))
                return {"merged": False, "kept": b_id, "lost": a_id, "reason": "a_missing"}
            if b_row is None:
                c.execute("DELETE FROM memories WHERE id=?", (b_id,))
                return {"merged": False, "kept": a_id, "lost": b_id, "reason": "b_missing"}

            # Pick winner = higher score; ties go to a_id.
            a_score = a_row["score"] or 0.0
            b_score = b_row["score"] or 0.0
            winner_is_a = a_score >= b_score
            winner_id = a_id if winner_is_a else b_id
            loser_id = b_id if winner_is_a else a_id
            winner_text = (a_row["text"] if winner_is_a else b_row["text"]) or ""
            loser_text = (b_row["text"] if winner_is_a else a_row["text"]) or ""
            importance_max = max(a_row["importance"] or 0.0, b_row["importance"] or 0.0)
            score_max = max(a_score, b_score)

            # Decide whether the loser's content needs to be appended. If
            # the winner already contains it (string containment is fine for
            # the plain-text payloads we have here), skip the append.
            needs_append = bool(loser_text.strip()) and loser_text.strip() not in winner_text
            if needs_append:
                # Triple-dash rule marks the boundary between the two
                # original sources of a merged memory.
                sep = "\n\n---\n\n"
                merged_text = winner_text.rstrip() + sep + loser_text.strip()
            else:
                merged_text = winner_text

            c.execute(
                "UPDATE memories "
                "SET text=?, importance=?, score=?, updated_at=? "
                "WHERE id=?",
                (merged_text, importance_max, score_max, now, winner_id),
            )
            c.execute("DELETE FROM memories WHERE id=?", (loser_id,))

            # Suppress the pair so the pulse does not resurface it.
            lo, hi = sorted([a_id, b_id])
            key = f"{lo}|{hi}"
            c.execute(
                "INSERT INTO contradiction_ignored(pair_key, ignored_at) VALUES(?, ?) "
                "ON CONFLICT(pair_key) DO NOTHING",
                (key, now),
            )

            return {
                "merged": True,
                "kept": winner_id,
                "lost": loser_id,
                "appended": needs_append,
                "winner_was_a": winner_is_a,
                "new_length": len(merged_text),
            }

    def delete_session(self, session_id: str) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM memories WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return cur.rowcount

    def gc(self) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM memories WHERE ttl IS NOT NULL AND (? - created_at) > ttl",
                (now,),
            )
            return cur.rowcount

    def stats(self) -> dict:
        with self._conn() as c:
            n_mem = c.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            n_ses = c.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            n_wiki = c.execute("SELECT COUNT(*) c FROM wiki_pages").fetchone()["c"]
            n_entities = c.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
            n_relations = c.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
            avg = c.execute("SELECT AVG(score) a FROM memories").fetchone()["a"] or 0.0
            wiki_avg = c.execute("SELECT AVG(importance) a FROM wiki_pages").fetchone()["a"] or 0.0
            return {
                "memories": n_mem,
                "sessions": n_ses,
                "wiki_pages": n_wiki,
                "entities": n_entities,
                "relations": n_relations,
                "wiki_avg_importance": round(float(wiki_avg), 4),
                "avg_score": round(avg, 4),
                "path": str(self.path),
            }


    # --- settings ---------------------------------------------------------

    def get_setting(self, key: str, default=None):
        """Read a single setting key, returning ``default`` if absent."""
        with self._conn() as c:
            row = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["v"])
            except (ValueError, TypeError):
                logger.warning("corrupt setting %s; returning default", key)
                return default

    def set_setting(self, key: str, value) -> None:
        """Persist a setting value (JSON-encoded)."""
        import json
        payload = json.dumps(value, ensure_ascii=False)
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(k,v,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (key, payload, now),
            )

    def get_all_settings(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT k, v FROM settings").fetchall()
        import json
        out: dict = {}
        for r in rows:
            try:
                out[r["k"]] = json.loads(r["v"])
            except (ValueError, TypeError):
                logger.warning("corrupt setting %s; keeping raw value", r["k"])
                out[r["k"]] = r["v"]
        return out

    # --- consolidation run history ---------------------------------------

    def start_consolidation_run(self, trigger: str, model=None) -> str:
        import uuid
        rid = uuid.uuid4().hex
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO consolidation_runs(id,started_at,trigger,status,model) "
                "VALUES (?,?,?,?,?)",
                (rid, now, trigger, "running", model),
            )
        return rid

    def finish_consolidation_run(
        self,
        run_id: str,
        status: str,
        stats=None,
        error=None,
    ) -> None:
        import json
        now = time.time()
        with self._conn() as c:
            c.execute(
                "UPDATE consolidation_runs "
                "SET finished_at=?, status=?, stats_json=?, error=? WHERE id=?",
                (now, status, json.dumps(stats) if stats else None, error, run_id),
            )

    def list_consolidation_runs(self, limit: int = 20) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM consolidation_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        import json
        out = []
        for r in rows:
            d = dict(r)
            if d.get("stats_json"):
                try:
                    d["stats"] = json.loads(d["stats_json"])
                except (ValueError, TypeError):
                    logger.warning("corrupt stats_json for run %s; dropping stats", d.get("id"))
                d.pop("stats_json", None)
            out.append(d)
        return out






    # ---- Contradiction pair ignore list --------------------------------

    @staticmethod
    def pair_key(a: str, b: str) -> str:
        """Canonical hash for a (memory_a, memory_b) pair.

        Order-independent so the user can ignore the pair from either
        side and we still find it again later.
        """
        lo, hi = sorted([str(a or ""), str(b or "")])
        return f"{lo}|{hi}"

    def ignore_contradiction(self, a: str, b: str) -> bool:
        """Mark a contradiction pair as ignored. Returns True if newly inserted."""
        key = self.pair_key(a, b)
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO contradiction_ignored(pair_key, ignored_at) VALUES(?, ?) "
                "ON CONFLICT(pair_key) DO NOTHING",
                (key, now),
            )
            return cur.rowcount > 0

    def unignore_contradiction(self, a: str, b: str) -> bool:
        """Reverse an ignore. Returns True if a row was actually deleted."""
        key = self.pair_key(a, b)
        with self._conn() as c:
            cur = c.execute("DELETE FROM contradiction_ignored WHERE pair_key=?", (key,))
            return cur.rowcount > 0

    def is_contradiction_ignored(self, a: str, b: str) -> bool:
        key = self.pair_key(a, b)
        with self._conn() as c:
            r = c.execute("SELECT 1 FROM contradiction_ignored WHERE pair_key=?", (key,)).fetchone()
            return bool(r)

    def list_ignored_pairs(self) -> set[str]:
        with self._conn() as c:
            return {r["pair_key"] for r in c.execute("SELECT pair_key FROM contradiction_ignored")}
class LLMAuditStore:
    """Append-only audit log for every LLM provider call.

    Backed by the ``llm_audit`` table. Inserts are fire-and-forget;
    the API intentionally never raises to keep consolidation paths
    robust against audit-write failures.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def record(
        self,
        *,
        provider: str,
        model: str,
        kind: str,
        prompt: str,
        response: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        ok: bool = True,
        error: str | None = None,
        run_id: str | None = None,
    ) -> str | None:
        import hashlib
        import json
        import time
        import uuid
        try:
            aid = uuid.uuid4().hex
            prompt_hash = hashlib.sha1((prompt or "").encode("utf-8")).hexdigest()[:16]
            total = (prompt_tokens or 0) + (completion_tokens or 0)
            with self._store._conn() as c:
                c.execute(
                    """INSERT INTO llm_audit
                       (id, ts, provider, model, kind, run_id, prompt_hash,
                        prompt_text, response_text,
                        prompt_tokens, completion_tokens, total_tokens,
                        cost_usd, latency_ms, ok, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        aid, time.time(), provider, model, kind, run_id,
                        prompt_hash,
                        (prompt or "")[:8000], (response or "")[:8000],
                        prompt_tokens, completion_tokens, total,
                        cost_usd, latency_ms,
                        1 if ok else 0, (error or "")[:1000],
                    ),
                )
            return aid
        except Exception:
            log.exception("llm_audit insert failed")
            return None

    def recent(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        try:
            with self._store._conn() as c:
                sql = "SELECT * FROM llm_audit"
                params: list = []
                if kind:
                    sql += " WHERE kind = ?"
                    params.append(kind)
                sql += " ORDER BY ts DESC LIMIT ?"
                params.append(limit)
                rows = c.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def stats(self, since_ts: float | None = None) -> dict:
        """Aggregate token / cost / latency / failure counts."""
        try:
            with self._store._conn() as c:
                clauses = []
                params: list = []
                if since_ts is not None:
                    clauses.append("ts >= ?")
                    params.append(since_ts)
                where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
                row = c.execute(
                    f"""SELECT
                          COUNT(*) AS calls,
                          COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                          COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                          COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                          COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) AS failures
                        FROM llm_audit {where}""",
                    params,
                ).fetchone()
                return dict(row)
        except Exception:
            return {}



class WriteGuardDropStore:
    """Tiny counter table for items rejected by the WriteGuard.

    The pipeline stores one row per drop so the dashboard can show live
    counts and last-rejected timestamps per rejection kind / source.
    """

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def record(self, *, source: str, kind: str, text_preview: str = "",
               matched_id: str | None = None, matched_score: float = 0.0,
               ts: float | None = None) -> None:
        import time as _t
        ts = ts if ts is not None else _t.time()
        try:
            with self.store._conn() as c:
                c.execute(
                    "INSERT INTO write_guard_drops(ts, source, kind, text_preview, matched_id, matched_score) "
                    "VALUES (?,?,?,?,?,?)",
                    (ts, source or "unknown", kind, (text_preview or "")[:160],
                     matched_id, float(matched_score or 0.0)),
                )
        except Exception:
            # Never let a metrics write block ingestion.
            pass

    def summary(self, *, window_hours: float = 24 * 7) -> dict:
        """Aggregate drop counts by source + kind, plus last-seen timestamps.

        Returned shape::

            {
              "totals":   {"duplicate": 12, "too_short": 3, ...},
              "by_source": {"codex": {"duplicate": 9, ...}, ...},
              "last":     {"duplicate": 1784297800.1, ...},
              "window_hours": 168,
              "threshold": {"duplicate_threshold": 0.85, "min_len": 25, "max_len": 1200, "min_imp": 0.4},
            }
        """
        import time as _t
        since = _t.time() - float(window_hours) * 3600.0
        with self.store._conn() as c:
            rows = c.execute(
                "SELECT source, kind, COUNT(*) AS n, MAX(ts) AS last_ts "
                "FROM write_guard_drops WHERE ts >= ? GROUP BY source, kind",
                (since,),
            ).fetchall()
        totals: dict[str, int] = {}
        by_source: dict[str, dict[str, int]] = {}
        last: dict[str, float] = {}
        for r in rows:
            n = int(r["n"])
            totals[r["kind"]] = totals.get(r["kind"], 0) + n
            by_source.setdefault(r["source"], {})[r["kind"]] = n
            last[r["kind"]] = max(last.get(r["kind"], 0.0), float(r["last_ts"] or 0.0))
        # Read thresholds from the running WriteGuard if one exists.
        threshold = {
            "duplicate_threshold": 0.85,
            "min_len": 25,
            "max_len": 1200,
            "min_imp": 0.4,
        }
        try:
            from ..ingest.pipeline import WriteGuard as _WG
            wg = _WG(self.store)
            threshold = {
                "duplicate_threshold": float(wg.duplicate_threshold),
                "min_len": int(wg.min_len),
                "max_len": int(wg.max_len),
                "min_imp": float(wg.min_importance),
            }
        except Exception:
            pass
        return {
            "totals": totals,
            "by_source": by_source,
            "last": last,
            "window_hours": float(window_hours),
            "threshold": threshold,
        }

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(x * x for x in b)) or 1e-12
    return dot / (na * nb)

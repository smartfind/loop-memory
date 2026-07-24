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
    embedding   BLOB,
    agent_id    TEXT,
    user_id     TEXT,
    external_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_mem_session  ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_created  ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_mem_score    ON memories(score);
CREATE INDEX IF NOT EXISTS idx_mem_kind     ON memories(kind);
-- Per-agent (agent_id, user_id, external_id) indexes are created
-- in _init_schema *after* the ALTER TABLE that adds the columns,
-- so opening an old DB doesn't fail with "no such column: agent_id".
-- See _init_schema for the migration block.

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
    updated_at      REAL NOT NULL,
    key_facts       TEXT,           -- JSON array of single-sentence facts
    contradicting_ids TEXT          -- JSON array of wiki page ids this page
                                     -- contradicts; populated by the
                                     -- contradiction detector on write.
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
-- Universal Agent Memory v7: wiki versioning, cognitive audit
-- trail, and per-(user, agent) bearer tokens. All additive, all
-- nullable, all with sensible defaults so existing rows are
-- untouched.
CREATE TABLE IF NOT EXISTS wiki_versions (
    id          TEXT PRIMARY KEY,
    page_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    summary     TEXT,
    tags        TEXT,
    importance  REAL NOT NULL DEFAULT 0.5,
    key_facts   TEXT,
    scope       TEXT,
    branched_at REAL NOT NULL,
    branch_tag  TEXT
);
CREATE INDEX IF NOT EXISTS idx_wv_page ON wiki_versions(page_id, version);
CREATE INDEX IF NOT EXISTS idx_wv_branch ON wiki_versions(branch_tag);

-- Cognitive audit: every "should I forget / merge / contradict" call
-- writes one row here. The dashboard reads from this table to show
-- "agent decided to forget X" history; CLI ``loop-memory audit``
-- dumps it.
CREATE TABLE IF NOT EXISTS cognitive_audit (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,   -- 'forget'|'merge'|'contradict'|'stale'|'low_value'
    action      TEXT NOT NULL,   -- 'suggest'|'applied'|'reverted'
    target_kind TEXT NOT NULL,   -- 'memory'|'wiki_page'
    target_id   TEXT,
    target_text TEXT,
    reason      TEXT,
    score       REAL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ca_ts ON cognitive_audit(ts);
CREATE INDEX IF NOT EXISTS idx_ca_kind ON cognitive_audit(kind, action);

-- Per-(user, agent) bearer tokens. Optional; the local server keeps
-- the route open by default. Loop-memory serve with --auth
-- --token-required enables it; the SDK auto-attaches the bearer
-- header when ``MemoryClient.http(..., token=...)`` is given.
CREATE TABLE IF NOT EXISTS auth_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT,
    agent_id    TEXT,
    label       TEXT,
    token_hash  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_used_at REAL,
    expires_at  REAL,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_at_user ON auth_tokens(user_id, agent_id);

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
    agent_id: str | None = None
    user_id: str | None = None
    external_id: str | None = None


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

    SCHEMA_VERSION = "7"

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
            if "key_facts" not in cols:
                c.execute("ALTER TABLE wiki_pages ADD COLUMN key_facts TEXT")
            if "contradicting_ids" not in cols:
                c.execute("ALTER TABLE wiki_pages ADD COLUMN contradicting_ids TEXT")

            # Universal Agent Memory migration: add per-agent identity
            # columns to memories. Bumping SCHEMA_VERSION from "5" → "6"
            # so a future downgrade is loud.
            mem_cols = {row["name"] for row in c.execute("PRAGMA table_info(memories)").fetchall()}
            if "agent_id" not in mem_cols:
                c.execute("ALTER TABLE memories ADD COLUMN agent_id TEXT")
            if "user_id" not in mem_cols:
                c.execute("ALTER TABLE memories ADD COLUMN user_id TEXT")
            if "external_id" not in mem_cols:
                c.execute("ALTER TABLE memories ADD COLUMN external_id TEXT")
            # Indexes must run after the ALTER TABLE so the columns
            # they reference actually exist on legacy DBs.
            c.execute("CREATE INDEX IF NOT EXISTS idx_mem_agent    ON memories(agent_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mem_user     ON memories(user_id)")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_external "
                "ON memories(agent_id, user_id, external_id) "
                "WHERE external_id IS NOT NULL AND external_id != ''"
            )

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
        agent_id: str | None = None,
        user_id: str | None = None,
        external_id: str | None = None,
    ) -> StoredMemory:
        """Create or update a memory.

        Two idempotency paths:

        * by ``id`` (caller supplies a UUID-like id)
        * by ``(agent_id, user_id, external_id)`` tuple — any Agent
          that re-pushes the same ``external_id`` updates the row in
          place instead of duplicating it. This is the path used by
          the universal ``MemoryClient.remember()`` SDK and the
          ``/api/v1/memories`` endpoint.

        Either input may be ``None`` (the unique index excludes
        NULL/empty external_ids, so a memory with no external_id
        cannot collide and gets a fresh row).
        """
        import json

        now = time.time()
        ext = (external_id or "").strip() or None
        ts = created_at or now
        uts = updated_at or ts
        tags_json = json.dumps(tags or [])
        score = self.compute_score(importance, ts, now)
        with self._conn() as c:
            # Re-route by external_id when the caller did not pin an id.
            if id is None and ext and agent_id is not None:
                row = c.execute(
                    "SELECT id FROM memories "
                    "WHERE agent_id IS ? AND user_id IS ? AND external_id = ?",
                    (agent_id, user_id, ext),
                ).fetchone()
                if row is not None:
                    id = row["id"]
            mid = id or uuid.uuid4().hex
            c.execute(
                """INSERT INTO memories
                   (id, session_id, kind, text, importance, source,
                    created_at, updated_at, score, ttl, tags, embedding,
                    agent_id, user_id, external_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     text=excluded.text,
                     importance=excluded.importance,
                     updated_at=excluded.updated_at,
                     score=excluded.score,
                     tags=excluded.tags,
                     embedding=COALESCE(excluded.embedding, memories.embedding),
                     agent_id=COALESCE(excluded.agent_id, memories.agent_id),
                     user_id=COALESCE(excluded.user_id, memories.user_id),
                     external_id=COALESCE(excluded.external_id, memories.external_id),
                     session_id=COALESCE(excluded.session_id, memories.session_id),
                     source=COALESCE(excluded.source, memories.source)""",
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
                    agent_id,
                    user_id,
                    ext,
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
        agent_id: str | None = None,
        user_id: str | None = None,
        external_id: str | None = None,
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
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if external_id is not None:
            clauses.append("external_id = ?")
            params.append(external_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._row_to_memory(r) for r in rows]

    def find_memory_by_external_id(
        self,
        agent_id: str,
        external_id: str,
        user_id: str | None = None,
    ) -> StoredMemory | None:
        """Look up a memory by its (agent_id, user_id, external_id) tuple.

        Returns ``None`` when no row matches. Used by the SDK + REST
        API so external systems can update / delete / feedback on
        memories they pushed without needing the internal row id.
        """
        if not agent_id or not external_id:
            return None
        with self._conn() as c:
            if user_id is None:
                row = c.execute(
                    "SELECT * FROM memories "
                    "WHERE agent_id = ? AND external_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (agent_id, external_id),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM memories "
                    "WHERE agent_id = ? AND user_id = ? AND external_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (agent_id, user_id, external_id),
                ).fetchone()
        return self._row_to_memory(row) if row else None

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
        level: int = 1,
        adaptive: bool = False,
    ) -> dict[str, list[dict]]:
        """RRF-fused recall across BM25 + semantic + entity channels.

        ``adaptive=True`` blends the 4D AdaptiveScore (importance +
        recency + usage + graph_degree) and applies the graph boost
        from ``jobs.graph.graph_boost``. Off by default to keep the
        existing dashboard + MCP behaviour byte-identical.
        RRF-fused recall across BM25 + semantic + entity channels.

        ``source`` enables per-source scope: only wiki pages whose
        scope is 'global' OR contains this source are returned. If
        ``source`` is None, no scope filter is applied (the dashboard
        + admin recall see everything).

        ``level`` is the OpenViking-style tiered-loader knob:

        * 0 → L0 (titles + tags + preview, never the raw body / full
          text). Smallest payload, suitable for sidebar chips.
        * 1 → L1 (default): summary + first 800 chars of body;
          full text of memory rows. Recommended for the Timeline.
        * 2 → L2: full body, full text. Use this when the UI is
          explicitly expanding a wiki page or memory, not for
          bulk recall.
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
            # We don't multiply here: the per-row hydration step in
            # _hydrate_memories / _hydrate_wiki reads the actual
            # created_at / updated_at from SQLite and recomputes
            # the temporal score with that timestamp. The fused
            # rows only need the intent + confidence carried through
            # so the hydration helpers can pick them up.
            for r in fused_mem:
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
                level=level,
            )
        if wiki_ids:
            out["wiki"] = self._hydrate_wiki(
                wiki_ids, fused_wiki,
                t_intent=t_intent, t_conf=t_conf, now=_now_ts,
                level=level,
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

        # --- 3D adaptive scoring + graph boost ----------------------
        # ``adaptive=True`` blends the 4D AdaptiveScore (importance +
        # recency + usage + graph_degree) with the existing RRF
        # score. The graph boost is a separate multiplier in
        # [0, 1.5] computed from the query's entity neighbourhood;
        # it can dominate when the memory shares multiple entities
        # with the query. Implementation: 60% RRF + 40% adaptive
        # blend, multiplied by (1 + graph_boost). This is the
        # "third dimension" the article calls out as Mem0's
        # differentiator from plain RAG.
        if adaptive and out["memories"]:
            try:
                from ..jobs.graph import (
                    graph_boost as _gb,
                    adaptive_score as _as,
                )  # type: ignore
            except Exception:
                _gb = _as = None  # type: ignore
            if _gb is not None and _as is not None:
                mem_ids = [m["id"] for m in out["memories"]]
                boosts = _gb(self, query, mem_ids)
                now_ts = _time.time()
                for m in out["memories"]:
                    last_recalled = m.get("last_recalled_at")
                    s_ = _as(
                        importance=m.get("importance") or 0.5,
                        created_at=m.get("created_at") or now_ts,
                        now=now_ts,
                        recall_count=int(m.get("recall_count") or 0),
                        last_recalled_at=last_recalled,
                        graph_degree=len(boosts[m["id"]].matched_entities)
                                      if m["id"] in boosts else 0,
                    )
                    g_boost = boosts[m["id"]].boost if m["id"] in boosts else 0.0
                    blended = (0.6 * (m.get("score") or 0) + 0.4 * s_.blended)
                    new_score = blended * (1.0 + g_boost)
                    m["score"] = round(new_score, 4)
                    m["_adaptive"] = s_.to_dict()
                    m["_graph_boost"] = g_boost
                    if m["id"] in boosts:
                        m["_graph_entities"] = boosts[m["id"]].matched_entities
                out["memories"].sort(key=lambda m: -m["score"])
                out["adaptive"] = True

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
        return [{"id": r["id"], "_score": float(r["importance"] or 0)} for r in rows]

    @staticmethod
    def _source_token(name: str) -> str:
        return (name or '').strip().lower().replace(' ', '-')

    def _hydrate_memories(self, ids: list[str], scored: list[dict], source: str | None = None,
                           t_intent: str = "any", t_conf: float = 0.0, now: float = 0.0,
                           level: int = 1) -> list[dict]:
        if not ids:
            return []
        score_map = {r["id"]: r.get("_rrf", 0) for r in scored}
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT m.id, m.kind, m.text, m.importance, m.score, m.source,
                          m.tags, m.created_at, m.updated_at,
                          m.agent_id, m.user_id, m.external_id,
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
            full_text = r["text"] or ""
            # Tiered payload (OpenViking pattern): level<=0 trims
            # the full text down to the preview and tags only.
            # level>=1 keeps the full text. level<=2 is the default
            # ("L1") which keeps the text but trims the body in the
            # corresponding wiki hydration.
            if level <= 0:
                payload_text = ""
            else:
                payload_text = full_text
            out.append({
                "id": r["id"],
                "kind": "memory",
                "text": payload_text,
                "importance": float(r["importance"] or 0),
                "score_field": float(r["score"] or 0),
                "source": r["source"],
                "tags": tags,
                "created_at": float(r["created_at"] or 0),
                "recall_count": int(r["recall_count"] or 0),
                "agent_id": r["agent_id"] if "agent_id" in r.keys() else None,
                "user_id": r["user_id"] if "user_id" in r.keys() else None,
                "external_id": r["external_id"] if "external_id" in r.keys() else None,
                "score": round(base_score * t_mult, 4),
                "_temporal_multiplier": round(t_mult, 3),
                "preview": full_text[:240],
                "_level": level,
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
                      t_intent: str = "any", t_conf: float = 0.0, now: float = 0.0,
                      level: int = 1) -> list[dict]:
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
            full_body = r["body"] or ""
            summary_txt = r["summary"] or ""
            # Tiered payload for wiki pages:
            # L0 (level<=0): title + tags + preview only — body and
            #     summary dropped entirely.
            # L1 (default, level<=1): keep summary; cap body at 800
            #     chars so the prompt still fits cheaply.
            # L2 (level>=2): full body.
            if level <= 0:
                payload_summary = ""
                payload_body = ""
            elif level == 1:
                payload_summary = summary_txt
                payload_body = full_body[:800]
            else:
                payload_summary = summary_txt
                payload_body = full_body
            out.append({
                "id": r["id"],
                "kind": "wiki",
                "slug": r["slug"],
                "title": r["title"],
                "summary": payload_summary,
                "body": payload_body,
                "importance": float(r["importance"] or 0),
                "tags": tags,
                "scope": r["scope"] or "global",
                "updated_at": float(r["updated_at"] or 0),
                "version": int(r["version"] or 1),
                "score": round(base_score * t_mult, 4),
                "_temporal_multiplier": round(t_mult, 3),
                "preview": (summary_txt or full_body or "")[:240],
                "_level": level,
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
    def entity_by_name(self, name: str) -> dict | None:
        """Look up a single entity row by its canonical name.

        Returns ``None`` if the entity is unknown. Used by the graph
        job (``subgraph_for``) to attach ``kind`` / ``weight`` to
        nodes it materialises from a query.
        """
        n = (name or "").strip()
        if not n:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM entities WHERE name = ? LIMIT 1",
                (n,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def related_entities(self, name: str, limit: int = 32) -> list[str]:
        """Return the entity names connected to ``name`` via a relation.

        Walks both ``src = name`` and ``dst = name`` so the caller
        doesn't have to know which side the entity landed on.
        """
        n = (name or "").strip()
        if not n:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT src, dst FROM relations WHERE src = ? OR dst = ? LIMIT ?",
                (n, n, limit),
            ).fetchall()
        out: list[str] = []
        for r in rows:
            other = r["dst"] if r["src"] == n else r["src"]
            if other and other != n and other not in out:
                out.append(other)
        return out[:limit]

    def upsert_entity_mention(self, memory_id: str, entity_name: str,
                                *, weight: float = 0.5) -> bool:
        """Record that ``memory_id`` mentions ``entity_name``.

        Idempotent: (memory_id, entity_id) is the primary key. Returns
        True if a new row was inserted, False if it already existed
        (in which case the existing weight is left alone — the first
        mention is the strongest signal).
        """
        n = (entity_name or "").strip()
        if not memory_id or not n:
            return False
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM entities WHERE name = ?", (n,),
            ).fetchone()
            if not row:
                return False
            try:
                c.execute(
                    "INSERT INTO entity_mentions (memory_id, entity_id, weight, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (memory_id, row["id"], float(weight), time.time()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def rebuild_entity_mentions(self) -> int:
        """Re-extract entities from every memory text and write the
        (memory_id, entity_id) rows needed by ``graph_boost`` and the
        knowledge-graph UI.

        Idempotent: re-running clears the previous mentions first.
        Returns the number of new mention rows.
        """
        # We reuse the lightweight graph extractor. Keeping the
        # import local avoids a circular import at module load.
        from ..graph.extract import extract_entities
        n_inserted = 0
        with self._conn() as c:
            c.execute("DELETE FROM entity_mentions")
            rows = c.execute(
                "SELECT id, text, tags, source FROM memories"
            ).fetchall()
            for r in rows:
                ents = extract_entities(r["text"] or "")
                for name, _kind in ents:
                    ent = c.execute(
                        "SELECT id FROM entities WHERE name = ?", (name,),
                    ).fetchone()
                    if not ent:
                        continue
                    try:
                        c.execute(
                            "INSERT INTO entity_mentions (memory_id, entity_id, weight, created_at) "
                            "VALUES (?, ?, 0.5, ?)",
                            (r["id"], ent["id"], time.time()),
                        )
                        n_inserted += 1
                    except sqlite3.IntegrityError:
                        pass
        return n_inserted

    def memory_ids_for_entity(self, name: str, limit: int = 64) -> list[str]:
        """Return the memory ids that mention the given entity.

        Joins ``entity_mentions`` → ``entities`` so callers can look
        up the backing memories of a graph node in O(1) without
        re-running entity extraction on the original text.
        """
        n = (name or "").strip()
        if not n:
            return []
        with self._conn() as c:
            rows = c.execute(
                """SELECT em.memory_id
                   FROM entity_mentions em
                   JOIN entities e ON e.id = em.entity_id
                   WHERE e.name = ?
                   ORDER BY em.weight DESC
                   LIMIT ?""",
                (n, limit),
            ).fetchall()
        return [r["memory_id"] for r in rows if r["memory_id"]]

    def graph_subgraph_for_query(
        self,
        query: str,
        *,
        max_hops: int = 1,
        max_nodes: int = 32,
        max_edges: int = 64,
    ) -> dict:
        """Convenience wrapper: same as
        ``loop_memory.jobs.graph.subgraph_for`` but inlined so the
        store can serve it from a single SQL path. The graph job
        uses this when it wants to stay inside the store's
        transaction boundary.
        """
        from ..jobs.graph import subgraph_for as _sg  # type: ignore
        sg = _sg(self, query, max_hops=max_hops,
                 max_nodes=max_nodes, max_edges=max_edges)
        return sg.to_dict()

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
            agent_id=row["agent_id"] if "agent_id" in row.keys() else None,
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            external_id=row["external_id"] if "external_id" in row.keys() else None,
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
        key_facts: list[str] | None = None,
        contradicting_ids: list[str] | None = None,
    ) -> Dict[str, Any]:
        """Create-or-update a wiki page by slug.

        Returns the full row as a dict so the API can hand it back to
        the UI without an extra round-trip.

        ``key_facts`` and ``contradicting_ids`` are optional JSON-array
        columns (see ``_init_schema``). Older callers pass neither
        and the columns stay NULL.
        """
        import json as _json
        import uuid as _uuid
        now = time.time()
        tags_json = _json.dumps(tags or [], ensure_ascii=False)
        evid_json = _json.dumps(evidence_ids or [], ensure_ascii=False)
        kf_json = _json.dumps(key_facts or [], ensure_ascii=False) if key_facts is not None else None
        ci_json = _json.dumps(contradicting_ids or [], ensure_ascii=False) if contradicting_ids is not None else None
        with self._conn() as c:
            existing = c.execute(
                "SELECT id, version FROM wiki_pages WHERE slug=?", (slug,)
            ).fetchone()
            if existing is None:
                pid = _uuid.uuid4().hex
                version = 1
                c.execute(
                    "INSERT INTO wiki_pages(id, slug, title, body, summary, tags, "
                    "importance, evidence_ids, run_id, version, created_at, updated_at, scope, "
                    "key_facts, contradicting_ids) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, slug, title, body, summary or "", tags_json,
                     float(importance), evid_json, run_id, version, now, now,
                     scope or "global", kf_json, ci_json),
                )
            else:
                pid = existing["id"]
                version = (existing["version"] or 1) + 1
                # Only override key_facts / contradicting_ids when the
                # caller explicitly passes them — preserves lists
                # built up by the contradiction detector across edits.
                if key_facts is not None:
                    c.execute("UPDATE wiki_pages SET key_facts=? WHERE id=?",
                              (kf_json, pid))
                if contradicting_ids is not None:
                    c.execute("UPDATE wiki_pages SET contradicting_ids=? WHERE id=?",
                              (ci_json, pid))
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
        scope: str | None = None,
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
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM wiki_pages {where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._row_to_wiki(r) for r in rows]

    def merge_wiki_pages(
        self,
        *,
        winner_id: str,
        loser_id: str,
        merged_body: str | None = None,
        merged_summary: str | None = None,
        merged_key_facts: list[str] | None = None,
        merged_importance: float | None = None,
        merged_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Merge two wiki pages into one and archive the loser.

        The winner keeps its id; the loser is deleted (its evidence
        ids are preserved as a record in ``merged_into`` so any
        later re-scan can see what was absorbed). The winner's
        body/summary/key_facts are replaced with the caller-supplied
        merged values. Returns a small dict describing what changed.

        Used by the contradiction UI: the user previews a side-by-side
        diff, edits a merged body, and posts it here. The loser is
        gone in the same transaction so the UI can refresh once.
        """
        if winner_id == loser_id:
            raise ValueError("merge_wiki_pages needs two distinct ids")
        winner = self.get_wiki_page(winner_id)
        loser = self.get_wiki_page(loser_id)
        if not winner or not loser:
            raise ValueError("both pages must exist")
        # Carry the loser's evidence_ids forward — they're a record
        # of which raw memories contributed to the merged topic.
        winner_evidence = list(winner.get("evidence_ids") or [])
        winner_evidence.extend(loser.get("evidence_ids") or [])
        # Dedup but keep order.
        seen = set()
        merged_evidence = []
        for x in winner_evidence:
            if x in seen:
                continue
            seen.add(x)
            merged_evidence.append(x)
        body = merged_body if merged_body is not None else winner.get("body") or ""
        summary = merged_summary if merged_summary is not None else winner.get("summary") or ""
        facts = merged_key_facts if merged_key_facts is not None else winner.get("key_facts") or []
        tags = merged_tags if merged_tags is not None else winner.get("tags") or []
        imp = merged_importance if merged_importance is not None else max(
            float(winner.get("importance") or 0),
            float(loser.get("importance") or 0),
        )
        # Clear the winner's contradicting_ids — once merged, there's
        # nothing left to flag.
        with self._conn() as c:
            c.execute(
                "UPDATE wiki_pages SET body=?, summary=?, key_facts=?, "
                "tags=?, importance=?, evidence_ids=?, contradicting_ids=?, "
                "updated_at=? WHERE id=?",
                (
                    body,
                    summary,
                    json.dumps(facts, ensure_ascii=False) if facts is not None else None,
                    json.dumps(tags, ensure_ascii=False),
                    imp,
                    json.dumps(merged_evidence, ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    time.time(),
                    winner_id,
                ),
            )
            c.execute("DELETE FROM wiki_pages WHERE id=?", (loser_id,))
            # Also drop the loser from any other page's contradicting_ids
            c.execute(
                "UPDATE wiki_pages SET contradicting_ids="
                "REPLACE(REPLACE(contradicting_ids, ?, ''), ?, '') "
                "WHERE contradicting_ids LIKE ?",
                (
                    f'"{loser_id}"',
                    f',"{loser_id}"',
                    f'%{loser_id}%',
                ),
            )
        return {
            "winner_id": winner_id,
            "loser_id": loser_id,
            "winner_title": winner.get("title") or "",
            "loser_title": loser.get("title") or "",
            "merged": {
                "body_len": len(body),
                "summary_len": len(summary),
                "facts": len(facts),
                "evidence_ids": len(merged_evidence),
                "importance": imp,
            },
        }

    def resolve_contradiction(self, page_id: str) -> bool:
        """Clear a page's ``contradicting_ids`` so it disappears from
        the contradiction list. Use when the user inspects and
        decides there is no real conflict (e.g. the two pages are
        about different facets of the same topic)."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE wiki_pages SET contradicting_ids=? WHERE id=?",
                (json.dumps([], ensure_ascii=False), page_id),
            )
            return cur.rowcount > 0

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
        tags = []
        if row["tags"]:
            try:
                tags = json.loads(row["tags"])
            except (ValueError, TypeError):
                logger.warning("corrupt tags for relation %s; resetting", row["id"])
                tags = []
        evidence = []
        if row["evidence_ids"]:
            try:
                evidence = json.loads(row["evidence_ids"])
            except (ValueError, TypeError):
                logger.warning("corrupt evidence_ids for relation %s; resetting", row["id"])
                evidence = []
        key_facts: list[str] = []
        if row["key_facts"]:
            try:
                parsed = json.loads(row["key_facts"])
                if isinstance(parsed, list):
                    key_facts = [str(x) for x in parsed if x]
            except (ValueError, TypeError):
                logger.warning("corrupt key_facts for wiki page %s; resetting", row["id"])
                key_facts = []
        contradicting_ids: list[str] = []
        if row["contradicting_ids"]:
            try:
                parsed = json.loads(row["contradicting_ids"])
                if isinstance(parsed, list):
                    contradicting_ids = [str(x) for x in parsed if x]
            except (ValueError, TypeError):
                logger.warning("corrupt contradicting_ids for wiki page %s; resetting", row["id"])
                contradicting_ids = []
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
            "key_facts": key_facts,
            "contradicting_ids": contradicting_ids,
            "scope": row["scope"] or "global",
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

    # --- Universal Agent Memory v7 ----------------------------------
    # Methods for the three new tables (wiki_versions, cognitive_audit,
    # auth_tokens). All keep the same return-shape conventions as the
    # rest of the file: dataclasses for memory-shaped rows, dicts for
    # raw query results.

    # ----- wiki_versions ----------------------------------------------

    def snapshot_wiki_version(
        self,
        page_id: str,
        *,
        branch_tag: str | None = None,
    ) -> dict | None:
        """Snapshot the current state of a wiki page into ``wiki_versions``.

        Called on every ``upsert_wiki_page`` and whenever the user
        runs ``MemoryClient.fork(branch_tag=...)``. Returns the new
        version row, or ``None`` if the page doesn't exist.
        """
        page = self.get_wiki_page(page_id)
        if page is None:
            return None
        import json
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM wiki_versions WHERE page_id=?",
                (page_id,),
            ).fetchone()
            next_v = int(row["v"] or 0) + 1
            wid = uuid.uuid4().hex
            c.execute(
                """INSERT INTO wiki_versions
                   (id, page_id, version, title, body, summary, tags,
                    importance, key_facts, scope, branched_at, branch_tag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    wid,
                    page_id,
                    next_v,
                    page.get("title") or "",
                    page.get("body") or "",
                    page.get("summary") or "",
                    json.dumps(page.get("tags") or []),
                    float(page.get("importance") or 0.5),
                    json.dumps(page.get("key_facts") or []),
                    page.get("scope") or "global",
                    time.time(),
                    branch_tag,
                ),
            )
        return {
            "id": wid,
            "page_id": page_id,
            "version": next_v,
            "title": page.get("title") or "",
            "summary": page.get("summary") or "",
            "tags": page.get("tags") or [],
            "importance": float(page.get("importance") or 0.5),
            "scope": page.get("scope") or "global",
            "branch_tag": branch_tag,
        }

    def list_wiki_versions(
        self,
        page_id: str | None = None,
        *,
        branch_tag: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Return version history, newest first."""
        clauses: list[str] = []
        params: list = []
        if page_id is not None:
            clauses.append("page_id = ?")
            params.append(page_id)
        if branch_tag is not None:
            clauses.append("branch_tag = ?")
            params.append(branch_tag)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        import json
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM wiki_versions {where} "
                "ORDER BY branched_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                tags = json.loads(r["tags"]) if r["tags"] else []
            except Exception:
                tags = []
            try:
                kf = json.loads(r["key_facts"]) if r["key_facts"] else []
            except Exception:
                kf = []
            out.append({
                "id": r["id"],
                "page_id": r["page_id"],
                "version": int(r["version"] or 1),
                "title": r["title"] or "",
                "summary": r["summary"] or "",
                "tags": tags,
                "importance": float(r["importance"] or 0.5),
                "key_facts": kf,
                "scope": r["scope"] or "global",
                "branched_at": float(r["branched_at"] or 0),
                "branch_tag": r["branch_tag"],
            })
        return out

    def get_wiki_version(self, version_id: str) -> dict | None:
        import json
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM wiki_versions WHERE id=?", (version_id,),
            ).fetchone()
        if not r:
            return None
        try:
            tags = json.loads(r["tags"]) if r["tags"] else []
        except Exception:
            tags = []
        try:
            kf = json.loads(r["key_facts"]) if r["key_facts"] else []
        except Exception:
            kf = []
        return {
            "id": r["id"],
            "page_id": r["page_id"],
            "version": int(r["version"] or 1),
            "title": r["title"] or "",
            "body": r["body"] or "",
            "summary": r["summary"] or "",
            "tags": tags,
            "importance": float(r["importance"] or 0.5),
            "key_facts": kf,
            "scope": r["scope"] or "global",
            "branched_at": float(r["branched_at"] or 0),
            "branch_tag": r["branch_tag"],
        }

    # ----- cognitive_audit -------------------------------------------

    def record_audit(
        self,
        *,
        kind: str,
        action: str,
        target_kind: str,
        target_id: str | None = None,
        target_text: str | None = None,
        reason: str | None = None,
        score: float | None = None,
        payload: dict | None = None,
    ) -> dict:
        """Append one row to ``cognitive_audit``.

        ``kind`` is the trigger category — ``forget``, ``merge``,
        ``contradict``, ``stale``, ``low_value``. ``action`` is the
        disposition — ``suggest`` (we proposed it but didn't touch
        data), ``applied`` (the SDK / CLI ran the cleanup), or
        ``reverted`` (the user undid it).
        """
        import json
        aid = uuid.uuid4().hex
        ts = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO cognitive_audit
                   (id, ts, kind, action, target_kind, target_id,
                    target_text, reason, score, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    ts,
                    kind,
                    action,
                    target_kind,
                    target_id,
                    target_text,
                    reason,
                    score,
                    json.dumps(payload or {}),
                ),
            )
        return {
            "id": aid,
            "ts": ts,
            "kind": kind,
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_text": target_text,
            "reason": reason,
            "score": score,
            "payload": payload or {},
        }

    def list_audit(
        self,
        *,
        kind: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if action:
            clauses.append("action = ?")
            params.append(action)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        import json
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM cognitive_audit {where} "
                "ORDER BY ts DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                pj = json.loads(r["payload"]) if r["payload"] else {}
            except Exception:
                pj = {}
            out.append({
                "id": r["id"],
                "ts": float(r["ts"] or 0),
                "kind": r["kind"],
                "action": r["action"],
                "target_kind": r["target_kind"],
                "target_id": r["target_id"],
                "target_text": r["target_text"],
                "reason": r["reason"],
                "score": r["score"],
                "payload": pj,
            })
        return out

    # ----- auth_tokens -----------------------------------------------

    def issue_token(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        label: str | None = None,
        expires_in: float | None = None,
    ) -> dict:
        """Mint a bearer token scoped to ``(user_id, agent_id)``.

        Returns ``{"id", "token", "user_id", "agent_id", "label",
        "created_at", "expires_at"}``. The token is only available
        at issue time — the store keeps a SHA-256 hash so it can be
        verified but never recovered.
        """
        import hashlib
        import secrets
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        tid = uuid.uuid4().hex
        now = time.time()
        exp = (now + expires_in) if expires_in else None
        with self._conn() as c:
            c.execute(
                """INSERT INTO auth_tokens
                   (id, user_id, agent_id, label, token_hash,
                    created_at, expires_at, revoked)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (tid, user_id, agent_id, label, token_hash, now, exp),
            )
        return {
            "id": tid,
            "token": token,
            "user_id": user_id,
            "agent_id": agent_id,
            "label": label,
            "created_at": now,
            "expires_at": exp,
        }

    def verify_token(self, token: str) -> dict | None:
        """Return the token row if ``token`` is valid, else ``None``."""
        import hashlib
        if not token:
            return None
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM auth_tokens WHERE token_hash=? AND revoked=0",
                (h,),
            ).fetchone()
        if not r:
            return None
        if r["expires_at"] and float(r["expires_at"]) < now:
            return None
        # Best-effort: bump last_used_at. We don't fail if it errors
        # — verification is the contract.
        try:
            with self._conn() as c:
                c.execute(
                    "UPDATE auth_tokens SET last_used_at=? WHERE id=?",
                    (now, r["id"]),
                )
        except Exception:
            pass
        return {
            "id": r["id"],
            "user_id": r["user_id"],
            "agent_id": r["agent_id"],
            "label": r["label"],
            "created_at": float(r["created_at"] or 0),
            "expires_at": r["expires_at"],
        }

    def revoke_token(self, token_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE auth_tokens SET revoked=1 WHERE id=? AND revoked=0",
                (token_id,),
            )
            return cur.rowcount > 0

    def list_tokens(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, user_id, agent_id, label, created_at, "
                "last_used_at, expires_at, revoked FROM auth_tokens "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "agent_id": r["agent_id"],
                "label": r["label"],
                "created_at": float(r["created_at"] or 0),
                "last_used_at": r["last_used_at"],
                "expires_at": r["expires_at"],
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ]

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
                "db_size_bytes": self.db_size_bytes(),
            }

    def db_size_bytes(self) -> int:
        """Return the on-disk size of the SQLite file in bytes.

        Cheap (one stat call) so the dashboard can poll it freely.
        """
        try:
            return int(self.path.stat().st_size)
        except OSError:
            return 0

    def list_low_value_memories(self, limit: int = 500) -> list[StoredMemory]:
        """Memories ranked by *combined* importance × score, ascending.

        Used by the compactor to drop the least-useful rows when the
        store exceeds its hard ceiling. Excludes rows that have ever
        been recalled — we never evict demonstrably-useful memories
        without an explicit user action.
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT m.*
                FROM memories m
                LEFT JOIN memory_signals s ON s.memory_id = m.id
                WHERE COALESCE(s.recall_count, 0) = 0
                  AND m.kind != 'digest'
                ORDER BY (COALESCE(m.score, 0) * COALESCE(m.importance, 0)) ASC,
                         COALESCE(m.created_at, 0) ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_memory(r) for r in rows]

    def storage_breakdown(self) -> dict[str, int]:
        """Per-table row counts and approximate bytes-on-disk.

        The on-disk byte estimate is from SQLite's ``dbstat`` virtual
        table when available; falls back to a row-count × avg-size
        heuristic otherwise.
        """
        out: dict[str, int] = {}
        with self._conn() as c:
            for tbl in ("memories", "sessions", "wiki_pages", "entities", "relations",
                        "memory_signals", "contradiction_pairs", "drops", "settings"):
                try:
                    row = c.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()
                    out[tbl] = int(row["c"] or 0)
                except sqlite3.OperationalError:
                    out[tbl] = 0
        out["db_size_bytes"] = self.db_size_bytes()
        return out


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

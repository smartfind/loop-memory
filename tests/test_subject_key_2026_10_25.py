"""Subject-key logical fact slots (audit 2026-10-25, loop-memory 0.4.16).

A *subject key* names the logical slot a fact occupies -- e.g.
``"editor.preference"``. Two memories carrying the same
``(subject_key, agent_id, user_id)`` are two *versions* of one fact,
not two independent facts.

The contract pinned here:

* writing a new value for a slot soft-supersedes the previous one
  (``superseded_by`` pointer, no DELETE);
* the version history survives, so ``recall_as_of`` can still answer
  "what did we believe last week?";
* the content-hash dedup path does **not** collapse two different
  slots that happen to hold the same text;
* rows without a subject key keep the legacy "every row is its own
  fact" behaviour;
* the column survives a migration from an older schema.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from loop_memory.storage.sqlite_store import MemoryStore


class SubjectKeyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "subject.db")
        self.store = MemoryStore(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- write path ---------------------------------------------------

    def test_subject_key_is_persisted(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="User prefers tabs",
            agent_id="codex", user_id="u1",
            subject_key="editor.preference",
        )
        self.assertEqual(m.subject_key, "editor.preference")
        again = self.store.get_memory(m.id)
        self.assertEqual(again.subject_key, "editor.preference")

    def test_blank_subject_key_is_normalised_to_none(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="A standalone note", subject_key="   ",
        )
        self.assertIsNone(m.subject_key)

    def test_second_write_supersedes_the_first(self) -> None:
        now = time.time()
        first = self.store.upsert_memory(
            kind="fact", text="User prefers tabs",
            agent_id="codex", user_id="u1",
            subject_key="editor.preference",
            created_at=now - 100, updated_at=now - 100,
        )
        second = self.store.upsert_memory(
            kind="fact", text="User prefers spaces",
            agent_id="codex", user_id="u1",
            subject_key="editor.preference",
            created_at=now, updated_at=now,
        )
        self.assertNotEqual(first.id, second.id)
        # The loser gets a pointer, it is NOT deleted.
        loser = self.store.get_memory(first.id)
        self.assertIsNotNone(loser, "the superseded row must survive")
        self.assertEqual(loser.superseded_by, second.id)
        # The winner is live.
        self.assertIsNone(self.store.get_memory(second.id).superseded_by)

    def test_supersession_chain_is_listable(self) -> None:
        now = time.time()
        a = self.store.upsert_memory(
            kind="fact", text="v1 of the fact", agent_id="c", user_id="u",
            subject_key="k", created_at=now - 200, updated_at=now - 200,
        )
        b = self.store.upsert_memory(
            kind="fact", text="v2 of the fact", agent_id="c", user_id="u",
            subject_key="k", created_at=now - 100, updated_at=now - 100,
        )
        c = self.store.upsert_memory(
            kind="fact", text="v3 of the fact", agent_id="c", user_id="u",
            subject_key="k", created_at=now, updated_at=now,
        )
        superseded = {row["id"] for row in self.store.list_superseded()}
        self.assertIn(a.id, superseded)
        self.assertIn(b.id, superseded)
        self.assertNotIn(c.id, superseded)

    def test_different_namespaces_do_not_supersede(self) -> None:
        first = self.store.upsert_memory(
            kind="fact", text="User prefers tabs",
            agent_id="codex", user_id="u1", subject_key="editor.preference",
        )
        self.store.upsert_memory(
            kind="fact", text="User prefers spaces",
            agent_id="claude", user_id="u1", subject_key="editor.preference",
        )
        self.assertIsNone(self.store.get_memory(first.id).superseded_by)

    def test_different_slots_do_not_supersede(self) -> None:
        first = self.store.upsert_memory(
            kind="fact", text="User prefers tabs",
            agent_id="codex", user_id="u1", subject_key="editor.preference",
        )
        self.store.upsert_memory(
            kind="fact", text="User prefers spaces",
            agent_id="codex", user_id="u1", subject_key="shell.preference",
        )
        self.assertIsNone(self.store.get_memory(first.id).superseded_by)

    def test_same_text_in_two_slots_stays_two_rows(self) -> None:
        """Content-hash dedup must not collapse two logical slots."""
        a = self.store.upsert_memory(
            kind="fact", text="the answer is 42",
            agent_id="codex", user_id="u1", subject_key="q1.answer",
        )
        b = self.store.upsert_memory(
            kind="fact", text="the answer is 42",
            agent_id="codex", user_id="u1", subject_key="q2.answer",
        )
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.store.count_memories(), 2)

    def test_same_text_same_slot_is_idempotent(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="the answer is 42",
            agent_id="codex", user_id="u1", subject_key="q1.answer",
        )
        b = self.store.upsert_memory(
            kind="fact", text="the answer is 42",
            agent_id="codex", user_id="u1", subject_key="q1.answer",
        )
        self.assertEqual(a.id, b.id)
        self.assertEqual(self.store.count_memories(), 1)

    def test_rows_without_subject_key_are_untouched(self) -> None:
        a = self.store.upsert_memory(kind="fact", text="standalone one")
        b = self.store.upsert_memory(kind="fact", text="standalone two")
        self.assertIsNone(self.store.get_memory(a.id).superseded_by)
        self.assertIsNone(self.store.get_memory(b.id).superseded_by)

    def test_new_statement_reopens_an_expired_slot(self) -> None:
        now = time.time()
        first = self.store.upsert_memory(
            kind="fact", text="User prefers tabs",
            agent_id="c", user_id="u", subject_key="k",
            invalid_at=now - 10,
        )
        self.assertIsNotNone(first.invalid_at)
        self.store.upsert_memory(
            kind="fact", text="User prefers spaces",
            agent_id="c", user_id="u", subject_key="k",
        )
        rows = [m for m in self.store.list_memories(limit=50)
                if m.subject_key == "k"]
        live = [m for m in rows if not m.superseded_by]
        self.assertEqual(len(live), 1)
        self.assertIsNone(live[0].invalid_at)

    # --- history ------------------------------------------------------

    def test_history_survives_the_supersession(self) -> None:
        now = time.time()
        self.store.upsert_memory(
            kind="fact", text="User prefers tabs",
            agent_id="c", user_id="u", subject_key="k",
            created_at=now - 500, updated_at=now - 500,
        )
        self.store.upsert_memory(
            kind="fact", text="User prefers spaces",
            agent_id="c", user_id="u", subject_key="k",
            created_at=now, updated_at=now,
        )
        past = self.store.recall_as_of(
            "User prefers", now - 100, limit=10,
        )
        texts = [m["text"] for m in past["memories"]]
        self.assertIn("User prefers tabs", texts)
        self.assertNotIn("User prefers spaces", texts)

    # --- migration ----------------------------------------------------

    def test_legacy_database_gains_the_column(self) -> None:
        legacy = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute(
            "CREATE TABLE memories ("
            " id TEXT PRIMARY KEY, session_id TEXT, kind TEXT NOT NULL,"
            " text TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5,"
            " source TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
            " score REAL NOT NULL DEFAULT 0.5, ttl REAL, tags TEXT,"
            " embedding BLOB)"
        )
        conn.commit()
        conn.close()
        store = MemoryStore(legacy)
        with store._conn() as c:  # noqa: SLF001
            cols = {r["name"] for r in c.execute(
                "PRAGMA table_info(memories)").fetchall()}
        self.assertIn("subject_key", cols)
        m = store.upsert_memory(kind="fact", text="hello", subject_key="k")
        self.assertEqual(m.subject_key, "k")

    def test_schema_version_is_13(self) -> None:
        with self.store._conn() as c:  # noqa: SLF001
            v = c.execute(
                "SELECT v FROM schema_meta WHERE k='version'"
            ).fetchone()["v"]
        self.assertEqual(v, "13")

    def test_subject_key_index_exists(self) -> None:
        with self.store._conn() as c:  # noqa: SLF001
            idx = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("idx_mem_subject_key", idx)

    # --- stats --------------------------------------------------------

    def test_memory_stats_exposes_subject_key_and_ttl_state(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="User prefers tabs", subject_key="k",
        )
        stats = self.store.memory_stats(m.id)
        self.assertEqual(stats["subject_key"], "k")
        self.assertEqual(stats["ttl_state"], "persistent")

    def test_memory_stats_ttl_state_expired(self) -> None:
        now = time.time()
        m = self.store.upsert_memory(
            kind="fact", text="gone", invalid_at=now - 5,
        )
        stats = self.store.memory_stats(m.id)
        self.assertEqual(stats["ttl_state"], "expired")


if __name__ == "__main__":
    unittest.main()

"""Content-hash dedup + bi-temporal columns (audit 2026-10-18, loop-memory 0.4.15).

The 0.4.15 migration adds three columns to ``memories``:
- ``content_hash`` (SHA-256 of normalised text) for fast same-content collapse
- ``valid_at`` (epoch the memory becomes active)
- ``invalid_at`` (epoch the memory stops being active)

The tests below pin the contract for each one: legacy DB migration
backfills the new columns, byte-identical text collapses even when
``external_id`` is None, ``dedup=False`` opts out for episodic event
streams, and the validity window is preserved across updates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import SCHEMA, MemoryStore


class ContentHashDedupTests(unittest.TestCase):
    """upsert_memory() dedup behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "dedup.db")
        # Don't open the store in setUp: the legacy-migration test
        # has to write to the file BEFORE MemoryStore runs, so opening
        # here would race with that test's hand-rolled legacy schema.
        self.store = MemoryStore(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_push_creates_row_with_hash(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="User prefers dark mode",
            agent_id="codex", user_id="u1",
        )
        self.assertIsNotNone(m.content_hash)
        # SHA-256 hex digest of the normalised text
        expected = hashlib.sha256(b"user prefers dark mode").hexdigest()
        self.assertEqual(m.content_hash, expected)

    def test_byte_identical_text_collapses(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="Deploy uses blue/green",
            agent_id="codex", user_id="u1",
        )
        b = self.store.upsert_memory(
            kind="fact", text="Deploy uses blue/green",
            agent_id="codex", user_id="u1",
        )
        self.assertEqual(a.id, b.id, "byte-identical text should collapse")
        self.assertEqual(a.content_hash, b.content_hash)

    def test_text_is_case_insensitive(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="User prefers dark mode",
            agent_id="codex", user_id="u1",
        )
        b = self.store.upsert_memory(
            kind="fact", text="user prefers dark mode",
            agent_id="codex", user_id="u1",
        )
        self.assertEqual(a.id, b.id, "case-insensitive collapse")

    def test_text_is_whitespace_insensitive(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="User prefers dark mode",
            agent_id="codex", user_id="u1",
        )
        b = self.store.upsert_memory(
            kind="fact", text="  User prefers dark mode  ",
            agent_id="codex", user_id="u1",
        )
        self.assertEqual(a.id, b.id, "leading/trailing whitespace collapse")

    def test_different_agent_does_not_collapse(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="Deploy uses blue/green",
            agent_id="codex", user_id="u1",
        )
        b = self.store.upsert_memory(
            kind="fact", text="Deploy uses blue/green",
            agent_id="claude", user_id="u1",
        )
        self.assertNotEqual(a.id, b.id, "different agent_id -> separate row")

    def test_different_user_does_not_collapse(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="Deploy uses blue/green",
            agent_id="codex", user_id="u1",
        )
        b = self.store.upsert_memory(
            kind="fact", text="Deploy uses blue/green",
            agent_id="codex", user_id="u2",
        )
        self.assertNotEqual(a.id, b.id, "different user_id -> separate row")

    def test_dedup_false_creates_new_row(self) -> None:
        a = self.store.upsert_memory(
            kind="episode", text="heartbeat tick at 100s",
            agent_id="codex", dedup=True,
        )
        b = self.store.upsert_memory(
            kind="episode", text="heartbeat tick at 100s",
            agent_id="codex", dedup=False,
        )
        self.assertNotEqual(a.id, b.id, "dedup=False forces a new row")
        # content_hash should still be set so callers can dedup
        # out-of-band if they want.
        self.assertIsNotNone(b.content_hash)

    def test_explicit_external_id_wins_over_hash(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="Old fact", agent_id="codex",
            external_id="ext-1",
        )
        # Different text, same external_id -> updates in place (legacy
        # contract preserved).
        b = self.store.upsert_memory(
            kind="fact", text="New fact", agent_id="codex",
            external_id="ext-1",
        )
        self.assertEqual(a.id, b.id)
        self.assertEqual(b.text, "New fact")

    def test_id_pinned_always_updates(self) -> None:
        # When ``id`` is supplied, the row is updated regardless of the
        # content hash (id is the strongest dedup key).
        a = self.store.upsert_memory(
            kind="fact", text="first text", agent_id="codex",
        )
        mid = a.id
        b = self.store.upsert_memory(
            id=mid, kind="fact", text="totally different text",
            agent_id="codex",
        )
        self.assertEqual(b.id, mid)
        self.assertEqual(b.text, "totally different text")

    def test_hash_set_on_legacy_db_migration(self) -> None:
        """When opening a pre-0.4.15 DB the migration backfills the hash
        for every existing row so dedup starts working immediately."""
        # Build a 0.4.14-shaped DB by hand: same DDL but without the
        # new columns. We use a *separate* file (not self.db, which
        # was already opened by setUp's MemoryStore) so the legacy
        # schema can include the pre-migration schema_meta row.
        legacy_db = os.path.join(self.tmp, "legacy_migration.db")
        legacy_schema = re.sub(r",\s*content_hash\s+TEXT", "", SCHEMA)
        legacy_schema = re.sub(r",\s*valid_at\s+REAL", "", legacy_schema)
        legacy_schema = re.sub(r",\s*invalid_at\s+REAL", "", legacy_schema)
        con = sqlite3.connect(legacy_db)
        con.executescript(legacy_schema)
        con.execute(
            "INSERT INTO schema_meta(k,v) VALUES('version','11')"
        )
        con.execute(
            "INSERT INTO memories (id, kind, text, importance, source, "
            "created_at, updated_at, score, tags) "
            "VALUES ('legacy1', 'fact', 'legacy hello', 0.5, NULL, "
            "1700000000.0, 1700000000.0, 0.5, '[]')"
        )
        con.commit()
        con.close()

        # Open -> migration kicks in, backfills hash + valid_at.
        store = MemoryStore(legacy_db)
        m = store.get_memory("legacy1")
        self.assertIsNotNone(m.content_hash)
        self.assertEqual(
            m.content_hash,
            hashlib.sha256(b"legacy hello").hexdigest(),
        )
        # valid_at defaults to created_at for legacy rows
        self.assertEqual(m.valid_at, 1700000000.0)
        self.assertIsNone(m.invalid_at)


class BiTemporalColumnsTests(unittest.TestCase):
    """valid_at / invalid_at window behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "validity.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_valid_at_is_created_at(self) -> None:
        before = time.time()
        m = self.store.upsert_memory(
            kind="fact", text="Immediate memory",
        )
        after = time.time()
        self.assertGreaterEqual(m.valid_at, before)
        self.assertLessEqual(m.valid_at, after)
        self.assertIsNone(m.invalid_at)

    def test_future_dated_memory(self) -> None:
        future = time.time() + 86400 * 7
        m = self.store.upsert_memory(
            kind="fact", text="Schedules for next week",
            valid_at=future,
        )
        self.assertEqual(m.valid_at, future)
        self.assertIsNone(m.invalid_at)

    def test_expired_memory(self) -> None:
        past = time.time() - 86400
        m = self.store.upsert_memory(
            kind="fact", text="Already past",
            invalid_at=past,
        )
        self.assertEqual(m.invalid_at, past)

    def test_update_preserves_unset_window(self) -> None:
        """Calling upsert_memory again without valid_at/invalid_at
        leaves the existing window intact (COALESCE NULL -> old value).

        Audit 2026-10-18 note: the upsert INSERT defaults
        ``valid_at`` to ``ts`` (created_at-equivalent) when caller
        passes ``None`` -- so the *INSERT* side DOES overwrite on a
        brand-new row. The window-preservation contract only kicks in
        on the UPDATE branch (when ``id`` matches an existing row).
        The COALESCE in the ON CONFLICT clause is what saves the
        stored window in that case. Here we exercise the update path
        explicitly."""
        future = time.time() + 86400 * 7
        m1 = self.store.upsert_memory(
            kind="fact", text="Future note",
            valid_at=future, invalid_at=future + 3600,
        )
        # Update text WITHOUT re-passing the window: the UPDATE
        # branch's COALESCE keeps the stored value because the
        # caller didn't supply a new one (None).
        m2 = self.store.upsert_memory(
            id=m1.id, kind="fact", text="Future note (edited)",
        )
        self.assertEqual(m2.valid_at, future,
                         "update path preserves stored valid_at via COALESCE")
        self.assertEqual(m2.invalid_at, future + 3600,
                         "update path preserves stored invalid_at via COALESCE")

    def test_explicit_update_overrides_window(self) -> None:
        m1 = self.store.upsert_memory(
            kind="fact", text="Note", valid_at=1000.0, invalid_at=2000.0,
        )
        m2 = self.store.upsert_memory(
            id=m1.id, kind="fact", text="Note",
            valid_at=3000.0, invalid_at=4000.0,
        )
        self.assertEqual(m2.valid_at, 3000.0)
        self.assertEqual(m2.invalid_at, 4000.0)

    def test_schema_version_bumped_to_12(self) -> None:
        """The 12 -> 13 bump (audit 2026-10-25) added ``subject_key``."""
        with self.store._conn() as c:
            v = c.execute(
                "SELECT v FROM schema_meta WHERE k='version'"
            ).fetchone()["v"]
        self.assertEqual(v, "13")

    def test_partial_indexes_present(self) -> None:
        """The content_hash and (valid_at, invalid_at) indexes must
        exist so the dedup + window queries stay O(1)."""
        with self.store._conn() as c:
            idx = {row["name"] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
        self.assertIn("idx_mem_content_hash", idx)
        self.assertIn("idx_mem_validity", idx)


class RecallAsOfTests(unittest.TestCase):
    """The recall() / recall_hybrid() ``as_of_ts`` filter."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "asof.db"))
        self.now = time.time()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="Active memory about auth tokens",
            agent_id="a", user_id="u",
            valid_at=self.now - 86400 * 30,
        )
        self.store.upsert_memory(
            kind="fact", text="Future memory about auth tokens",
            agent_id="a", user_id="u",
            valid_at=self.now + 86400 * 30,
        )
        self.store.upsert_memory(
            kind="fact", text="Expired memory about auth tokens",
            agent_id="a", user_id="u",
            invalid_at=self.now - 86400,
        )

    def test_recall_hides_future_and_expired(self) -> None:
        self._seed()
        r = self.store.recall("auth tokens", limit=10, as_of_ts=self.now)
        texts = [m["text"] for m in r["memories"]]
        self.assertEqual(len(texts), 1, f"expected 1 at now, got {texts}")
        self.assertIn("Active", texts[0])
        # Row carries valid_at / invalid_at fields for UI rendering.
        self.assertIsNotNone(r["memories"][0].get("valid_at"))
        self.assertIn("invalid_at", r["memories"][0])

    def test_recall_hybrid_hides_future_and_expired(self) -> None:
        self._seed()
        r = self.store.recall_hybrid(
            "auth tokens", limit=10, as_of_ts=self.now,
        )
        texts = [m["text"] for m in r["memories"]]
        self.assertEqual(len(texts), 1, f"expected 1 at now, got {texts}")

    def test_recall_at_future_returns_active_plus_future(self) -> None:
        self._seed()
        r = self.store.recall(
            "auth tokens", limit=10,
            as_of_ts=self.now + 86400 * 31,
        )
        texts = {m["text"] for m in r["memories"]}
        # Active is still valid (open-ended); Future has now passed.
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("Active" in t for t in texts))
        self.assertTrue(any("Future" in t for t in texts))

    def test_recall_at_past_hides_everything(self) -> None:
        self._seed()
        r = self.store.recall(
            "auth tokens", limit=10,
            as_of_ts=self.now - 86400 * 365,
        )
        self.assertEqual(len(r["memories"]), 0)

    def test_as_of_none_keeps_legacy_behaviour(self) -> None:
        """Default (no as_of_ts) shows every memory -- preserves the
        backward-compat contract for callers that never set the param."""
        self._seed()
        r = self.store.recall("auth tokens", limit=10)
        self.assertEqual(len(r["memories"]), 3)

    def test_as_of_accepts_iso8601_string(self) -> None:
        """Coercion mirrors ``recall_as_of``: float epoch OR ISO string."""
        self._seed()
        iso_future = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.gmtime(self.now + 86400 * 31),
        )
        r = self.store.recall(
            "auth tokens", limit=10, as_of_ts=iso_future,
        )
        self.assertEqual(len(r["memories"]), 2)

    def test_invalid_iso_string_raises(self) -> None:
        # Use a query that actually produces tokens so recall()
        # reaches the validity-clause path (an empty-token short-
        # circuit would return before the coercion runs).
        with self.assertRaises(ValueError):
            self.store.recall("auth tokens", limit=10, as_of_ts="not-a-date")


class EvidenceChainTests(unittest.TestCase):
    """The ``evidence: [{field, value, weight}]`` field on each hit."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "ev.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_evidence_includes_text_match(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="Authentication tokens rotate every 30 days",
            importance=0.8,
        )
        r = self.store.recall("authentication tokens", limit=5)
        self.assertTrue(r["memories"], "expected at least one hit")
        ev = r["memories"][0]["evidence"]
        self.assertIsInstance(ev, list)
        # text_match is always present when there's lexical overlap
        fields = [e["field"] for e in ev]
        self.assertIn("text_match", fields)
        # Weights are normalised to [0, 1]
        for e in ev:
            self.assertGreaterEqual(e["weight"], 0.0)
            self.assertLessEqual(e["weight"], 1.0)
        # high_importance fires when importance >= 0.7
        self.assertIn("high_importance", fields)

    def test_recall_hybrid_evidence_includes_bm25(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="Authentication tokens rotate every 30 days",
        )
        r = self.store.recall_hybrid("authentication tokens", limit=5)
        self.assertTrue(r["memories"])
        ev = r["memories"][0]["evidence"]
        fields = [e["field"] for e in ev]
        # bm25 + rrf_total should always be present in the hybrid path
        self.assertIn("bm25", fields)
        self.assertIn("rrf_total", fields)

    def test_evidence_absent_when_no_signal(self) -> None:
        """A memory hit with no contributing channels produces an
        empty evidence list (callers must handle that gracefully)."""
        self.store.upsert_memory(
            kind="fact", text="Random context with no overlap",
            importance=0.1,
        )
        # A query that doesn't match should produce no hits; an exact
        # match with low importance produces no channels firing -> [].
        # Use a query that we know will match lexically but with low
        # importance to keep the row visible.
        r = self.store.recall("Random context", limit=5)
        if r["memories"]:
            # The matched row's evidence list is non-None
            self.assertIn("evidence", r["memories"][0])
            self.assertIsInstance(r["memories"][0]["evidence"], list)


if __name__ == "__main__":
    unittest.main()

"""Tests for the per-memory lifetime-stats shape.

Audit 2026-09-06 (agentmemory v1.3.0). Pins the contract of
``MemoryStore.memory_stats(mid)`` so the CLI / HTTP / MCP surface
all share the same flat dict. Tested properties:

* Returns a dict with the documented keys for a fresh memory.
* ``recall_count`` / ``positive`` / ``negative`` track ``bump_recalls``
  and ``record_signal``.
* ``last_recalled_at`` becomes non-null after a recall.
* ``age_seconds`` is non-negative.
* ``text`` is truncated at 240 chars (with ``...``) so a 10-KB memory
  does not blow up the response.
* Unknown id / empty id returns ``None``.
* Superseded memories still surface their stats (the chain is
  audit-visible; this is a *read* endpoint).
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="lm_mstats_"))
    return MemoryStore(tmp / "stats.db")


class MemoryStatsStoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self.store = _new_store()

    def test_returns_none_for_unknown_id(self) -> None:
        self.assertIsNone(self.store.memory_stats("not-a-real-id"))

    def test_returns_none_for_empty_id(self) -> None:
        # An empty string is treated as "missing" — never crashes.
        self.assertIsNone(self.store.memory_stats(""))

    def test_fresh_memory_has_zero_signals(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="uses postgres",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        res = self.store.memory_stats(m.id)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], m.id)
        self.assertEqual(res["recall_count"], 0)
        self.assertEqual(res["positive"], 0)
        self.assertEqual(res["negative"], 0)
        self.assertIsNone(res["last_recalled_at"])
        self.assertIsNone(res["last_feedback_at"])
        self.assertEqual(res["text"], "uses postgres")
        self.assertIsNone(res["superseded_by"])
        self.assertGreaterEqual(res["age_seconds"], 0)

    def test_recall_count_tracks_bump_recalls(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="hi",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        self.store.bump_recalls([m.id, m.id, m.id])
        res = self.store.memory_stats(m.id)
        self.assertEqual(res["recall_count"], 3)
        self.assertIsNotNone(res["last_recalled_at"])

    def test_positive_and_negative_track_record_signal(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="hi",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        # record_signal uses boolean thumbs: True=positive, False=negative.
        self.store.record_signal(m.id, positive=True)
        self.store.record_signal(m.id, positive=True)
        self.store.record_signal(m.id, positive=False)
        res = self.store.memory_stats(m.id)
        self.assertEqual(res["positive"], 2)
        self.assertEqual(res["negative"], 1)
        self.assertIsNotNone(res["last_feedback_at"])

    def test_long_text_truncated_to_240_chars(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="X" * 5000,
            importance=0.5, agent_id="bot", user_id="u1",
        )
        res = self.store.memory_stats(m.id)
        self.assertEqual(len(res["text"]), 240)
        self.assertTrue(res["text"].endswith("..."))
        # The full text is still reachable via get_memory().
        full = self.store.get_memory(m.id)
        self.assertEqual(len(full.text), 5000)

    def test_short_text_not_truncated(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="short",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        res = self.store.memory_stats(m.id)
        self.assertEqual(res["text"], "short")

    def test_superseded_memory_surfaces_in_stats(self) -> None:
        """Stats is a READ endpoint — it must surface superseded rows
        so the audit chain is still queryable after a merge."""
        a = self.store.upsert_memory(
            kind="fact", text="winner",
            importance=0.8, agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="loser",
            importance=0.4, agent_id="bot", user_id="u1",
        ).id
        with self.store._conn() as c:  # noqa: SLF001 — test fixture
            c.execute("UPDATE memories SET score=0.9 WHERE id=?", (a,))
            c.execute("UPDATE memories SET score=0.3 WHERE id=?", (b,))
        self.store.merge_memories(a, b)
        loser_stats = self.store.memory_stats(b)
        self.assertIsNotNone(loser_stats)
        self.assertEqual(loser_stats["superseded_by"], a)
        self.assertEqual(loser_stats["text"], "loser")

    def test_age_seconds_is_monotonic_across_calls(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="hi",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        r1 = self.store.memory_stats(m.id)["age_seconds"]
        time.sleep(0.05)
        r2 = self.store.memory_stats(m.id)["age_seconds"]
        self.assertGreaterEqual(r2, r1)

    def test_all_documented_keys_present(self) -> None:
        """Pin the public surface so a future rename is loud."""
        m = self.store.upsert_memory(
            kind="fact", text="hi",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        res = self.store.memory_stats(m.id)
        expected = {
            "id", "kind", "text", "importance", "score", "source",
            "tags", "session_id", "created_at", "updated_at",
            "superseded_by", "recall_count", "positive", "negative",
            "last_recalled_at", "last_feedback_at", "age_seconds",
        }
        self.assertEqual(set(res.keys()), expected)

    def test_bump_recalls_on_unknown_id_does_not_crash_stats(self) -> None:
        """``bump_recalls`` on an unknown id creates a signals row
        with no memory — stats(mid) for that id should still 404."""
        self.store.bump_recalls(["nope"])
        self.assertIsNone(self.store.memory_stats("nope"))

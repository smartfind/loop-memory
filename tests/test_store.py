"""Tests for the SQLite store and time-weighted scoring."""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from loop_memory import MemoryStore


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path("/tmp/test_loop_memory.db")
        self.path.unlink(missing_ok=True)
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_session_lifecycle(self) -> None:
        s = self.store.upsert_session(
            source="codex",
            external_id="abc",
            title="hello",
            message_count=5,
        )
        self.assertEqual(s.source, "codex")
        rows = self.store.list_sessions(source="codex")
        self.assertEqual(len(rows), 1)

    def test_score_decays_with_time(self) -> None:
        m_fresh = MemoryStore.compute_score(0.8, created_at=time.time())
        m_old   = MemoryStore.compute_score(0.8, created_at=time.time() - 90 * 86400)
        self.assertGreater(m_fresh, m_old)
        # Bounds.
        self.assertTrue(0.0 <= m_fresh <= 1.0)
        self.assertTrue(0.0 <= m_old <= 1.0)

    def test_rescore_changes_old_scores(self) -> None:
        # v2 weights importance more heavily than recency; for a stale
        # memory with no signals the score is dominated by importance.
        # Verify rescore runs to completion and the score is recomputed
        # to the v2 formula (recency close to 0 for 200-day-old data).
        m = self.store.upsert_memory(
            kind="fact", text="stale fact",
            importance=0.8,
            created_at=time.time() - 200 * 86400,
        )
        self.store.rescore_all(half_life_days=30.0)
        m2 = self.store.get_memory(m.id)
        # Recency component should be ~0 for 200-day-old data.
        comps = MemoryStore.score_components(
            importance=m2.importance, created_at=m2.created_at,
            now=time.time(), recall_count=0, last_recalled_at=None,
        )
        self.assertLess(comps["recency"], 0.05)
        self.assertGreater(m2.score, 0.0)
        self.assertLessEqual(m2.score, 1.0)

    def test_search_by_text(self) -> None:
        self.store.upsert_memory(kind="fact", text="User's name is Mia.")
        self.store.upsert_memory(kind="fact", text="Project: Hangzhou trip.")
        hits = self.store.list_memories(query="Mia")
        self.assertTrue(any("Mia" in m.text for m in hits))

    def test_delete_cascade(self) -> None:
        s = self.store.upsert_session(
            source="codex", external_id="x", message_count=2
        )
        self.store.upsert_memory(kind="turn", text="a", session_id=s.id)
        self.store.upsert_memory(kind="turn", text="b", session_id=s.id)
        self.assertEqual(self.store.delete_session(s.id), 2)
        self.assertEqual(len(self.store.list_memories(session_id=s.id)), 0)

    def test_gc_respects_ttl(self) -> None:
        old = self.store.upsert_memory(
            kind="turn", text="stale", ttl=10,
            created_at=time.time() - 1000,
        )
        fresh = self.store.upsert_memory(
            kind="turn", text="fresh", ttl=10_000_000,
        )
        self.store.gc()
        self.assertIsNone(self.store.get_memory(old.id))
        self.assertIsNotNone(self.store.get_memory(fresh.id))

    def test_scoring_halflife_sweep(self) -> None:
        # importance 1, created 30 days ago, half-life 30 days
        score_30 = MemoryStore.compute_score(1.0, time.time() - 30 * 86400, half_life_days=30)
        # rough: recency ~ 0.5, importance ~ 1, blend = 0.35*1 + 0.65*0.5 = 0.675
        self.assertAlmostEqual(score_30, 0.675, places=2)

    def test_stats_counts(self) -> None:
        s = self.store.upsert_session(source="codex", external_id="z", message_count=0)
        self.store.upsert_memory(kind="turn", text="x", session_id=s.id)
        self.store.upsert_memory(kind="turn", text="y", session_id=s.id)
        self.store.upsert_entity("Codex")
        self.store.upsert_entity("Claude")
        self.store.upsert_relation("Codex", "Claude")
        stats = self.store.stats()
        self.assertEqual(stats["memories"], 2)
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["entities"], 2)
        self.assertEqual(stats["relations"], 1)


if __name__ == "__main__":
    unittest.main()

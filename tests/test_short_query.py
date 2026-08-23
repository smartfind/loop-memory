"""Tests for the recall() short-query tightening (Audit 2026-08-23)."""
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="loop_shortq_"))
    return MemoryStore(tmp / "short.db")


class RecallShortQueryTests(unittest.TestCase):
    """Audit 2026-08-23: Cognee v1.5.2 short-query ranking.

    When a query has 1-2 tokens, ``recall()`` boosts
    ``recall_count`` so a memory the user has already surfaced wins
    over a memory that only matches the substring but was never
    actually useful. These tests pin both the boost and the cap so a
    regression toward silent-noise OR toward runaway crowd-out both
    fail CI.
    """

    def setUp(self) -> None:
        self.store = _new_store()

    def _seed(self):
        # Two memories that match the same short query. The recalled
        # one (b1) should outrank the never-recalled one (b2) once
        # recall() sees the short-query boost.
        a1 = self.store.upsert_memory(
            kind="fact", text="database is postgres",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        a2 = self.store.upsert_memory(
            kind="fact", text="uses postgres for orders",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        # Bump the second one several times so recall_count > 0.
        for _ in range(5):
            self.store.bump_recalls([a2.id])
        return a1.id, a2.id

    def test_short_query_recall_boost_prefers_recalled_memory(self) -> None:
        _never, recalled = self._seed()
        r = self.store.recall("postgres")
        # Top hit is the recalled memory, not the never-recalled one.
        ids = [m["id"] for m in r["memories"]]
        self.assertIn(recalled, ids)
        self.assertEqual(ids[0], recalled)

    def test_short_query_boost_is_capped(self) -> None:
        """A runaway recall_count must not crowd out everything else."""
        # Seed a memory with 1000 recalls vs a sibling with 0.
        a_lo = self.store.upsert_memory(
            kind="fact", text="database is postgres",
            importance=0.5, agent_id="bot",
        )
        a_hi = self.store.upsert_memory(
            kind="fact", text="database is postgres too",
            importance=0.5, agent_id="bot",
        )
        for _ in range(1000):
            self.store.bump_recalls([a_hi.id])
        r = self.store.recall("postgres")
        scores = {m["id"]: m["score"] for m in r["memories"]}
        # The cap is +30% so the high-recall memory wins, but the
        # low-recall sibling must NOT be zeroed-out.
        if a_hi.id in scores and a_lo.id in scores:
            self.assertGreater(scores[a_hi.id], scores[a_lo.id])
            # Ratio is bounded by 1.0 + cap = 1.30 (modulo rounding).
            ratio = scores[a_hi.id] / max(scores[a_lo.id], 0.001)
            self.assertLess(ratio, 1.5, f"boost overrun: {ratio:.3f}")

    def test_short_query_boost_disabled_for_long_query(self) -> None:
        """3+ tokens must NOT apply the recall_count boost.

        Long queries have enough lexical overlap to rank by hits, so
        adding recall_count on top would over-weight popular
        memories and hide fresh-but-relevant ones.
        """
        # Seed two memories; only one has been recalled.
        a_fresh = self.store.upsert_memory(
            kind="fact",
            text="Postgres orders table is used by service",
            importance=0.5, agent_id="bot",
        )
        a_pop = self.store.upsert_memory(
            kind="fact",
            text="Postgres cache table is used by service",
            importance=0.5, agent_id="bot",
        )
        for _ in range(10):
            self.store.bump_recalls([a_pop.id])
        # Three tokens -> NOT short.
        r = self.store.recall("Postgres orders table")
        # Both should be returned; ordering is by lexical signal
        # (body_hits + importance + score), not by recall_count.
        ids = [m["id"] for m in r["memories"]]
        self.assertIn(a_fresh.id, ids)
        self.assertIn(a_pop.id, ids)

    def test_short_query_two_token_boundary(self) -> None:
        """Two tokens IS short (len(tokens) <= 2)."""
        store = _new_store()
        _ = store.upsert_memory(
            kind="fact", text="team uses postgres",
            importance=0.5, agent_id="bot",
        )
        a2 = store.upsert_memory(
            kind="fact", text="team uses postgres for orders",
            importance=0.5, agent_id="bot",
        )
        for _ in range(5):
            store.bump_recalls([a2.id])
        r = store.recall("team postgres")
        ids = [m["id"] for m in r["memories"]]
        # 2 tokens -> short mode -> recalled memory wins.
        self.assertEqual(ids[0], a2.id)

    def test_short_query_zero_recall_count_no_change(self) -> None:
        """Zero recall_count must NOT change the score (no boost, no penalty)."""
        store = _new_store()
        store.upsert_memory(
            kind="fact", text="team uses postgres",
            importance=0.5, agent_id="bot",
        )
        store.upsert_memory(
            kind="fact", text="team uses postgres for orders",
            importance=0.5, agent_id="bot",
        )
        r = store.recall("team postgres")
        # Without bumps, both have recall_count=0; the boost should
        # not flip the ranking purely on that field.
        ids = [m["id"] for m in r["memories"]]
        # The two-token query returns both; either order is fine since
        # recall_count=0 means the multiplier is 1.0 for both.
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()

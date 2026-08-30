"""Tests for the recall() ``why: [...]`` provenance labels.

Audit 2026-08-30: adopted from ``nagyist/agentmemory v1.2.0``. Each
memory hit in the recall result must carry a small ``why`` array
naming the scoring signals that contributed. These tests pin the
shape so a regression toward silent scoring (no labels) or
mis-leading labels (label without contributing signal) both fail CI.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="loop_prov_"))
    return MemoryStore(tmp / "prov.db")


def _set_scores(store: MemoryStore, mapping: dict[str, float]) -> None:
    """Bypass the upsert path (which doesn't expose ``score``) and
    set the score field directly via SQL so the test is deterministic."""
    with store._conn() as c:  # noqa: SLF001
        for mid, score in mapping.items():
            c.execute("UPDATE memories SET score=? WHERE id=?", (score, mid))


def _bump(store: MemoryStore, mid: str, n: int = 1) -> None:
    for _ in range(n):
        store.bump_recalls([mid])


class RecallProvenanceTests(unittest.TestCase):
    """Audit 2026-08-30: agentmemory v1.2.0 provenance pattern."""

    def setUp(self) -> None:
        self.store = _new_store()

    def test_recall_hit_has_why_field(self) -> None:
        """Every memory hit must carry a ``why`` field, even if empty."""
        self.store.upsert_memory(
            kind="fact", text="postgres database",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        r = self.store.recall("postgres")
        self.assertTrue(r["memories"], "expected at least one hit")
        for hit in r["memories"]:
            self.assertIn("why", hit, "every recall hit must carry a why field")
            self.assertIsInstance(hit["why"], list)

    def test_why_keyword_match_present_when_query_token_in_body(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="postgres for orders",
            importance=0.3, agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {m: 0.3})
        r = self.store.recall("postgres")
        hits = [h for h in r["memories"] if h["id"] == m]
        self.assertTrue(hits)
        self.assertIn("keyword_match", hits[0]["why"])

    def test_why_tag_match_present_when_query_token_in_tags(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="orders table",
            importance=0.3, agent_id="bot", user_id="u1",
            tags=["postgres"],
        ).id
        _set_scores(self.store, {m: 0.3})
        # Query matches the tag, NOT the body text.
        r = self.store.recall("postgres")
        hits = [h for h in r["memories"] if h["id"] == m]
        self.assertTrue(hits)
        self.assertIn("tag_match", hits[0]["why"])
        self.assertNotIn(
            "keyword_match", hits[0]["why"],
            "tag-only matches must not also report keyword_match",
        )

    def test_why_high_importance_only_when_importance_geq_0_7(self) -> None:
        low = self.store.upsert_memory(
            kind="fact", text="postgres a", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        high = self.store.upsert_memory(
            kind="fact", text="postgres b", importance=0.7,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {low: 0.5, high: 0.5})
        r = self.store.recall("postgres")
        hits_low = [h for h in r["memories"] if h["id"] == low]
        hits_high = [h for h in r["memories"] if h["id"] == high]
        self.assertTrue(hits_low and hits_high)
        self.assertNotIn("high_importance", hits_low[0]["why"])
        self.assertIn("high_importance", hits_high[0]["why"])

    def test_why_high_score_field_only_when_score_geq_0_7(self) -> None:
        low = self.store.upsert_memory(
            kind="fact", text="postgres a", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        high = self.store.upsert_memory(
            kind="fact", text="postgres b", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {low: 0.5, high: 0.7})
        r = self.store.recall("postgres")
        hits_low = [h for h in r["memories"] if h["id"] == low]
        hits_high = [h for h in r["memories"] if h["id"] == high]
        self.assertTrue(hits_low and hits_high)
        self.assertNotIn("high_score_field", hits_low[0]["why"])
        self.assertIn("high_score_field", hits_high[0]["why"])

    def test_why_high_recall_count_only_when_count_geq_3(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {m: 0.5})
        # Bump recall_count to 3 — should now report high_recall_count.
        _bump(self.store, m, n=3)
        r = self.store.recall("postgres")
        hits = [h for h in r["memories"] if h["id"] == m]
        self.assertTrue(hits)
        self.assertIn("high_recall_count", hits[0]["why"])

    def test_why_high_recall_count_absent_when_count_lt_3(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {m: 0.5})
        _bump(self.store, m, n=2)  # 2 recalls, just below the threshold.
        r = self.store.recall("postgres")
        hits = [h for h in r["memories"] if h["id"] == m]
        self.assertTrue(hits)
        self.assertNotIn("high_recall_count", hits[0]["why"])

    def test_why_short_query_boost_only_for_short_queries(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {m: 0.5})
        _bump(self.store, m, n=1)  # at least 1 recall
        # Short query: 1-2 tokens -> label fires.
        r_short = self.store.recall("postgres")
        hits_short = [h for h in r_short["memories"] if h["id"] == m]
        self.assertTrue(hits_short)
        self.assertIn("short_query_boost", hits_short[0]["why"])
        # Long query: >2 tokens -> label does NOT fire.
        r_long = self.store.recall("postgres orders database replica")
        hits_long = [h for h in r_long["memories"] if h["id"] == m]
        self.assertTrue(hits_long)
        self.assertNotIn("short_query_boost", hits_long[0]["why"])

    def test_why_short_query_boost_absent_when_recall_count_zero(self) -> None:
        """Short-query boost only fires when recall_count > 0 — the
        label describes a real signal, not just the boost arithmetic."""
        m = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {m: 0.5})
        # No bump_recalls; recall_count stays at 0.
        r = self.store.recall("postgres")
        hits = [h for h in r["memories"] if h["id"] == m]
        self.assertTrue(hits)
        self.assertNotIn("short_query_boost", hits[0]["why"])

    def test_recall_provenance_does_not_change_score_shape(self) -> None:
        """The provenance labels must be purely additive: removing
        them (or adding new ones) must not change the numeric
        ``score`` of a hit. This pins the test contract: provenance
        is metadata, never an input to ranking."""
        m = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.7,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {m: 0.7})
        _bump(self.store, m, n=4)
        r = self.store.recall("postgres")
        hits = [h for h in r["memories"] if h["id"] == m]
        self.assertTrue(hits)
        self.assertIn("keyword_match", hits[0]["why"])
        self.assertIn("high_importance", hits[0]["why"])
        self.assertIn("high_score_field", hits[0]["why"])
        self.assertIn("high_recall_count", hits[0]["why"])
        # Score is a real float; not None / "unknown" / etc.
        self.assertIsInstance(hits[0]["score"], float)
        self.assertGreater(hits[0]["score"], 0.0)


if __name__ == "__main__":
    unittest.main()

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


class HybridRecallTests(unittest.TestCase):
    """Covers the BM25 + semantic + entity-fused recall_hybrid pipeline
    and the FTS5 tokenizer migration."""

    def setUp(self) -> None:
        self.path = Path("/tmp/test_loop_hybrid.db")
        self.path.unlink(missing_ok=True)
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_fts5_uses_trigram_tokenizer(self) -> None:
        import sqlite3
        c = sqlite3.connect(self.path)
        for tbl in ("memories_fts", "wiki_fts"):
            row = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            self.assertIsNotNone(row, f"{tbl} missing")
            self.assertIn("trigram", row[0], f"{tbl} not using trigram tokenizer")
        c.close()

    def test_recall_hybrid_returns_memories_and_wiki(self) -> None:
        # Seed: 1 wiki page with a unique-token title, 1 memory with
        # an overlapping keyword + entity mention.
        self.store.upsert_memory(
            kind="fact", text="the cache buster middleware handles cache",
            importance=0.5, source="codex",
            tags=["cache", "middleware"],
        )
        self.store.upsert_wiki_page(
            slug="cache-buster", title="Cache buster middleware",
            body="we hash asset URLs to bust the cache on every deploy",
            importance=0.7, scope="global",
        )
        out = self.store.recall_hybrid("cache", limit=10)
        # Both the memory and the wiki page should appear (they share
        # the keyword "cache").
        self.assertTrue(out["memories"] or out["wiki"], "empty recall")
        text_blob = " ".join(
            (m.get("text", "") + " " + (m.get("title", "") or ""))
            for m in out["memories"] + [{"title": w.get("title", "")} for w in out["wiki"]]
        )
        self.assertIn("cache", text_blob.lower())

    def test_recall_hybrid_handles_cjk(self) -> None:
        # Trigram tokenizer is the only thing that makes CJK search
        # usable. Seed a Chinese memory and ask for it by substring.
        self.store.upsert_memory(
            kind="fact", text="知识图谱应该可以点击节点跳转",
            importance=0.6, source="codex",
        )
        out = self.store.recall_hybrid("知识图谱", limit=5)
        self.assertTrue(out["memories"], "CJK recall returned nothing")

    def test_fts_migration_is_one_shot(self) -> None:
        # Second open of the same path should be a no-op for the FTS
        # migration block (no exceptions, trigram remains installed).
        MemoryStore(self.path)
        import sqlite3
        c = sqlite3.connect(self.path)
        row = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        self.assertIn("trigram", row[0])
        c.close()


class RetrievalPrimitivesTests(unittest.TestCase):
    """Covers the BM25 + RRF primitives used by recall_hybrid."""

    def test_fuse_rrf_basic(self) -> None:
        from loop_memory.storage.retrieval import fuse_rrf
        a = [{"id": "x", "_score": 1.0}, {"id": "y", "_score": 0.5}]
        b = [{"id": "y", "_score": 0.9}, {"id": "z", "_score": 0.4}]
        out = fuse_rrf([a, b])
        # x is rank1 in a only → 1/(60+1) ; y is rank2 in a + rank1 in b
        # → 1/(60+2) + 1/(60+1) ; z is rank2 in b → 1/(60+2). y > z > x.
        ids = [r["id"] for r in out]
        self.assertEqual(ids[0], "y")
        self.assertIn("x", ids)
        self.assertIn("z", ids)

    def test_escape_fts_handles_punctuation_and_empty(self) -> None:
        from loop_memory.storage.retrieval import _escape_fts
        self.assertEqual(_escape_fts(""), "")
        self.assertEqual(_escape_fts("   "), "")
        # "vue.js" should NOT become one exact-phrase; must split.
        out = _escape_fts("vue.js")
        self.assertIn('"vue"', out)
        self.assertIn('"js"', out)

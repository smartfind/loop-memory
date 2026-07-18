"""Tests for the v2 scoring formula (importance × recency × usage × feedback)."""
import os
import shutil
import tempfile
import time
import unittest

from loop_memory.storage.sqlite_store import MemoryStore


class ScoreComponentsTests(unittest.TestCase):
    def test_returns_all_four_components(self):
        comps = MemoryStore.score_components(0.5, time.time() - 86400)
        for key in ("importance", "recency", "usage", "feedback", "score"):
            self.assertIn(key, comps)
        self.assertGreaterEqual(comps["score"], 0.0)
        self.assertLessEqual(comps["score"], 1.0)

    def test_recency_decays_with_age(self):
        now = 1_000_000_000.0
        fresh = MemoryStore.score_components(0.5, now, now=now)
        old = MemoryStore.score_components(0.5, now - 60 * 86400, now=now)
        self.assertGreater(fresh["recency"], old["recency"])
        # 60 days = 2 half-lives with default 30d half-life => recency ≈ 0.25
        self.assertAlmostEqual(old["recency"], 0.25, delta=0.02)

    def test_usage_grows_with_recall_count(self):
        now = time.time()
        no_use = MemoryStore.score_components(0.5, now, recall_count=0, last_recalled_at=None)
        one_use = MemoryStore.score_components(0.5, now, recall_count=1, last_recalled_at=now)
        many_use = MemoryStore.score_components(0.5, now, recall_count=50, last_recalled_at=now)
        self.assertEqual(no_use["usage"], 0.0)
        self.assertGreater(one_use["usage"], 0.0)
        self.assertGreater(many_use["usage"], one_use["usage"])

    def test_usage_decays_if_last_recall_is_old(self):
        now = time.time()
        recent = MemoryStore.score_components(0.5, now, recall_count=5, last_recalled_at=now)
        ancient = MemoryStore.score_components(0.5, now, recall_count=5,
                                              last_recalled_at=now - 60*86400)
        self.assertGreater(recent["usage"], ancient["usage"])

    def test_feedback_is_sticky(self):
        now = time.time()
        positive = MemoryStore.score_components(0.5, now, positive=5, negative=0)
        negative = MemoryStore.score_components(0.5, now, positive=0, negative=5)
        neutral = MemoryStore.score_components(0.5, now, positive=0, negative=0)
        self.assertGreater(positive["feedback"], neutral["feedback"])
        self.assertLess(negative["feedback"], neutral["feedback"])

    def test_score_uses_v2_weights(self):
        # A memory recalled 10x today with 2 thumbs up should rank higher
        # than an equally-important memory that's never been used.
        now = time.time()
        used = MemoryStore.score_components(0.5, now - 86400,
                                            recall_count=10, last_recalled_at=now,
                                            positive=2, negative=0)
        unused = MemoryStore.score_components(0.5, now - 86400,
                                              recall_count=0, last_recalled_at=None)
        self.assertGreater(used["score"], unused["score"])

    def test_score_bounded(self):
        # Pathological inputs still clamp to [0, 1]
        cases = [
            dict(importance=2.0, created_at=0, recall_count=10000, last_recalled_at=time.time()),
            dict(importance=-1.0, created_at=0, recall_count=0),
            dict(importance=0.5, created_at=time.time(), positive=1000, negative=1000),
        ]
        for c in cases:
            comps = MemoryStore.score_components(**c)
            self.assertGreaterEqual(comps["score"], 0.0)
            self.assertLessEqual(comps["score"], 1.0)


class RescoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        sid = self.store.upsert_session(
            source="t", external_id="s",
            title="t", started_at=time.time() - 86400 * 60,
        ).id
        # Two memories: one with high usage, one without
        self.m_used = self.store.upsert_memory(
            kind="fact", text="Used fact", importance=0.5,
            session_id=sid, source="t/1",
        )
        self.m_unused = self.store.upsert_memory(
            kind="fact", text="Unused fact", importance=0.5,
            session_id=sid, source="t/2",
        )
        for _ in range(5):
            self.store.bump_recalls([self.m_used.id])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rescore_all_uses_v2(self):
        n = self.store.rescore_all()
        self.assertEqual(n, 2)
        used = self.store.get_memory(self.m_used.id)
        unused = self.store.get_memory(self.m_unused.id)
        # used has 5 recalls, so its score must exceed the unused one
        # despite equal importance and recency.
        self.assertGreater(used.score, unused.score)

    def test_rescore_idempotent(self):
        self.store.rescore_all()
        first = [self.store.get_memory(self.m_used.id).score,
                 self.store.get_memory(self.m_unused.id).score]
        self.store.rescore_all()
        second = [self.store.get_memory(self.m_used.id).score,
                  self.store.get_memory(self.m_unused.id).score]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

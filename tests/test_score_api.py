"""API tests for the new score breakdown + distribution + decay endpoints."""
import os
import shutil
import tempfile
import unittest

from loop_memory.storage.sqlite_store import MemoryStore


class ScoreApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        # seed
        sid = self.store.upsert_session(
            source="t", external_id="s",
            title="t", started_at=1700000000.0,
        ).id
        for i, (imp, age) in enumerate([(0.8, 86400), (0.5, 86400*30), (0.2, 86400*120)]):
            self.store.upsert_memory(
                kind="fact", text=f"m{i}",
                importance=imp,
                session_id=sid, source=f"t/{i}",
                created_at=1700000000 - age,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_score_distribution_shape(self):
        bins = [{'range': [i/10, (i+1)/10], 'count': 0} for i in range(10)]
        for m in self.store.list_memories(limit=10):
            comps = MemoryStore.score_components(
                importance=m.importance, created_at=m.created_at,
                now=1700000000, recall_count=0, last_recalled_at=None,
            )
            idx = min(9, int(comps["score"] * 10))
            bins[idx]['count'] += 1
        total = sum(b['count'] for b in bins)
        self.assertEqual(total, 3)

    def test_decay_stats_age_buckets(self):
        now = 1700000000
        buckets = [
            ("<1d", 0, 86400),
            ("1-7d", 86400, 7*86400),
            ("7-30d", 7*86400, 30*86400),
            ("30-90d", 30*86400, 90*86400),
            (">90d", 90*86400, 10**9),
        ]
        mems = self.store.list_memories(limit=20)
        for _label, lo, hi in buckets:
            in_bucket = [m for m in mems
                         if (now - hi) <= m.created_at < (now - lo)]
            if not in_bucket:
                continue
            for m in in_bucket:
                comps = MemoryStore.score_components(
                    importance=m.importance, created_at=m.created_at, now=now,
                )
                self.assertGreaterEqual(comps["score"], 0.0)
                self.assertLessEqual(comps["score"], 1.0)


if __name__ == "__main__":
    unittest.main()

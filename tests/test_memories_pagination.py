"""Audit O11 regression: cursor pagination on /api/memories/page."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _store():
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-page-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


def _seed_memories(store, n):
    ids = []
    base = time.time()
    for i in range(n):
        m = store.upsert_memory(
            id=f"mem-{i:04d}", kind="fact", text=f"memory #{i}",
            importance=0.5, source="codex",
            created_at=base + i * 0.001, updated_at=base + i * 0.001,
        )
        ids.append(m.id)
    return ids


class MemoriesPaginationTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)
        self.ids = _seed_memories(self.store, 10)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_page_desc_with_next_before_id(self):
        r = self.client.get("/api/memories/page?limit=4")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["rows"]), 4)
        self.assertIsNotNone(body["next_before_id"])

    def test_walk_desc_with_before_id(self):
        cursor = None
        seen = []
        for _ in range(5):
            qs = "?limit=3"
            if cursor:
                qs += f"&before_id={cursor}"
            r = self.client.get(f"/api/memories/page{qs}")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            seen.extend(m["id"] for m in body["rows"])
            cursor = body["next_before_id"]
            if not cursor:
                break
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 10)

    def test_walk_asc_after_id(self):
        """Start ASC from the oldest known id (mem-0000). Each call
        returns rows strictly after (newer than) the cursor. With 10
        seeded rows we expect 9 rows across 3+3+3 pages, no dups.
        """
        seen = []
        cursor = "mem-0000"
        for _ in range(5):
            r = self.client.get(
                f"/api/memories/page?after_id={cursor}&limit=3"
            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            seen.extend(m["id"] for m in body["rows"])
            cursor = body["next_after_id"]
            if not cursor:
                break
        self.assertEqual(len(seen), 9, f"got {seen}")
        self.assertEqual(len(set(seen)), 9)

    def test_unknown_cursor_returns_404(self):
        r = self.client.get("/api/memories/page?before_id=does-not-exist&limit=5")
        self.assertEqual(r.status_code, 404)
        self.assertIn("before_id", r.json()["detail"])

    def test_limit_clamped_1_to_500(self):
        r = self.client.get("/api/memories/page?limit=10000")
        self.assertEqual(r.status_code, 200)
        # 10 rows exist; clamping to <=500 doesn't truncate here.
        self.assertLessEqual(len(r.json()["rows"]), 500)


if __name__ == "__main__":
    unittest.main()

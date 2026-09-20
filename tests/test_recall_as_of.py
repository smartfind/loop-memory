"""Tests for ``MemoryStore.recall_as_of()`` (audit 2026-09-20).

Bi-temporal ``as_of`` recall (loomcycle v1.33-v1.49, Apache-2.0,
2026-09-15). The store-level contract: ``recall_as_of(query,
as_of_ts, ...)`` returns only memories / wiki pages that were
*true* at ``as_of_ts`` — a memory created after that moment is
invisible, and a memory superseded before that moment is
correctly treated as the loser's text (i.e. the answer that was
true *then*).
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_asof_"))
    return MemoryStore(str(tmp / "asof.db")), tmp


class RecallAsOfStoreTests(unittest.TestCase):
    """Audit 2026-09-20: bi-temporal recall (loomcycle v1.33+)."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _now_epoch(self) -> float:
        return time.time()

    def _iso(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    # --- as_of basic semantics ---------------------------------------

    def test_as_of_before_any_memory_returns_empty(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        # A timestamp safely before any insert.
        r = self.store.recall_as_of("postgres", 1.0)
        self.assertEqual(r["memories"], [])
        self.assertEqual(r["wiki"], [])

    def test_as_of_after_memory_returns_it(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        future = self._now_epoch() + 1000
        r = self.store.recall_as_of("postgres", future)
        self.assertTrue(r["memories"], msg=r)
        self.assertIn("postgres", r["memories"][0]["abstract"].lower())

    def test_as_of_accepts_iso_string(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="redis queue patterns",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        future_iso = self._iso(self._now_epoch() + 1000)
        r = self.store.recall_as_of("redis", future_iso)
        self.assertTrue(r["memories"], msg=r)

    def test_as_of_accepts_iso_with_z_suffix(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="kafka offset mgmt",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        future = self._iso(self._now_epoch() + 1000).replace("+00:00", "Z")
        r = self.store.recall_as_of("kafka", future)
        self.assertTrue(r["memories"], msg=r)

    def test_as_of_invalid_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.store.recall_as_of("anything", "not-a-date")

    # --- as_of + supersession chain -----------------------------------

    def test_as_of_filters_out_superseded_memory(self) -> None:
        """Memory written at t0, superseded at t1. as_of < t1 must
        return the memory; as_of > t1 must hide it."""
        a = self.store.upsert_memory(
            kind="fact", text="postgres uses 8.0",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        # Force ``a``'s ``created_at`` to a known past moment so the
        # bi-temporal predicate is deterministic.
        with self.store._conn() as c:
            c.execute(
                "UPDATE memories SET created_at=? WHERE id=?",
                (1_000_000.0, a.id),
            )
        time.sleep(0.05)
        # Now write a replacement at t1 ≈ now+5s and supersede a.
        b = self.store.upsert_memory(
            kind="fact", text="postgres uses 17.0",
            importance=0.9, agent_id="bot", user_id="u1",
        )
        with self.store._conn() as c:
            c.execute(
                "UPDATE memories SET updated_at=?, created_at=? WHERE id=?",
                (time.time(), time.time(), b.id),
            )
        self.store.merge_memories(a_id=a.id, b_id=b.id)
        # as_of between t0 (1_000_000) and now → both visible (a
        # was still current at that moment).
        r_mid = self.store.recall_as_of("postgres", 1_000_000.0 + 60)
        self.assertTrue(r_mid["memories"], msg=r_mid)

    def test_as_of_returns_empty_for_empty_store(self) -> None:
        r = self.store.recall_as_of("anything", 0.0)
        self.assertEqual(r["memories"], [])
        self.assertEqual(r["wiki"], [])
        self.assertEqual(r["entities"], [])
        # ``tokens`` is non-empty (we tokenize the query); only the
        # result lists are empty because the store has no rows.
        self.assertEqual(r["tokens"], ["anything"])

    # --- as_of + wiki pages -------------------------------------------

    def test_as_of_filters_wiki_by_updated_at(self) -> None:
        page = self.store.upsert_wiki_page(
            slug="alpha", title="Alpha",
            body="alpha body", summary="alpha summary",
            importance=0.7,
        )
        # Force updated_at into the deep past.
        with self.store._conn() as c:
            c.execute(
                "UPDATE wiki_pages SET updated_at=? WHERE id=?",
                (1_000_000.0, page["id"]),
            )
        # Past window (before the page was rewritten).
        r = self.store.recall_as_of("alpha", 999_999.0)
        self.assertEqual(r["wiki"], [])
        # Future window returns the page.
        r = self.store.recall_as_of("alpha", 2_000_000.0)
        self.assertTrue(r["wiki"], msg=r)


if __name__ == "__main__":
    unittest.main()

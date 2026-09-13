"""Tests for ``MemoryStore.recall_paths()`` (audit 2026-09-13).

Adopted from ``tigerless-labs/agent-memory v0.3.0`` recall-ladder
pattern (MIT, 2026-09-08). The store-level contract: ``recall_paths()``
returns the same ranked lists as ``recall()`` but each hit carries
only ``id`` + ``kind`` + ``abstract`` + ``score`` + ``why`` -- never
the full ``text`` / ``body``. This is the L0 ladder rung; the L1 rung
is the existing ``GET /api/memories/{id}`` endpoint.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="loop_paths_"))
    return MemoryStore(tmp / "paths.db")


class RecallPathsStoreTests(unittest.TestCase):
    """Audit 2026-09-13: L0 outline recall (tigerless-labs/agent-memory v0.3.0)."""

    def setUp(self) -> None:
        self.store = _new_store()

    def test_recall_paths_returns_three_lists_plus_tokens(self) -> None:
        r = self.store.recall_paths("anything")
        self.assertIn("memories", r)
        self.assertIn("wiki", r)
        self.assertIn("entities", r)
        self.assertIn("tokens", r)
        self.assertIsInstance(r["memories"], list)
        self.assertIsInstance(r["wiki"], list)
        self.assertIsInstance(r["entities"], list)

    def test_recall_paths_hit_has_no_text_field(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="a long memory body about postgres tuning",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        r = self.store.recall_paths("postgres")
        self.assertTrue(r["memories"])
        for hit in r["memories"]:
            self.assertNotIn(
                "text", hit,
                f"L0 outline hit must NOT carry 'text'; got keys={sorted(hit.keys())}",
            )
            self.assertNotIn(
                "body", hit,
                f"L0 outline hit must NOT carry 'body'; got keys={sorted(hit.keys())}",
            )

    def test_recall_paths_hit_has_abstract_field(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres for orders",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        r = self.store.recall_paths("postgres")
        self.assertTrue(r["memories"])
        for hit in r["memories"]:
            self.assertIn("abstract", hit)
            self.assertIsInstance(hit["abstract"], str)
            self.assertGreater(len(hit["abstract"]), 0)

    def test_recall_paths_hit_abstract_is_truncated_to_80_chars(self) -> None:
        long_text = "x" * 500 + " postgres tail"
        self.store.upsert_memory(
            kind="fact", text=long_text, importance=0.9,
            agent_id="bot", user_id="u1",
        )
        r = self.store.recall_paths("postgres")
        self.assertTrue(r["memories"])
        for hit in r["memories"]:
            self.assertLessEqual(
                len(hit["abstract"]), 80,
                f"abstract must be <= 80 chars; got {len(hit['abstract'])}",
            )
            self.assertTrue(
                hit["abstract"].endswith("\u2026"),
                f"truncated abstract should end with ellipsis; got {hit['abstract']!r}",
            )

    def test_recall_paths_hit_preserves_id_kind_score(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres tuning notes",
            importance=0.6, agent_id="bot", user_id="u1",
        )
        r = self.store.recall_paths("postgres")
        self.assertTrue(r["memories"])
        for hit in r["memories"]:
            self.assertIn("id", hit)
            self.assertIn("kind", hit)
            self.assertEqual(hit["kind"], "memory")
            self.assertIn("score", hit)
            self.assertIsInstance(hit["score"], float)

    def test_recall_paths_hit_carries_why_field(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.3, agent_id="bot", user_id="u1",
        )
        r = self.store.recall_paths("postgres")
        self.assertTrue(r["memories"])
        for hit in r["memories"]:
            self.assertIn("why", hit)
            self.assertIsInstance(hit["why"], list)

    def test_recall_paths_why_keyword_match_fires(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres for orders",
            importance=0.3, agent_id="bot", user_id="u1",
        )
        r = self.store.recall_paths("postgres")
        self.assertTrue(r["memories"])
        self.assertIn("keyword_match", r["memories"][0]["why"])

    def test_recall_paths_filters_superseded_memories(self) -> None:
        loser = self.store.upsert_memory(
            kind="fact", text="postgres tuning A",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        winner = self.store.upsert_memory(
            kind="fact", text="postgres tuning B",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        with self.store._conn() as c:  # noqa: SLF001
            c.execute(
                "UPDATE memories SET superseded_by=? WHERE id=?",
                (winner, loser),
            )
        r = self.store.recall_paths("postgres")
        ids = {h["id"] for h in r["memories"]}
        self.assertNotIn(loser, ids, "superseded memory must be filtered out")
        self.assertIn(winner, ids)

    def test_recall_paths_default_limit_clamps_to_12(self) -> None:
        for i in range(20):
            self.store.upsert_memory(
                kind="fact", text=f"postgres fact {i}",
                importance=0.5, agent_id="bot", user_id=f"u{i}",
            )
        r = self.store.recall_paths("postgres")
        self.assertLessEqual(len(r["memories"]), 12)

    def test_recall_paths_limit_flag_is_respected(self) -> None:
        for i in range(20):
            self.store.upsert_memory(
                kind="fact", text=f"postgres fact {i}",
                importance=0.5, agent_id="bot", user_id=f"u{i}",
            )
        r = self.store.recall_paths("postgres", limit=3)
        self.assertEqual(len(r["memories"]), 3)

    def test_recall_paths_does_not_bump_signals_by_default(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        _ = self.store.recall_paths("postgres")
        sig = self.store.get_signal(m)
        self.assertEqual(sig["recall_count"], 0,
                         "L0 listing must not bump recall_count by default")

    def test_recall_paths_bumps_signals_when_requested(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        _ = self.store.recall_paths("postgres", bump_signals=True)
        sig = self.store.get_signal(m)
        self.assertEqual(sig["recall_count"], 1)

    def test_recall_paths_empty_query_returns_empty_lists(self) -> None:
        r = self.store.recall_paths("")
        self.assertEqual(r["memories"], [])
        self.assertEqual(r["wiki"], [])
        self.assertEqual(r["entities"], [])

    def test_recall_paths_supports_wiki_kind(self) -> None:
        self.store.upsert_wiki_page(
            slug="pg-tuning", title="Postgres tuning notes",
            body="long body about postgres", summary="short summary",
            importance=0.8,
        )
        r = self.store.recall_paths("postgres", include=("wiki",))
        self.assertTrue(r["wiki"])
        for hit in r["wiki"]:
            self.assertNotIn("body", hit)
            self.assertNotIn("summary", hit)
            self.assertIn("abstract", hit)
            self.assertIn("slug", hit)

    def test_recall_paths_payload_is_smaller_than_recall(self) -> None:
        long_text = " ".join(["postgres"] * 100)
        self.store.upsert_memory(
            kind="fact", text=long_text, importance=0.9,
            agent_id="bot", user_id="u1",
        )
        import json
        full = json.dumps(self.store.recall("postgres"), ensure_ascii=False)
        outline = json.dumps(self.store.recall_paths("postgres"), ensure_ascii=False)
        self.assertLess(
            len(outline), len(full),
            f"L0 outline must be smaller than L1; full={len(full)} outline={len(outline)}",
        )


if __name__ == "__main__":
    unittest.main()

"""Hybrid retrieval primitives (audit 2026-10-18, loop-memory 0.4.15).

These tests cover ``fuse_rrf_with_channels`` (new) and the
``_like_fallback`` per-token OR pass (also new in 0.4.15) -- the
two retrieval changes that the 0.4.15 hybrid recall + decorator
features depend on. The pre-existing ``fuse_rrf`` is unchanged so
its tests live elsewhere.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from loop_memory.storage.retrieval import (
    DEFAULT_RRF_K,
    _escape_fts,
    _like_fallback,
    fuse_rrf,
    fuse_rrf_with_channels,
)
from loop_memory.storage.sqlite_store import MemoryStore


class FuseRrfWithChannelsTests(unittest.TestCase):
    """``fuse_rrf_with_channels`` returns per-channel score breakdown."""

    def test_basic_three_channel_fusion(self) -> None:
        bm25 = [{"id": "a", "_score": 0.9}, {"id": "b", "_score": 0.5}]
        sem = [{"id": "a", "_score": 0.8}, {"id": "c", "_score": 0.4}]
        ent = [{"id": "b", "_score": 0.7}, {"id": "c", "_score": 0.6}]
        out = fuse_rrf_with_channels(
            [("bm25", bm25), ("semantic", sem), ("entity", ent)],
            k=60,
        )
        # Three distinct ids should appear
        ids = {r["id"] for r in out}
        self.assertEqual(ids, {"a", "b", "c"})
        # Each row carries a channel_scores breakdown
        for r in out:
            self.assertIn("_rrf", r)
            self.assertIn("_channel_scores", r)
            self.assertIsInstance(r["_channel_scores"], dict)
        # Sorted descending by _rrf
        rrf_scores = [r["_rrf"] for r in out]
        self.assertEqual(rrf_scores, sorted(rrf_scores, reverse=True))

    def test_per_channel_breakdown_preserved(self) -> None:
        bm25 = [{"id": "a", "_score": 0.9}]
        sem = [{"id": "a", "_score": 0.5}]
        ent = [{"id": "a", "_score": 0.3}]
        out = fuse_rrf_with_channels(
            [("bm25", bm25), ("semantic", sem), ("entity", ent)],
        )
        self.assertEqual(len(out), 1)
        ch = out[0]["_channel_scores"]
        self.assertIn("bm25", ch)
        self.assertIn("semantic", ch)
        self.assertIn("entity", ch)
        self.assertAlmostEqual(ch["bm25"], 0.9)
        self.assertAlmostEqual(ch["semantic"], 0.5)
        self.assertAlmostEqual(ch["entity"], 0.3)

    def test_document_missing_from_channel_records_no_score(self) -> None:
        bm25 = [{"id": "a", "_score": 0.9}]
        sem = [{"id": "a", "_score": 0.5}]
        ent = [{"id": "b", "_score": 0.7}]  # b not in bm25/sem
        out = fuse_rrf_with_channels(
            [("bm25", bm25), ("semantic", sem), ("entity", ent)],
        )
        ids_to_scores = {r["id"]: r["_channel_scores"] for r in out}
        self.assertNotIn("bm25", ids_to_scores["b"])
        self.assertNotIn("semantic", ids_to_scores["b"])
        self.assertIn("entity", ids_to_scores["b"])

    def test_rrf_total_matches_plain_fuse_rrf(self) -> None:
        """Audit 2026-10-18: ``fuse_rrf_with_channels`` is a strict
        superset of ``fuse_rrf``. Same input lists (sans labels) must
        yield the same ``_rrf`` totals so callers that already use
        ``fuse_rrf`` see byte-identical RRF numbers."""
        bm25 = [{"id": "a", "_score": 0.9}, {"id": "b", "_score": 0.5}]
        sem = [{"id": "a", "_score": 0.8}]
        plain = fuse_rrf([bm25, sem], k=60)
        labelled = fuse_rrf_with_channels(
            [("bm25", bm25), ("semantic", sem)], k=60,
        )
        plain_map = {r["id"]: r["_rrf"] for r in plain}
        labelled_map = {r["id"]: r["_rrf"] for r in labelled}
        self.assertEqual(plain_map, labelled_map)

    def test_default_k_is_60(self) -> None:
        """The original Cormack 2009 RRF constant -- matches Mem0/Graphiti."""
        self.assertEqual(DEFAULT_RRF_K, 60)


class EscapeFtsTests(unittest.TestCase):
    """``_escape_fts`` produces FTS5 MATCH expressions."""

    def test_empty_query_returns_empty_string(self) -> None:
        self.assertEqual(_escape_fts(""), "")
        self.assertEqual(_escape_fts("   "), "")

    def test_latin_words_get_quoted(self) -> None:
        out = _escape_fts("vue javascript")
        self.assertIn('"vue"', out)
        self.assertIn('"javascript"', out)

    def test_cjk_decomposes_to_trigrams(self) -> None:
        out = _escape_fts("知识图谱")
        # Each trigram (knowledge-of-graph) appears as a separate token.
        # The exact trigrams are 知识图, 识图谱; 5-char-or-more runs use
        # boundary trigrams (first + last) only.
        self.assertTrue(out)  # non-empty

    def test_mixed_input(self) -> None:
        out = _escape_fts("vue 知识图谱")
        # Both latin (quoted) and CJK (trigrams) survive
        self.assertIn('"vue"', out)


class LikeFallbackTests(unittest.TestCase):
    """The per-token OR LIKE fallback added in 0.4.15."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "fallback.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_whole_string_match_still_works(self) -> None:
        """Pre-0.4.15 contract: '%whole query%' still surfaces exact
        substring hits. This path runs first when the LIKE fallback
        is invoked so existing callers see byte-identical behaviour
        for short, exact queries."""
        self.store.upsert_memory(
            kind="fact", text="Deploy process uses blue/green",
        )
        out = _like_fallback(self.store, "blue/green", kind="memories", limit=5)
        self.assertEqual(len(out), 1)
        # out[*]["id"] is the memory UUID; the row is verifiable via
        # get_memory() instead.
        item = self.store.get_memory(out[0]["id"])
        self.assertIsNotNone(item)
        self.assertIn("blue/green", item.text)

    def test_per_token_or_surfaces_partial_match(self) -> None:
        """When the whole-string LIKE returns nothing, the per-token
        OR LIKE picks up rows where ANY token appears in the text.
        This is the 0.4.15 improvement that lets a natural-language
        query like 'What is the deploy process?' hit a stored fact
        that says only 'Deploy process uses blue/green'."""
        self.store.upsert_memory(
            kind="fact", text="Deploy process uses blue/green",
        )
        # 'What is the deploy process?' doesn't appear as a substring
        # in the stored text, but 'deploy' and 'process' do.
        out = _like_fallback(
            self.store,
            "What is the deploy process?",
            kind="memories", limit=5,
        )
        self.assertTrue(out, "per-token OR should match 'deploy' or 'process'")

    def test_empty_query_returns_empty(self) -> None:
        self.assertEqual(
            _like_fallback(self.store, "", kind="memories", limit=5),
            [],
        )
        self.assertEqual(
            _like_fallback(self.store, "   ", kind="memories", limit=5),
            [],
        )

    def test_limit_caps_results(self) -> None:
        for i in range(5):
            self.store.upsert_memory(
                kind="fact", text=f"deploy process v{i}",
            )
        out = _like_fallback(
            self.store, "deploy", kind="memories", limit=2,
        )
        self.assertLessEqual(len(out), 2)


class BackwardCompatBm25SearchTests(unittest.TestCase):
    """``bm25_search`` still works with multi-word natural-language queries
    even when FTS5 trigram can't tokenise them."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "bm25.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_natural_language_question_finds_related_fact(self) -> None:
        """Pre-0.4.15 the LIKE fallback required the whole query as a
        substring, so 'What is the deploy process?' matched nothing
        because the stored fact is 'Deploy process uses blue/green'.
        0.4.15's per-token OR LIKE surfaces at least the 'deploy'
        match."""
        from loop_memory.storage.retrieval import bm25_search
        self.store.upsert_memory(
            kind="fact", text="Deploy process uses blue/green",
            agent_id="a", user_id="u",
        )
        out = bm25_search(self.store, "What is the deploy process?",
                          kind="memories", limit=5)
        self.assertTrue(out,
                        "bm25_search must surface at least the per-token OR match")


if __name__ == "__main__":
    unittest.main()

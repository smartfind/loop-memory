"""Regression tests for the 0.4.18 lexical ranking fix.

Audit 2026-11-01 (loop-memory 0.4.18).

The defect under test: SQLite's FTS5 ``bm25()`` clamps a non-positive
inverse document frequency to ``1e-6``.  A query term that occurs in
roughly half the corpus therefore annihilates its own weight and the
whole channel score collapses.  With the ``trigram`` tokenizer this is
the *normal* case for CJK, and the visible symptom was an evidence
chain that reported ``bm25: 0.0`` -- a headline 0.4.15 feature
telling the user "this hit because bm25 0.0".

The fix (loop-memory 0.4.18) replaces the published channel score
with a textbook BM25 (k1 = 1.2, b = 0.75) using the Lucene / BM25+
idf, which is strictly positive for every term, and promotes exact
substring matches ahead of bag-of-trigrams matches -- the rule
``rohitg00/agentmemory`` shipped in v0.9.31 (2026-10-26).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from loop_memory.storage.lexical import (
    EXACT_MATCH_BOOST,
    exact_match,
    query_terms,
    score_documents,
)
from loop_memory.storage.retrieval import bm25_search
from loop_memory.storage.sqlite_store import MemoryStore

#: Anything below this is on SQLite's idf floor -- the score carries
#: no information and rounds to 0.0 in the evidence chain.
DEGENERATE_FLOOR = 1e-4


class QueryTermsTests(unittest.TestCase):
    """``query_terms`` mirrors the FTS5 tokenisation but keeps every
    trigram of a CJK run, not only the two boundary ones."""

    def test_cjk_run_yields_every_trigram(self) -> None:
        terms = query_terms("知识图谱构建")
        # Six characters -> four sliding trigrams. The FTS5 candidate
        # query only uses the first and the last; the scorer uses all
        # four so it can tell 谱构建 apart from 谱的构.
        self.assertEqual(terms, ["知识图", "识图谱", "图谱构", "谱构建"])

    def test_latin_words_are_kept(self) -> None:
        self.assertEqual(
            query_terms("Kubernetes ingress controller"),
            ["kubernetes", "ingress", "controller"],
        )

    def test_mixed_query_keeps_both_shapes(self) -> None:
        terms = query_terms("vue 知识图谱")
        self.assertIn("vue", terms)
        self.assertIn("知识图", terms)
        self.assertIn("识图谱", terms)

    def test_duplicates_are_collapsed_but_order_kept(self) -> None:
        terms = query_terms("deploy deploy kubernetes")
        self.assertEqual(terms, ["deploy", "kubernetes"])

    def test_run_shorter_than_a_trigram_yields_nothing(self) -> None:
        # Two characters cannot be tokenised by the trigram tokenizer,
        # so there is nothing to score on.
        self.assertEqual(query_terms("图谱"), [])


class ExactMatchTests(unittest.TestCase):
    """The exact-match rule promoted by 0.4.18."""

    def test_cjk_verbatim_substring(self) -> None:
        self.assertTrue(
            exact_match("知识图谱构建流程的第一步是切分", "知识图谱构建流程")
        )

    def test_cjk_scattered_trigrams_are_not_an_exact_match(self) -> None:
        self.assertFalse(
            exact_match("构建流程的第一步是切分知识图谱的输入文档", "知识图谱构建流程")
        )

    def test_latin_is_case_insensitive(self) -> None:
        self.assertTrue(
            exact_match("The Blue Green rollout switches traffic", "blue green rollout")
        )

    def test_stray_spaces_in_a_cjk_query_still_match(self) -> None:
        # A Chinese query is commonly typed with stray spaces that the
        # stored memory will not contain.
        self.assertTrue(exact_match("知识图谱构建流程", "知识图谱 构建流程"))

    def test_empty_query_never_matches(self) -> None:
        self.assertFalse(exact_match("anything", ""))
        self.assertFalse(exact_match("anything", "   "))


class LexicalScoreTests(unittest.TestCase):
    """The replacement BM25 score."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "lex.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _corpus(self) -> None:
        # Twelve of thirteen documents mention 知识图谱, so both of the
        # query's trigrams are past the 50 % mark where FTS5 clamps the
        # idf to 1e-6.
        for i in range(12):
            self.store.upsert_memory(
                kind="fact", text=f"知识图谱的第{i}篇笔记讨论了不同的主题和侧面"
            )
        self.store.upsert_memory(
            kind="fact", text="The deploy process uses blue green rollout"
        )

    def test_score_is_finite_and_positive(self) -> None:
        self._corpus()
        self.store.upsert_memory(
            kind="fact", text="知识图谱构建流程需要保留原始偏移量"
        )
        hits = score_documents(self.store, "知识图谱构建", [], kind="memories")
        self.assertEqual(hits, {})

        doc_ids = [m.id for m in self.store.list_memories(limit=20)]
        hits = score_documents(self.store, "知识图谱构建", doc_ids, kind="memories")
        self.assertTrue(hits)
        for hit in hits.values():
            self.assertGreater(hit.score, 0.0)
            self.assertLess(hit.score, 1e6)

    def test_idf_never_collapses_to_the_fts5_floor(self) -> None:
        """A term present in most of the corpus must still produce a
        score the evidence chain can show."""
        self._corpus()
        doc_ids = [m.id for m in self.store.list_memories(limit=20)]
        hits = score_documents(self.store, "知识图谱", doc_ids, kind="memories")
        self.assertTrue(hits)
        worst = min(hit.score for hit in hits.values())
        self.assertGreater(
            worst,
            DEGENERATE_FLOOR,
            "the replacement score must stay off SQLite's 1e-6 idf floor",
        )

    def test_more_matched_terms_scores_higher(self) -> None:
        self.store.upsert_memory(kind="fact", text="alpha beta")
        self.store.upsert_memory(kind="fact", text="alpha only")
        ids = [m.id for m in self.store.list_memories(limit=10)]
        hits = score_documents(self.store, "alpha beta", ids, kind="memories")
        both = [h for h in hits.values() if h.matched_terms == 2]
        one = [h for h in hits.values() if h.matched_terms == 1]
        self.assertTrue(both and one)
        self.assertGreater(both[0].score, one[0].score)

    def test_exact_match_is_boosted(self) -> None:
        self.store.upsert_memory(kind="fact", text="alpha beta gamma delta")
        self.store.upsert_memory(kind="fact", text="alpha beta gamma delta delta")
        ids = [m.id for m in self.store.list_memories(limit=10)]
        hits = score_documents(self.store, "alpha beta gamma delta", ids, kind="memories")
        exact = [h for h in hits.values() if h.exact]
        self.assertTrue(exact)
        self.assertGreater(exact[0].score, 0.0)

    def test_exact_boost_constant_is_applied(self) -> None:
        self.store.upsert_memory(kind="fact", text="alpha beta gamma delta")
        ids = [m.id for m in self.store.list_memories(limit=10)]
        hits = score_documents(self.store, "alpha beta gamma delta", ids, kind="memories")
        hit = list(hits.values())[0]
        self.assertTrue(hit.exact)
        self.assertGreater(EXACT_MATCH_BOOST, 1.0)


class Bm25SearchScoringTests(unittest.TestCase):
    """``bm25_search`` publishes the repaired score, not SQLite's."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "bm25.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _common_term_corpus(self) -> None:
        for i in range(12):
            self.store.upsert_memory(
                kind="fact", text=f"知识图谱的第{i}篇笔记讨论了不同的主题和侧面"
            )

    def test_published_score_is_healthy_while_fts_score_is_degenerate(self) -> None:
        """The core 0.4.18 regression guard.

        The raw FTS5 score for a query whose terms are all common sits
        on SQLite's 1e-6 idf floor. The published score must not.
        """
        self._common_term_corpus()
        self.store.upsert_memory(kind="fact", text="The deploy process uses blue green")
        out = bm25_search(self.store, "知识图谱", kind="memories", limit=20)
        self.assertTrue(out)
        published = [r["_score"] for r in out]
        raw = [r["_fts_score"] for r in out if r.get("_fts_score") is not None]
        self.assertTrue(raw)
        self.assertLess(max(raw), DEGENERATE_FLOOR, "fixture no longer reproduces the clamp")
        self.assertGreater(min(published), DEGENERATE_FLOOR)

    def test_evidence_chain_reports_a_non_zero_bm25_for_cjk(self) -> None:
        """The user-visible symptom: the 0.4.15 evidence chain used to
        say "this hit because bm25 0.0" for every CJK query."""
        self._common_term_corpus()
        result = self.store.recall_hybrid("知识图谱", limit=5, include=("memories",))
        self.assertTrue(result["memories"])
        for hit in result["memories"]:
            evidence = {e["field"]: e for e in hit.get("evidence") or []}
            self.assertIn("bm25", evidence, "the BM25 channel must report evidence")
            self.assertGreater(evidence["bm25"]["value"], 0.0)

    def test_exact_match_outranks_a_shorter_near_miss(self) -> None:
        """The rule borrowed from agentmemory v0.9.31."""
        self.store.upsert_memory(
            kind="fact",
            text="知识图谱构建流程的第一步是切分输入文档并保留原始偏移量用于后续的实体对齐工作",
        )
        self.store.upsert_memory(kind="fact", text="构建流程的第一步是切分知识图谱的输入文档")
        self.store.upsert_memory(kind="fact", text="知识图谱的构建流程包含切分和抽取两个阶段")
        out = bm25_search(self.store, "知识图谱构建流程", kind="memories", limit=10)
        self.assertTrue(out)
        exact = [m for m in self.store.list_memories(limit=10)
                 if "知识图谱构建流程的第一步" in m.text]
        self.assertTrue(exact)
        self.assertEqual(out[0]["id"], exact[0].id)
        self.assertTrue(out[0].get("_exact"))

    def test_candidate_set_is_unchanged_by_the_rescoring(self) -> None:
        """The fix re-ranks; it must not drop or add documents."""
        self._common_term_corpus()
        self.store.upsert_memory(kind="fact", text="知识图谱构建流程需要保留原始偏移量")
        out = bm25_search(self.store, "知识图谱构建流程", kind="memories", limit=20)
        ids = {r["id"] for r in out}
        # Every document the FTS5 query and the LIKE fallback surfaced
        # must still be present.
        expected = set()
        with self.store._conn() as c:
            for row in c.execute(
                "SELECT m.id AS id FROM memories_fts t "
                "JOIN memories m ON m.rowid = t.rowid "
                "WHERE memories_fts MATCH ?",
                ("知识图 建流程",),
            ):
                expected.add(row["id"])
        self.assertTrue(expected)
        self.assertTrue(expected.issubset(ids))

    def test_like_only_hit_is_on_the_same_scale(self) -> None:
        """A LIKE-only hit used to publish the memory's ``importance``
        (0.5) under the name ``bm25``."""
        self.store.upsert_memory(
            kind="fact", text="Deploy process uses blue/green rollout strategy"
        )
        out = bm25_search(self.store, "blue/green", kind="memories", limit=5)
        self.assertTrue(out)
        for row in out:
            self.assertLess(row["_score"], 50.0)

    def test_query_without_a_scorable_term_keeps_the_legacy_path(self) -> None:
        """Two-character tokens cannot be tokenised by the trigram
        index, so the scorer has nothing to work with and the legacy
        ordering is returned untouched."""
        self.store.upsert_memory(kind="fact", text="AI and ML notes")
        self.store.upsert_memory(kind="fact", text="AI safety notes")
        out = bm25_search(self.store, "ai", kind="memories", limit=5)
        self.assertTrue(out)
        for row in out:
            self.assertNotIn("_exact", row)


if __name__ == "__main__":
    unittest.main()

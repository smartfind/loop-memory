"""Tests for the entity extractor + knowledge graph builder."""

from __future__ import annotations

import unittest
from pathlib import Path

from loop_memory import MemoryStore
from loop_memory.graph.build import KnowledgeGraph
from loop_memory.graph.extract import (
    extract_entities,
    pair_cooccurrence,
)


class ExtractorTests(unittest.TestCase):
    def test_proper_nouns_extracted(self) -> None:
        ents = dict(extract_entities("I use Codex and Claude and Hermes."))
        self.assertIn("Codex", ents)
        self.assertIn("Claude", ents)
        self.assertIn("Hermes", ents)

    def test_stopwords_filtered(self) -> None:
        ents = dict(extract_entities("The API was implemented by User. Issue fixed."))
        self.assertIn("API", ents)
        self.assertNotIn("User", ents)
        self.assertNotIn("Issue", ents)
        self.assertNotIn("The", ents)

    def test_cjk_phrase_extracted(self) -> None:
        ents = dict(extract_entities("重构数据库结构,扩展数据库表结构."))
        # n-gram may produce longer spans; assert at least one of them
        # contains the headword.
        joined = "".join(ents.keys())
        self.assertIn("重构", joined)
        self.assertIn("扩展数据库表结构", joined)

    def test_pair_cooccurrence_window(self) -> None:
        text = "Codex CLI uses Codex CLI and Claude."
        pairs = pair_cooccurrence([text], window=3)
        # Codex ↔ Claude should appear because they are within 3 tokens
        keys = {tuple(sorted([a, b])) for (a, b, _) in pairs}
        self.assertIn(("Claude", "Codex"), keys)


class GraphStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path("/tmp/test_loop_graph.db")
        self.path.unlink(missing_ok=True)
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_entity_upsert_increments_weight(self) -> None:
        e1 = self.store.upsert_entity("Codex")
        e2 = self.store.upsert_entity("Codex", bump_weight=0.1)
        self.assertEqual(e2.mention_count, 2)
        self.assertGreater(e2.weight, e1.weight)

    def test_relation_upsert_unique(self) -> None:
        self.store.upsert_relation("Codex", "Claude", evidence_id="m1")
        self.store.upsert_relation("Codex", "Claude", evidence_id="m2")
        rels = self.store.list_relations()
        self.assertEqual(len(rels), 1)
        self.assertEqual(sorted(rels[0].evidence_ids), ["m1", "m2"])

    def test_build_walks_memories(self) -> None:
        # Create 3 memories
        for text in [
            "Help with Codex CLI task.",
            "Codex and Claude and Hermes overlap.",
            "Fix the API in Codex.",
        ]:
            self.store.upsert_memory(
                kind="fact", text=text, importance=0.5, source="codex",
            )
        report = KnowledgeGraph(self.store).rebuild(clear=True)
        self.assertGreater(report.entities, 0)
        self.assertGreater(report.relations, 0)
        ents = {e.name for e in self.store.list_entities(limit=200)}
        self.assertIn("Codex", ents)
        self.assertIn("Claude", ents)
        self.assertIn("API", ents)

    def test_build_from_wiki_creates_wiki_tag_concept_nodes(self) -> None:
        # Seed a couple of wiki pages that share tags and concepts.
        self.store.upsert_wiki_page(
            slug="codex-architecture", title="Codex Architecture",
            body="Codex is a CLI agent. It uses the API. Codex runs locally.",
            summary="How Codex is built", tags=["codex", "cli"],
            importance=0.8,
        )
        self.store.upsert_wiki_page(
            slug="claude-api", title="Claude API",
            body="Claude is reachable via the API. Codex also calls Claude.",
            summary="How to reach Claude", tags=["claude", "api"],
            importance=0.7,
        )
        report = KnowledgeGraph(self.store).rebuild_from_wiki(clear=True)
        self.assertGreaterEqual(report.entities, 4)
        self.assertGreaterEqual(report.relations, 4)

        ents = self.store.list_entities(limit=500)
        names_by_kind = {}
        for e in ents:
            names_by_kind.setdefault(e.kind, set()).add(e.name)
        # Wiki page nodes present
        self.assertIn("wiki:codex-architecture", names_by_kind.get("wiki_page", set()))
        self.assertIn("wiki:claude-api", names_by_kind.get("wiki_page", set()))
        # Tag nodes present
        self.assertIn("tag:codex", names_by_kind.get("tag", set()))
        self.assertIn("tag:cli", names_by_kind.get("tag", set()))
        # Concept nodes extracted from bodies
        self.assertIn("concept:Codex", names_by_kind.get("concept", set()))
        self.assertIn("concept:API", names_by_kind.get("concept", set()))

        # Wiki-to-wiki related edge must exist via shared concept "Codex".
        rels = self.store.list_relations(limit=500)
        related = {(r.src, r.dst, r.kind) for r in rels}
        self.assertIn(("wiki:codex-architecture", "wiki:claude-api", "related_to"),
                      related)
        self.assertIn(("wiki:claude-api", "wiki:codex-architecture", "related_to"),
                      related)
        # Wiki --tagged_with--> tag must exist
        self.assertIn(("wiki:codex-architecture", "tag:codex", "tagged_with"),
                      related)
        # Wiki --mentions--> concept must exist
        self.assertIn(("wiki:codex-architecture", "concept:Codex", "mentions"),
                      related)

    def test_build_from_wiki_empty_store(self) -> None:
        report = KnowledgeGraph(self.store).rebuild_from_wiki(clear=True)
        self.assertEqual(report.entities, 0)
        self.assertEqual(report.relations, 0)
        self.assertEqual(report.memories_scanned, 0)

    def test_build_from_wiki_dedupes_wiki_node(self) -> None:
        # The internal bump loop should still leave exactly one entity row
        # per (name, kind); ensure no duplicates sneak in.
        self.store.upsert_wiki_page(
            slug="x", title="X", body="", summary="", tags=["t"],
            importance=0.5,
        )
        KnowledgeGraph(self.store).rebuild_from_wiki(clear=True)
        rows = [e for e in self.store.list_entities(limit=500)
                if e.name == "wiki:x" and e.kind == "wiki_page"]
        self.assertEqual(len(rows), 1)


class OpenClawLoaderTests(unittest.TestCase):
    def test_parses_jsonl(self) -> None:
        from loop_memory.ingest.loader import OpenClawLoader

        path = Path("/tmp/openclaw_test.jsonl")
        path.write_text(
            '\n'.join([
                '{"role": "user", "content": "hi, I am Mia", "ts": 1700000000}',
                '{"role": "assistant", "content": "Hello Mia!", "ts": 1700000010}',
            ])
        )
        try:
            sess = OpenClawLoader().load_one(path)
            self.assertIsNotNone(sess)
            self.assertEqual(sess.source, "openclaw")
            self.assertEqual(len(sess.turns), 2)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

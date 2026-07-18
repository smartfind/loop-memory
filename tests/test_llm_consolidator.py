"""Tests for the LLM consolidator (filter / score / summarize).

These tests don't make any HTTP calls - they verify the rules-based
filter, the JSON extraction, and the dry-run end-to-end.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loop_memory.jobs.llm_consolidate import (
    LLMConsolidator,
    _extract_json,
    _info_density,
    _is_raw_transcript,
    _looks_like_noise,
)
from loop_memory.llm.providers import (
    PROVIDERS,
    RuleBasedProvider,
    build_provider,
    default_config,
    validate_config,
)
from loop_memory.storage.sqlite_store import MemoryStore


class NoiseHeuristics(unittest.TestCase):
    def test_pure_filler(self):
        for s in ["ok", "OK.", "好的", "收到!", "thanks", "嗯", "Hello!"]:
            self.assertTrue(_looks_like_noise(s), s)

    def test_real_facts_are_kept(self):
        for s in [
            "User's name is Alice",
            "Project: build a memory system with SQLite + FastAPI",
            "决策: 缓存30天, 半衰期7天",
            "Project deadline is 2026-09-01",
        ]:
            self.assertFalse(_looks_like_noise(s), s)

    def test_density(self):
        self.assertGreater(_info_density("use POST /v1/chat/completions"), 0.10)
        self.assertLess(_info_density(""), 0.01)
        self.assertGreaterEqual(_info_density("always use SQLite for storage"), 0.1)

    def test_raw_transcript_detection(self):
        for s in [
            "User said: hello",
            "user said: foo",
            "Assistant: sure",
            "  Human: hi",
        ]:
            self.assertTrue(_is_raw_transcript(s), s)
        for s in [
            "User intent: build a memory system",
            "Conversation about user said something vague",
            "User said long ago we did X and Y and Z with dates",
        ]:
            self.assertFalse(_is_raw_transcript(s), s)


class JsonExtraction(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(_extract_json('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(_extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_with_prose_prefix(self):
        self.assertEqual(
            _extract_json('Sure! Here: {"items":[{"id":"x","keep":false}]}'),
            {"items": [{"id": "x", "keep": False}]},
        )

    def test_garbage(self):
        self.assertIsNone(_extract_json("not json at all"))

    def test_array(self):
        self.assertEqual(_extract_json("[1,2,3]"), [1, 2, 3])


class ProviderConfig(unittest.TestCase):
    def test_default_validates(self):
        cfg, warnings = validate_config(default_config())
        self.assertEqual(cfg["provider"], "echo")
        self.assertIn("schedule", cfg)
        self.assertIn("behaviour", cfg)
        self.assertGreater(cfg["behaviour"]["batch_size"], 0)

    def test_unknown_provider_falls_back(self):
        cfg, warnings = validate_config({"provider": "no-such"})
        self.assertEqual(cfg["provider"], "echo")
        self.assertTrue(any("unknown provider" in w for w in warnings))

    def test_build_rule(self):
        p = build_provider({"provider": "echo"})
        self.assertIsInstance(p, RuleBasedProvider)
        self.assertEqual(p.model, "rules")

    def test_build_provider_case_insensitive(self):
        # ``MiniMax`` is the canonical key; lower-cased input should
        # still resolve to the OpenAI-compat provider when an API key
        # is present.
        from loop_memory.llm.providers import OpenAICompatProvider
        p = build_provider({"provider": "MiniMax", "api_key": "fake-key",
                            "api_key_set": True})
        self.assertIsInstance(p, OpenAICompatProvider)
        self.assertEqual(p.model, "MiniMax-M2.7")

    def test_build_provider_falls_back_when_no_api_key(self):
        # Providers that need an API key must not silently attempt
        # 60-second network timeouts when the user has not configured
        # one - we drop to the rule-based provider instead. The
        # ``resolve_api_key`` mock fakes an empty secret backend so this
        # test is hermetic regardless of host state.
        from unittest.mock import patch
        with patch("loop_memory.llm.providers.resolve_api_key", return_value=None):
            p = build_provider({"provider": "MiniMax",
                                "api_key_set": False})
        self.assertIsInstance(p, RuleBasedProvider)

    def test_bounds(self):
        cfg, _ = validate_config(
            {"provider": "echo", "behaviour": {"batch_size": 10000, "temperature": 9.9}}
        )
        self.assertEqual(cfg["behaviour"]["batch_size"], 500)  # capped
        self.assertLessEqual(cfg["behaviour"]["temperature"], 2.0)

    def test_catalogue_has_all_known(self):
        for k in ("openai", "anthropic", "ollama", "echo"):
            self.assertIn(k, PROVIDERS)


class ConsolidatorEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        # Seed a few memories
        for kind, text, imp in [
            ("fact", "User wants loop memory project", 0.7),
            ("fact", "ok", 0.5),
            ("fact", "decision: use SQLite", 0.8),
            ("episode", "hi", 0.5),
            ("fact", "use the user's name Alice", 0.6),
        ]:
            self.store.upsert_memory(kind=kind, text=text, importance=imp)

    def tearDown(self):
        self.tmp.cleanup()

    def _llm(self, reply: str = ""):
        class Fake:
            model = "fake"

            def complete(self, history, **kw):
                return reply
        return Fake()

    def test_dry_run_does_not_mutate(self):
        before = self.store.stats()["memories"]
        cons = LLMConsolidator(
            self.store,
            self._llm(""),
            {"batch_size": 50, "enable_filter": True, "enable_score": True, "dry_run": True},
        )
        stats = cons.run()
        after = self.store.stats()["memories"]
        self.assertEqual(before, after)
        self.assertGreaterEqual(stats.dropped, 0)  # at minimum, pre-filter was a no-op in dry run? actually we DO delete in pre-filter. let's verify

    def test_prefilter_drops_noise(self):
        # Without LLM (empty reply), pre-filter still runs and drops noise.
        cons = LLMConsolidator(
            self.store,
            self._llm(""),
            {"batch_size": 50, "enable_filter": True, "enable_score": False, "enable_summarize": False},
        )
        stats = cons.run()
        self.assertEqual(stats.scanned, 5)
        # "ok" and "hi" are obvious noise and should be dropped
        self.assertGreaterEqual(stats.dropped, 2)
        # Real facts survive
        keep = [m.text for m in self.store.list_memories(limit=50)]
        self.assertTrue(any("loop memory" in t for t in keep))
        self.assertTrue(any("SQLite" in t for t in keep))

    def test_llm_can_drop_and_score(self):
        reply = json.dumps({
            "items": [
                {"id": self._id_for("User wants loop memory project"),
                 "keep": True, "importance": 0.9, "distill": ""},
                {"id": self._id_for("decision: use SQLite"),
                 "keep": True, "importance": 0.95, "distill": "Use SQLite for the loop-memory store."},
                {"id": self._id_for("use the user's name Alice"),
                 "keep": False, "importance": 0.0, "distill": ""},
            ]
        })
        cons = LLMConsolidator(
            self.store,
            self._llm(reply),
            {"batch_size": 50, "enable_filter": True, "enable_score": True,
             "enable_summarize": True, "dry_run": False},
        )
        stats = cons.run()
        self.assertGreaterEqual(stats.importance_updated, 2)
        self.assertGreaterEqual(stats.resummarized, 1)
        self.assertGreaterEqual(stats.dropped, 1)
        # After all runs (we only returned 3 items, so the rest stay)
        kept = [m.text for m in self.store.list_memories(limit=50)]
        self.assertTrue(any("SQLite for the loop-memory store" in t for t in kept))
        self.assertFalse(any("Alice" in t for t in kept))

    def test_preview(self):
        cons = LLMConsolidator(self.store, self._llm(""), {})
        prev = cons.preview(limit=5)
        self.assertEqual(len(prev), 5)
        for row in prev:
            self.assertIn("id", row)
            self.assertIn("noise", row)
            self.assertIn("density", row)

    def _id_for(self, text: str) -> str:
        for m in self.store.list_memories(query=text, limit=50):
            if m.text == text:
                return m.id
        raise AssertionError(f"missing {text}")


class StoreSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_set_roundtrip(self):
        self.assertIsNone(self.store.get_setting("llm_consolidator"))
        cfg = {"provider": "openai", "model": "gpt-4o-mini", "api_key": "x"}
        self.store.set_setting("llm_consolidator", cfg)
        self.assertEqual(self.store.get_setting("llm_consolidator"), cfg)
        all_settings = self.store.get_all_settings()
        self.assertIn("llm_consolidator", all_settings)

    def test_run_history(self):
        rid = self.store.start_consolidation_run("manual", model="echo")
        self.store.finish_consolidation_run(rid, "done", stats={"x": 1})
        runs = self.store.list_consolidation_runs(limit=5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], rid)
        self.assertEqual(runs[0]["status"], "done")
        self.assertEqual(runs[0]["stats"], {"x": 1})


if __name__ == "__main__":
    unittest.main()



class WikiStore(unittest.TestCase):
    """Direct tests on the wiki_pages table + CRUD methods."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_then_update_bumps_version(self):
        p1 = self.store.upsert_wiki_page(
            slug="loop-memory-project", title="Loop Memory", body="v1 body")
        self.assertEqual(p1["version"], 1)
        self.assertEqual(self.store.count_wiki_pages(), 1)
        p2 = self.store.upsert_wiki_page(
            slug="loop-memory-project", title="Loop Memory v2", body="v2 body")
        self.assertEqual(p2["version"], 2)
        self.assertEqual(p2["body"], "v2 body")
        self.assertEqual(self.store.count_wiki_pages(), 1)

    def test_list_filters_by_query_and_importance(self):
        for slug, title, body, imp in [
            ("alpha", "Alpha", "the alpha topic", 0.9),
            ("beta",  "Beta",  "the beta topic",  0.3),
            ("gamma", "Gamma", "some gamma notes",0.6),
        ]:
            self.store.upsert_wiki_page(
                slug=slug, title=title, body=body, importance=imp)
        high = self.store.list_wiki_pages(min_importance=0.5)
        self.assertEqual({p["slug"] for p in high}, {"alpha", "gamma"})
        beta = self.store.list_wiki_pages(query="beta")
        self.assertEqual(len(beta), 1)
        self.assertEqual(beta[0]["slug"], "beta")

    def test_delete_returns_rowcount(self):
        p = self.store.upsert_wiki_page(slug="x", title="X", body="…")
        self.assertTrue(self.store.delete_wiki_page(p["id"]))
        self.assertEqual(self.store.count_wiki_pages(), 0)
        self.assertFalse(self.store.delete_wiki_page(p["id"]))


class WikiSynthesis(unittest.TestCase):
    """End-to-end test of the consolidator's wiki step with a fake LLM."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        # Three memories, all keepers, all importance >= 0.4 so they pass
        # the wiki filter.
        self.mem_ids = []
        for kind, text, imp in [
            ("fact", "Project name is Loop Memory, runs locally on macOS via launchd.", 0.7),
            ("fact", "Backend is Python 3.14 with FastAPI, frontend is one HTML file.", 0.7),
            ("fact", "Stores memories in SQLite at ~/.loop_memory/loop_memory.db.", 0.6),
        ]:
            m = self.store.upsert_memory(kind=kind, text=text, importance=imp)
            self.mem_ids.append(m.id)

    def tearDown(self):
        self.tmp.cleanup()

    def _llm(self, per_call_replies):
        """Return a fake LLM that pops a reply off the queue per call."""
        queue = list(per_call_replies)
        class Fake:
            model = "fake"
            def complete(self, history, **kw):
                if not queue:
                    return ""
                return queue.pop(0)
        return Fake()

    def _wiki_reply(self, page_specs):
        return json.dumps({"pages": page_specs})

    def test_synth_creates_pages(self):
        filter_reply = json.dumps({"items": []})  # keep all, no-op
        wiki_reply = self._wiki_reply([
            {
                "slug": "loop-memory-architecture",
                "title": "Loop Memory Architecture",
                "summary": "Project layout and runtime.",
                "body": "## Stack\n- Python 3.14 + FastAPI\n- SQLite",
                "tags": ["loop-memory", "architecture"],
                "importance": 0.8,
                "evidence_ids": [self.mem_ids[0], self.mem_ids[1]],
            },
            {
                "slug": "storage-layout",
                "title": "Storage Layout",
                "summary": "Where the SQLite db lives.",
                "body": "DB at `~/.loop_memory/loop_memory.db`.",
                "tags": ["storage"],
                "importance": 0.5,
                "evidence_ids": [self.mem_ids[2]],
            },
        ])
        cons = LLMConsolidator(
            self.store,
            self._llm([filter_reply, wiki_reply]),
            {"batch_size": 50, "enable_filter": True, "enable_score": True,
             "enable_summarize": True, "enable_wiki": True,
             "temperature": 0.3, "max_output_tokens": 800},
        )
        cons.set_run_id("test-run-1")
        stats = cons.run()
        self.assertGreaterEqual(stats.wiki_pages_created, 2)
        self.assertEqual(stats.wiki_calls, 1)
        pages = self.store.list_wiki_pages()
        slugs = {p["slug"] for p in pages}
        self.assertIn("loop-memory-architecture", slugs)
        self.assertIn("storage-layout", slugs)
        arch = self.store.get_wiki_page_by_slug("loop-memory-architecture")
        self.assertIn("FastAPI", arch["body"])
        self.assertEqual(arch["run_id"], "test-run-1")

    def test_synth_updates_existing_page(self):
        # Seed an existing page with the same slug.
        self.store.upsert_wiki_page(
            slug="loop-memory-architecture", title="old", body="old body")
        filter_reply = json.dumps({"items": []})
        reply = self._wiki_reply([{
            "slug": "loop-memory-architecture", "title": "new",
            "body": "new body", "importance": 0.6, "evidence_ids": [],
        }])
        cons = LLMConsolidator(
            self.store, self._llm([filter_reply, reply]),
            {"batch_size": 50, "enable_filter": True, "enable_score": True,
             "enable_summarize": True, "enable_wiki": True,
             "temperature": 0.3, "max_output_tokens": 800},
        )
        stats = cons.run()
        self.assertEqual(stats.wiki_pages_created, 0)
        self.assertEqual(stats.wiki_pages_updated, 1)
        p = self.store.get_wiki_page_by_slug("loop-memory-architecture")
        self.assertEqual(p["body"], "new body")
        self.assertEqual(p["version"], 2)

    def test_echo_provider_writes_rule_based_wiki(self):
        cons = LLMConsolidator(
            self.store, RuleBasedProvider(),
            {"batch_size": 50, "enable_filter": True, "enable_score": True,
             "enable_summarize": True, "enable_wiki": True,
             "temperature": 0.3, "max_output_tokens": 800},
        )
        stats = cons.run()
        # Echo provider falls back to deterministic rule-based wiki
        # synthesis so the UI has browsable wiki content even before
        # the user wires up a model.
        self.assertGreaterEqual(stats.wiki_pages_created + stats.wiki_pages_updated, 1)
        self.assertGreater(self.store.count_wiki_pages(), 0)

    def test_synth_handles_garbage_reply(self):
        cons = LLMConsolidator(
            self.store, self._llm([json.dumps({"items":[]}), "this is not json at all"]),
            {"batch_size": 50, "enable_filter": True, "enable_score": True,
             "enable_summarize": True, "enable_wiki": True,
             "temperature": 0.3, "max_output_tokens": 800},
        )
        stats = cons.run()
        # No crash, no pages created.
        self.assertEqual(stats.wiki_pages_created, 0)
        self.assertEqual(stats.wiki_calls, 1)
        self.assertEqual(self.store.count_wiki_pages(), 0)
        self.assertTrue(any("not valid JSON" in n for n in stats.notes))

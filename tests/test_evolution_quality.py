"""Tests for the noise-cleaning helpers and the new Stage 0 dedup pass."""
import os
import shutil
import tempfile
import unittest

from loop_memory.jobs.evolution import (
    EvolutionConsolidator,
    EvolutionStats,
    _clean_noise,
    _is_low_signal,
    _title_from,
)
from loop_memory.llm.providers import RuleBasedProvider
from loop_memory.storage.sqlite_store import MemoryStore


class NoiseCleanerTests(unittest.TestCase):
    def test_strips_outcome_prefix(self):
        s = _clean_noise("Outcome: shipped release 0.3.0 today")
        self.assertFalse(s.lower().startswith("outcome"))
        self.assertIn("shipped release", s)

    def test_strips_thinking_block(self):
        s = _clean_noise("[thinking]\nLet me check the API.\n[/thinking]\nPlan: add rotation")
        self.assertNotIn("thinking", s.lower())
        self.assertNotIn("let me check", s.lower())
        self.assertIn("Plan: add rotation", s)

    def test_strips_code_fence(self):
        s = _clean_noise("Here is the fix:\n```python\nprint('hi')\n```\nAnd that solved it.")
        self.assertNotIn("```", s)
        self.assertNotIn("print", s)
        self.assertIn("And that solved it", s)

    def test_strips_user_paths(self):
        s = _clean_noise("Saved at /Users/smartfind/Desktop/foo.py:42 after the patch")
        self.assertNotIn("/Users/", s)
        self.assertIn("Saved at", s)

    def test_strips_urls(self):
        s = _clean_noise("See https://example.com/docs/api for the contract")
        self.assertNotIn("https://", s)

    def test_cap_length(self):
        long = "alpha " * 100
        s = _clean_noise(long, max_len=80)
        self.assertLessEqual(len(s), 81)

    def test_low_signal_status_pings(self):
        self.assertTrue(_is_low_signal("Outcome: tests are green ✅"))
        self.assertTrue(_is_low_signal("tests are green"))
        self.assertTrue(_is_low_signal("CI passed"))
        self.assertTrue(_is_low_signal("ok."))
        self.assertTrue(_is_low_signal(""))
        # Real content must NOT be flagged.
        self.assertFalse(_is_low_signal("User prefers dark mode and concise UI"))
        self.assertFalse(_is_low_signal("Project loop-memory: local memory system for Codex/Claude/Hermes/OpenClaw"))

    def test_title_from_cleans(self):
        self.assertEqual(_title_from("User prefers dark mode and concise UI"), "User prefers dark mode and concise UI")
        # Truncates long titles
        t = _title_from("a " * 50)
        self.assertLessEqual(len(t), 60)


class Stage0DedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        sid = self.store.upsert_session(
            source="test", external_id="s1", title="t",
            started_at=1700000000.0, ended_at=1700001000.0,
            message_count=1,
        ).id
        # 3 near-duplicate "fact" memories + 2 distinct ones
        samples = [
            ("fact", 0.80, "User prefers dark mode and concise UI layouts across all apps"),
            ("fact", 0.78, "User prefers dark mode and concise UI layouts across all apps"),
            ("fact", 0.76, "User prefers dark mode and concise UI layouts across all applications"),
            ("fact", 0.70, "Project loop-memory is a local memory system for LLM clients"),
            ("fact", 0.65, "API keys are stored locally in ~/.loop_memory/secrets.json with mode 0600"),
        ]
        for kind, imp, text in samples:
            self.store.upsert_memory(
                kind=kind, text=text, importance=imp,
                source="test", session_id=sid, tags=[kind, "test"],
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dedup_collapses_near_duplicates(self):
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        mems = self.store.list_memories(limit=50)
        before = len(mems)
        # Confirm at least one duplicate set exists in the seed.
        kept = ec._stage0_dedup_memories(mems, EvolutionStats())
        after_in_store = len(self.store.list_memories(limit=50))
        self.assertLess(after_in_store, before, "dedup should remove near-dups from DB")
        self.assertGreater(len(kept), 0)
        # At least 2 near-dups collapsed
        self.assertGreaterEqual(before - after_in_store, 2)

    def test_filter_noise_drops_status_pings(self):
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        sid = self.store.list_sessions(limit=1)[0].id
        self.store.upsert_memory(
            kind="episode", text="Outcome: tests are green ✅",
            importance=0.55, source="test", session_id=sid, tags=["test"],
        )
        self.store.upsert_memory(
            kind="episode", text="User prefers dark mode and concise UI layouts",
            importance=0.55, source="test", session_id=sid, tags=["test"],
        )
        mems = self.store.list_memories(limit=50)
        kept = ec._stage0_filter_noise(mems)
        # The status ping should be filtered out, the real one kept.
        texts = [m.text for m in kept]
        self.assertFalse(any("tests are green" in t for t in texts))
        self.assertTrue(any("User prefers dark mode" in t for t in texts))

    def test_rule_based_wiki_produces_real_bullets(self):
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        sid = self.store.list_sessions(limit=1)[0].id
        # Seed a focused cluster of clean facts
        for text in [
            "Project loop-memory is a local memory system for LLM clients",
            "API keys are stored locally in ~/.loop_memory/secrets.json with mode 0600",
            "Wiki pages are distilled by an LLM consolidator every hour",
            "Graph view renders entities as 3D-rotating nodes",
        ]:
            self.store.upsert_memory(
                kind="fact", text=text, importance=0.7,
                source="test", session_id=sid, tags=["fact"],
            )
        mems = self.store.list_memories(limit=50)
        # Drop all legacy wiki pages so we observe fresh ones
        for pg in self.store.list_wiki_pages(limit=50):
            self.store.delete_wiki_page(pg["id"])
        ec.run(memories=mems, limit=50)
        pages = self.store.list_wiki_pages(limit=20)
        self.assertGreater(len(pages), 0)
        for pg in pages:
            body = pg.get("body", "")
            self.assertIn("- ", body, f"wiki page {pg.get('slug')} has no bullets")
            self.assertGreater(len(body.split("\n")), 1)
            # Title should not be a raw truncated user prompt
            title = pg.get("title", "")
            self.assertFalse(title.startswith(("User intent:", "Outcome:", "[codex]", "[claude]")))
            # Body should not contain the legacy "/ " glue
            self.assertLess(body.count(" / "), 3)


class WikiCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retires_glued_pages(self):
        # Inject a noisy legacy page
        self.store.upsert_wiki_page(
            slug="legacy-noise", title="[codex] 帮我创建一个项目，搭建一套大模型Loop Engineering的记忆系统",
            body="[codex] 帮我创建一个项目 / [codex] 帮我深度调研 / [codex] 帮我深度调研 / ",
            summary="glued raw memories",
            tags=["auto", "episode"],
            importance=0.30,
            evidence_ids=[],
        )
        self.store.upsert_wiki_page(
            slug="keep-good", title="User prefers dark mode",
            body="- User prefers dark mode across all apps\n- Concise UI matters",
            summary="User dark-mode preference",
            tags=["auto", "fact"],
            importance=0.7,
            evidence_ids=["abc", "def"],
        )
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        from loop_memory.jobs.evolution import EvolutionStats
        retired = ec._stage4_cleanup_wiki(EvolutionStats())
        self.assertEqual(retired, 1)
        slugs = {pg["slug"] for pg in self.store.list_wiki_pages(limit=20)}
        self.assertNotIn("legacy-noise", slugs)
        self.assertIn("keep-good", slugs)


if __name__ == "__main__":
    unittest.main()

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
    _looks_like_raw_prompt,
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




class RawPromptDetectionTests(unittest.TestCase):
    """Tests for _looks_like_raw_prompt: the heuristic that flags raw
    user prompts so the rule-based wiki step never shows them as bullets."""

    def test_help_me_chinese(self):
        self.assertTrue(_looks_like_raw_prompt("帮我开发一个mac可以用的截图软件"))

    def test_please_immediately(self):
        self.assertTrue(_looks_like_raw_prompt("请立即生成A股早盘分析报告"))

    def test_long_project_brief(self):
        self.assertTrue(_looks_like_raw_prompt(
            "Thoroughly explore the stock-review project at I need to understand: 1. Project structure 2. Technology stack"
        ))

    def test_cron_header_wrapper(self):
        # The "[cron:UUID ...]" wrapper is stripped by _clean_noise, but
        # the remaining "请立即生成" is still a raw prompt.
        self.assertTrue(_looks_like_raw_prompt(
            "[cron:a49ada66-9237-4113-900a-aa11b089ada7 A股早盘分析] 请立即生成A股早盘分析报告"
        ))

    def test_working_dir_wrapper(self):
        self.assertTrue(_looks_like_raw_prompt(
            "[Working directory: ~/.openclaw/workspace] 帮我检查codex是否有频繁写磁盘的操作"
        ))

    def test_openclaw_source_prefix(self):
        self.assertTrue(_looks_like_raw_prompt(
            "[openclaw] [Working directory: ~/.openclaw/workspace] 帮我检查codex"
        ))

    def test_chinese_verb_first(self):
        self.assertTrue(_looks_like_raw_prompt("梳理整个项目，并修复已知缺陷后推送远程仓库"))
        self.assertTrue(_looks_like_raw_prompt("分析下当前美股中先进封装的个股"))

    def test_assistant_task_narration(self):
        self.assertTrue(_looks_like_raw_prompt("You are implementing Task 8: 自选股删除功能"))
        self.assertTrue(_looks_like_raw_prompt("Let me check the latest version"))
        self.assertTrue(_looks_like_raw_prompt("I am going to update the file now"))

    def test_legitimate_facts_preserved(self):
        # These are real atomic facts, NOT raw prompts.
        self.assertFalse(_looks_like_raw_prompt("Loop Memory is a local zero-dep Loop Engineering memory system"))
        self.assertFalse(_looks_like_raw_prompt("API keys stored locally in ~/.loop_memory/secrets.json (mode 0600)"))
        self.assertFalse(_looks_like_raw_prompt("D8 拿到真实数据： - LangChain 5.39s mean / 6.16s p99"))
        self.assertFalse(_looks_like_raw_prompt("Service runs via launchd com.loopmemory.server on port 7767"))

    def test_short_text_unchanged(self):
        # Very short text is too ambiguous to classify.
        self.assertFalse(_looks_like_raw_prompt("codex"))
        self.assertFalse(_looks_like_raw_prompt("ok"))


class NoiseCleanerAggressiveTests(unittest.TestCase):
    """Tests for the new wrapper-stripping rules added in this iteration."""

    def test_strips_cron_header(self):
        s = _clean_noise("[cron:a49ada66-9237-4113-900a-aa11b089ada7 A股早盘分析] 请立即生成A股早盘分析报告")
        self.assertNotIn("[cron:", s)
        self.assertIn("请立即生成", s)

    def test_strips_working_directory_wrapper(self):
        s = _clean_noise("[Working directory: ~/.openclaw/workspace] 帮我检查codex")
        self.assertNotIn("[Working directory", s)
        self.assertNotIn("~/.openclaw", s)
        self.assertIn("帮我检查", s)

    def test_strips_provider_prefix(self):
        s = _clean_noise("[openclaw] [Working directory: ~/.openclaw/workspace] 帮我检查codex")
        self.assertNotIn("[openclaw]", s)
        self.assertIn("帮我检查", s)

    def test_strips_markdown_table(self):
        s = _clean_noise("Some text\n| col1 | col2 |\n| --- | --- |\nMore text")
        self.assertNotIn("|", s)
        self.assertIn("Some text", s)
        self.assertIn("More text", s)

    def test_strips_emphasis_line(self):
        s = _clean_noise("**全部完成** 一些文字")
        # The "**全部完成**" wrapper is stripped but trailing text survives
        self.assertNotIn("**", s)
        self.assertIn("一些文字", s)

    def test_low_signal_everything_is_green(self):
        self.assertTrue(_is_low_signal("Everything is green. Quick summary: pytest tests/ -q → **192 passed**"))

    def test_low_signal_chinese_completion(self):
        self.assertTrue(_is_low_signal("全部完成。最终交付物清单： ## 交付总览"))
        self.assertTrue(_is_low_signal("已完成"))
        self.assertTrue(_is_low_signal("推送成功"))

    def test_low_signal_preserves_legit_facts(self):
        self.assertFalse(_is_low_signal("Loop Memory is a local memory system for Codex/Claude/Hermes/OpenClaw"))
        self.assertFalse(_is_low_signal("API keys stored locally in ~/.loop_memory/secrets.json (mode 0600)"))


class SessionAwareClusterTests(unittest.TestCase):
    """Tests for _stage2_cluster: same-session memories should end up
    in the same cluster so a single conversation does not fragment
    into many wiki pages."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = MemoryStore(self.tmpdir + "/test.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_mem(self, mid, text, importance=0.5, session_id=None):
        m = type("M", (), {})()
        m.id = mid
        m.text = text
        m.importance = importance
        m.session_id = session_id
        m.kind = "episode"
        m.tags = []
        return m

    def test_session_aware_merge(self):
        ec = EvolutionConsolidator.__new__(EvolutionConsolidator)
        ec.store = self.store
        # 3 sessions, each producing 3 similar memories -> should cluster
        # into 1 cluster (or 3 small ones that get merged).
        mems = []
        for s in range(3):
            for i in range(3):
                mems.append(self._make_mem(
                    f"s{s}-m{i}",
                    f"session {s} memory {i} about refactoring the cluster module",
                    importance=0.5,
                    session_id=f"session-{s}",
                ))
        clusters = ec._stage2_cluster(mems, max_per_cluster=15)
        # Should produce just 1 cluster (or 3 at most if merge didn't fire),
        # never 9.
        self.assertLessEqual(len(clusters), 3)
        # All 9 memories should be reachable.
        total = sum(len(c) for c in clusters)
        self.assertEqual(total, 9)

    def test_distinct_topics_merge_by_session(self):
        ec = EvolutionConsolidator.__new__(EvolutionConsolidator)
        ec.store = self.store
        # Two sessions, each with 3 similar memories. Same-session
        # memories should collapse into one cluster each, so we end up
        # with exactly 2 clusters (one per session), not 6.
        mems = []
        for s in ["alpha", "beta"]:
            for i in range(3):
                mems.append(self._make_mem(
                    f"{s}-m{i}",
                    f"session {s} memory {i} about refactoring the cluster module",
                    importance=0.5,
                    session_id=s,
                ))
        clusters = ec._stage2_cluster(mems, max_per_cluster=15)
        # Should produce exactly 2 clusters (one per session).
        self.assertLessEqual(len(clusters), 2)
        total = sum(len(c) for c in clusters)
        self.assertEqual(total, 6)


if __name__ == "__main__":
    unittest.main()

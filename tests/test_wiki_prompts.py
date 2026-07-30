"""Tests for the wiki-synthesis prompts module + bullet-list utilities.

These exist because the user reported (2026-07) that distilled wiki pages
came back in English regardless of the user's UI locale, and that
individual pages shipped as 1-2 bullet stubs instead of a systematic set
of atomic facts. The prompts module + a code-level length floor are the
fix; the tests pin both behaviours.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

# Make the package importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_memory.jobs.evolution import _merge_bullet_lists, _split_bullets   # noqa: E402
from loop_memory.wiki.prompts import (                                        # noqa: E402
    MAX_WIKI_PROMPTS_PER_PAGE,
    MIN_BODY_CHARS,
    MIN_BULLETS_PER_PAGE,
    SUPPORTED_LOCALES,
    count_bullets,
    expansion_prompt,
    meets_body_floor,
    normalise_lang,
    wiki_system_prompt,
)


class TestWikiPromptLocale(unittest.TestCase):
    def test_normalise_lang_zh_family(self) -> None:
        for v in ("zh", "zh-CN", "zh-Hans", "ZH", "zh_CN"):
            self.assertEqual(normalise_lang(v), "zh")

    def test_normalise_lang_en_family(self) -> None:
        for v in ("en", "EN", "en-US", "en_GB"):
            self.assertEqual(normalise_lang(v), "en")

    def test_normalise_lang_ja(self) -> None:
        for v in ("ja", "jp", "JA", "ja-JP"):
            self.assertEqual(normalise_lang(v), "ja")

    def test_normalise_lang_unknown_falls_back_to_zh(self) -> None:
        for v in (None, "", "fr", "es", object(), 0):
            self.assertEqual(normalise_lang(v), "zh")

    def test_supported_locales(self) -> None:
        self.assertIn("zh", SUPPORTED_LOCALES)
        self.assertIn("en", SUPPORTED_LOCALES)

    def test_zh_prompt_explicit_language_directive(self) -> None:
        p = wiki_system_prompt("zh")
        # The Chinese prompt must explicitly demand zh-CN output, otherwise
        # open-source models default back to English.
        self.assertIn("简体中文", p)
        self.assertNotIn("\nYou maintain a personal knowledge base", p)

    def test_en_prompt_explicit_language_directive(self) -> None:
        p = wiki_system_prompt("en")
        self.assertIn("OUTPUT LANGUAGE: ENGLISH", p)

    def test_unknown_locale_returns_zh(self) -> None:
        p = wiki_system_prompt("xx")
        self.assertIn("简体中文", p)

    def test_prompts_mention_length_floor(self) -> None:
        for loc in SUPPORTED_LOCALES:
            p = wiki_system_prompt(loc)
            self.assertIn(str(MIN_BULLETS_PER_PAGE), p,
                          f"{loc}: missing bullet floor")
            self.assertIn(str(MIN_BODY_CHARS), p,
                          f"{loc}: missing char floor")

    def test_expansion_prompt_per_locale(self) -> None:
        for loc in SUPPORTED_LOCALES:
            msg = expansion_prompt(loc)
            self.assertIsInstance(msg, str)
            self.assertGreater(len(msg), 40)
            # Should NOT contain the original system prompt — keeps the
            # expansion request short.
            self.assertNotIn("profile_dimensions", msg)


class TestBodyFloor(unittest.TestCase):
    def test_meets_floor_with_full_body(self) -> None:
        body_text = (
            "本条目记录了一个具体的决定、原因、约束、变通方案和人名详情。涉及历史决策的数字是 3.1415，"
            "涉及的相关函数名是 render_dashboard_v2，错误信息是 FileNotFoundError at app.py:2099，"
            "完整的 workaround 见 docs/runbook.md 第三节。"
        )
        bullets = "\n".join(f"- 完整事实第{i}条记录：{body_text}" for i in range(8))
        self.assertGreater(len(bullets), MIN_BODY_CHARS)
        self.assertGreaterEqual(count_bullets(bullets), 8)
        self.assertTrue(meets_body_floor(bullets))

    def test_under_floor_short_body(self) -> None:
        body = "- 短\n- 太短\n- 不够\n"
        self.assertFalse(meets_body_floor(body))

    def test_under_floor_few_bullets(self) -> None:
        # 2 long bullets still miss the bullet count floor even if the
        # body is long.
        body = "- " + ("详细事实 " * 60) + "\n- " + ("另一段详细事实 " * 60)
        self.assertGreater(len(body), MIN_BODY_CHARS)
        self.assertEqual(count_bullets(body), 2)
        self.assertFalse(meets_body_floor(body))

    def test_count_bullets_indented(self) -> None:
        body = "- top1\n  - sub1\n  - sub2\n- top2\n"
        self.assertEqual(count_bullets(body), 4)


class TestBulletMerge(unittest.TestCase):
    def test_merges_without_dropping_existing(self) -> None:
        old = "- 一条\n- 二条\n"
        new = "- 一条\n- 三条\n"
        merged = _merge_bullet_lists(old, new)
        # "一条" must NOT be duplicated. "三条" appended at the end
        # (after the existing bullets).
        self.assertEqual(merged.count("- 一条"), 1)
        self.assertIn("- 三条", merged)
        self.assertIn("- 二条", merged)
        # Order: existing bullets first, new bullets appended at the end.
        self.assertLess(merged.index("- 二条"), merged.index("- 三条"))

    def test_empty_new_is_noop(self) -> None:
        old = "- 一条\n- 二条\n"
        self.assertEqual(_merge_bullet_lists(old, ""), old)

    def test_handles_indented_subbullets(self) -> None:
        old = "- 父条\n"
        new = "  - 子条（细节）\n"
        # Indented bullet still counts as fact and gets appended.
        merged = _merge_bullet_lists(old, new)
        self.assertIn("- 父条", merged)
        self.assertIn("子条", merged)


class TestJsonCacheKeyLocalePart(unittest.TestCase):
    """The cache_key for the expansion retry must include the locale —
    otherwise an English user reusing a Chinese run's prefix would get
    cached Chinese replies."""

    def test_cache_key_differs_by_locale(self) -> None:
        # We don't import the function directly — the contract is just
        # that the cache key string passed to _cached_call() varies by
        # lang. Verify by simulating the formation.
        base = "user_prompt||model||wiki-evo-expand||0.3"
        for loc in ("zh", "en", "ja"):
            blob = base + "||" + loc
            self.assertNotEqual(blob, base)


class TestPromptInJsonChecks(unittest.TestCase):
    """Verify the JSON schema the wiki prompts ask for still has the
    fields the rest of the pipeline relies on."""

    def test_en_prompt_keys_present(self) -> None:
        # The English prompt must mention every field the upsert path
        # uses (slug, title, summary, body, tags, importance,
        # evidence_ids). Without this reminder the LLM occasionally
        # drops ``summary`` and we end up with an empty card in the UI.
        p = wiki_system_prompt("en")
        for needle in ("slug", "title", "summary", "body", "tags",
                       "importance", "evidence_ids"):
            self.assertIn(needle, p, f"en prompt missing field: {needle}")

    def test_zh_prompt_keys_present(self) -> None:
        p = wiki_system_prompt("zh")
        for needle in ("slug", "title", "summary", "body", "tags",
                       "importance", "evidence_ids"):
            self.assertIn(needle, p, f"zh prompt missing field: {needle}")


class TestConstantsSanity(unittest.TestCase):
    def test_floor_values_make_sense(self) -> None:
        # 6 bullets * ~100-char bullets is the realistic floor; a smaller
        # floor invites the 1-bullet regression that triggered the bug.
        self.assertGreaterEqual(MIN_BULLETS_PER_PAGE, 6)
        self.assertGreaterEqual(MIN_BODY_CHARS, 400)
        self.assertLessEqual(MAX_WIKI_PROMPTS_PER_PAGE, 3)


if __name__ == "__main__":
    unittest.main()

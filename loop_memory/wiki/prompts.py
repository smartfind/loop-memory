"""Wiki synthesis prompts (multi-locale) and length-floor helpers.

Why this module exists
----------------------
The legacy prompt strings lived inline at the top of ``evolution.py``
and ``llm_consolidate.py``. Two consequences:

1. They were written in English without any directive about output
   language, so even Chinese-language users got distilled wiki pages
   in English (the assistant's "default" tone). The user reported
   "知识库中浓缩的知识为何全是英文，单个知识偏短不成体系" — every
   distilled page came out in English, individual pages were 1-2
   bullets long, and there was no systematic structure across the
   wiki.

2. There was no length floor — the LLM could produce a 2-bullet page
   ("- we use SQLite") even when a richer answer was in the source
   memories.

This module centralises the wiki-synthesis prompts in three
languages (Chinese, English, with a Japanese fallback when the user's
``lang`` is ja). All Stage-4 callers should pick a prompt through
``wiki_system_prompt(lang)`` rather than referencing the legacy
``_WIKI_SYSTEM`` constant directly.

The length / bullet floors (``MIN_BULLETS_PER_PAGE`` /
``MIN_BODY_CHARS``) are exported so a post-processing pass in
``evolution.py`` can re-prompt the LLM when a returned page comes
back too thin to be useful. The "completeness over compactness"
v2 invariant now lives in code, not just in the prompt text.
"""

from __future__ import annotations

import re as _re

# ----------------------------------------------------------------------------
# Length / bullet floors (v3 — code-level guarantee on top of prompt-level)
# ----------------------------------------------------------------------------

#: Minimum number of bullets the LLM must emit for a non-trivial wiki
#: page. ``evolution._stage4_wiki_synthesis`` will re-issue one
#: expansion prompt when a returned page falls below this floor before
#: falling back to the deterministic rule-based synthesizer.
MIN_BULLETS_PER_PAGE = 6

#: Minimum body length in characters. Mirrors ``MIN_BULLETS_PER_PAGE``
#: for prose-style summaries so a page can't sneak past the bullet
#: count by emitting six 3-character bullets.
MIN_BODY_CHARS = 600

#: When the first LLM pass returns a page below the floor, the
#: consolidator retries once with an expansion instruction before
#: giving up. ``MAX_WIKI_PROMPTS_PER_PAGE = 2`` keeps the worst-case
#: LLM bill under control even on a chatty session.
MAX_WIKI_PROMPTS_PER_PAGE = 2


# ----------------------------------------------------------------------------
# Locale helpers
# ----------------------------------------------------------------------------

#: Supported output locales. Anything outside this set falls back to
#: English. The default user-facing locale is ``zh`` because the
#: default UI language is Chinese; English pages come back when the
#: user explicitly switches the toggle.
SUPPORTED_LOCALES = ("zh", "en", "ja")


def normalise_lang(lang: object | None) -> str:
    """Return one of :data:`SUPPORTED_LOCALES`. Unknown / empty → ``zh``."""
    if not lang:
        return "zh"
    s = str(lang).strip().lower()
    if s.startswith("zh"):
        return "zh"
    if s.startswith("en"):
        return "en"
    if s.startswith("ja") or s.startswith("jp"):
        return "ja"
    return "zh"


# ----------------------------------------------------------------------------
# Shared prompt structure
# ----------------------------------------------------------------------------
#
# Both locales stress the same two invariants:
#
#   * **Completeness over compactness.** Every atomic fact (number,
#     name, decision, error message, workaround) in the source MUST
#     land on at least one bullet.
#   * **Systematic, not chatty.** Each page is a 6-20 bullet cluster
#     grouped by *user-profile dimension* (preferences, decisions,
#     projects, domain, feedback). Sub-bullets (``  -`` indent) are
#     encouraged when a single sentence carries two related facts.
#
# Locale-specific wordings live in small f-strings so a contributor
# can read both side by side and catch drift.

_EN_PROMPT = (
    "You maintain a personal knowledge base for ONE user. You receive the user's "
    "existing wiki pages plus a batch of distilled cluster summaries (each already "
    "filtered for noise).\n"
    "GOAL: each page must be a COMPLETE, ACTIONABLE, SYSTEMATIC note the user "
    "would actually want to recall later — NOT a quote of the original "
    "conversation, and NOT a half-fact that loses the actionable detail.\n"
    "OUTPUT LANGUAGE: ENGLISH. Every value of `title`, `summary`, `body`, "
    "`tags`, `key_facts`, and `slug` MUST be in English. Do NOT switch to "
    "Chinese or any other language even if the source memories were mixed.\n"
    "COMPLETENESS OVER COMPACTNESS: if a cluster contains a number, a name, a "
    "decision, a constraint, an error message, or a workaround, that detail "
    "MUST land on the relevant page. The user will rely on this knowledge base "
    "to skip re-deriving facts. Losing a fact is much worse than a longer page.\n"
    "ALWAYS bucket into one of these dimensions, and make the slug reflect the topic:\n"
    "  preferences-<topic>, decision-<topic>, project-<topic>, domain-<topic>, "
    "feedback-<topic>. Examples: 'prefers-dark-mode', 'decision-batch-size-50', "
    "'project-loop-memory', 'domain-crypto-swing-trades', 'feedback-no-mixed-lang'.\n"
    "Each page MUST have:\n"
    "  slug: lowercase, hyphen-separated, prefixed with the dimension; no hard "
    "length cap, but keep it readable in a URL. NEVER a truncated user prompt.\n"
    "  title: a real noun phrase that names the topic; no hard length cap, but "
    "keep it under ~12 words. NEVER a truncated user prompt.\n"
    "  summary: 1-3 sentence definition that stands on its own; no hard length "
    "cap, no truncation mid-clause, MUST be understandable without the source.\n"
    "  body: bullet-point markdown. Each bullet MUST start with '- '. One "
    "atomic fact per bullet. NO hard cap on bullet count — use as many bullets "
    "as the source clusters justify. SYSTEMATIC pages MUST have at least "
    f"{MIN_BULLETS_PER_PAGE} bullets and at least {MIN_BODY_CHARS} characters "
    "of body content; if the source is thinner, prefer to MERGE it into an "
    "existing page rather than emit a half-fact single-bullet stub. Sub-bullets "
    "(indented with two spaces: '  -') are encouraged when one fact has two "
    "halves (e.g. 'uses X for Y' / 'uses Z for W'). Every decision, number, "
    "name, error, constraint, or workaround from the source MUST appear in at "
    "least one bullet. No prose paragraphs. No 'Outcome: ...' echoes. Code "
    "fences ONLY when the fact is literally a command or config snippet.\n"
    "  tags: 3-6 lowercase tags, snake_case\n"
    "  importance: 0..1 (1 = critical user preference/project, 0.3 = transient detail)\n"
    "  evidence_ids: list of memory ids that back this page (cite real ids from the input)\n"
    "SKIP a cluster summary if it is just a user prompt, status update, or repeats "
    "another cluster. PREFER updating an existing page (same slug) over creating "
    "a near-duplicate — when updating, APPEND new atomic facts rather than "
    "rewriting existing ones, so cumulative knowledge is preserved. Reply with "
    "JSON: {\"pages\": [...]}. If nothing adds new info, reply {\"pages\": []}. "
    "No prose, no markdown outside the JSON."
)


_ZH_PROMPT = (
    "你为同一名用户维护一份个人长期知识库。你会拿到用户的已有 wiki 页面 + 一组 "
    "经过过滤的「簇摘要」(Stage 3 已剔除噪音)。\n"
    "目标：每一页都必须是【完整 + 可执行 + 成体系】的笔记，用户日后回查能直接用 "
    "—— 不要复述对话，也不要丢掉任何关键事实。\n"
    "**输出语言必须使用简体中文（zh-CN）**。所有的 title / summary / body / "
    "tags / key_facts / slug 字段都必须用中文。即便源记忆里有英文术语或代码 "
    "片段，正文叙述仍用中文表达（专有名词可保留英文原文，但不要整页变成英文）。\n"
    "**完整性优先于简洁性**：簇里出现的数字、人名、决定、约束、报错、workaround "
    "必须落到对应页的某一条 bullet 上。用户依赖这份知识库减少重复推导，丢掉任 "
    "何一条事实都比写长一点更糟糕。\n"
    "**结构化成体系**：每页都按以下 5 个用户画像维度之一归类，slug 直接拼上主题：\n"
    "  preferences-<主题>、decision-<主题>、project-<主题>、domain-<主题>、"
    "feedback-<主题>。例如 'prefers-dark-mode'、'decision-batch-size-50'、"
    "'project-loop-memory'、'domain-crypto-swing-trades'、'feedback-no-mixed-lang'。\n"
    "每一页必须包含以下字段：\n"
    "  slug：小写，连字符分隔，前缀为维度；不做硬切，但 URL 要可读；禁止截断的原始用户输入。\n"
    "  title：能直接命名主题的名词短语，不做硬切，控制在 12 个词以内；禁止截断的原始用户输入。\n"
    "  summary：1-3 句能独立成立的概要；不做硬切、不在分句中间截断；脱离原文也要能读懂。\n"
    f"  body：项目式 Markdown。每条 bullet 必须以 '- ' 开头。一条 bullet 一个原子事实。"
    f"不做硬性 bullet 数上限，但**系统性页面至少 {MIN_BULLETS_PER_PAGE} 条 bullet、"
    f"正文至少 {MIN_BODY_CHARS} 个字符**；如果源材料偏薄，宁可并入已有页面，也不要"
    f"生成只有一两条 bullet 的单薄页面。当一条事实包含两半内容（例如'用 X 做 Y，用 Z 做 W'）"
    f"时，强烈推荐用两层 bullet 缩进（'  -'）。源材料里的每一个决定、数字、"
    f"名字、报错、约束、workaround 必须出现在至少一条 bullet 上。禁止整段散文；"
    f"禁止 'Outcome: ...' 之类回声；只有在事实本身就是命令/配置片段时才用代码块。\n"
    "  tags：3-6 个小写标签，snake_case 风格。\n"
    "  importance：0..1（1=关键用户偏好/项目决定，0.3=临时细节）。\n"
    "  evidence_ids：引用来源记忆 ID 列表（用输入中真实存在的 ID）。\n"
    "**跳过**单纯复述用户问题、状态更新或重复其他簇的摘要。**优先更新已有页面**"
    "（同 slug）而不是新建近似页面；更新时只追加新事实，不要重写已有事实，"
    "以保留累积知识。回复 JSON: {\"pages\": [...]}。若没有新增信息，回复 "
    "{\"pages\": []}。不要在 JSON 之外输出任何说明文字或 Markdown。"
)


_JA_PROMPT = (
    "あなたは一人のユーザーのための個人ナレッジベースを維持します。入力はユーザーの"
    "既存 wiki ページ群と、Stage 3 でノイズ除去済みのクラスター要約のバッチです。\n"
    "ゴール: 各ページはユーザーが後で再参照したい【完全で・実用的・体系的な】"
    "メモでなければなりません。元の発言の引き写しでも、要点だけ抜いた薄い"
    "ものでもいけません。\n"
    "**出力言語は日本語 (ja-JP) とします**。title / summary / body / tags / "
    "key_facts / slug のすべての値を日本語で記述してください。クラスター内に"
    "英語の専門用語が混ざっていても、説明は日本語で書いてください（固有名詞は"
    "原文のまま可）。\n"
    "**完全性 > 簡潔性**: 数字・名前・決定・制約・エラーメッセージ・回避策は"
    "全て、該当ページの bullet に必ず 1 ヶ所以上載せてください。\n"
    "**体系化**: 以下の 5 つのユーザープロファイル次元のいずれかに分類し、"
    "slug にその次元を付けてください: preferences-, decision-, project-, "
    "domain-, feedback-。\n"
    "各ページに必須のフィールド:\n"
    "  slug: 小文字ハイフン区切り、次元を前置。URL として読める長さに。"
    "  title: トピックを直接表す名詞句、12 語以内。\n"
    f"  body: Markdown の箇条書き。各 bullet は '- ' で始める。一つの bullet に"
    f"一つのアトミックな事実。**体系的なページでは最低 {MIN_BULLETS_PER_PAGE} "
    f"個の bullet と {MIN_BODY_CHARS} 文字以上**を必須とします。"
)


# Length-floor instruction injected into a second-pass expansion prompt when
# the first LLM reply comes back thinner than the floors. We keep it short
# and locale-aware because the synthesiser copies it verbatim onto the
# end of the first reply's conversation.

def expansion_prompt(lang: str) -> str:
    """Locale-specific text that asks the LLM to expand an under-floor page."""
    loc = normalise_lang(lang)
    if loc == "zh":
        return (
            "上面给出的页面太短，未达到成体系的最低要求（bullet 数 / 正文字符数）。"
            "请在该页面下追加新的 bullet，每条 bullet 仍以 '- ' 开头，承载一条原子事实，"
            "尽量从所有提供的 cluster summary 里抽取尚未落入本页的关键细节（数字、"
            "名字、决定、报错、workaround）。不要重写或删除已有 bullet，只在末尾追加。"
            "回复 JSON：{\"pages\": [{...原页面 + 新 bullet + 同一 slug/title/summary...}]}。"
        )
    if loc == "ja":
        return (
            "前述ページは最低要件 (bullet 数 / 本文文字数) を満たしていません。"
            "ページ末尾に新しい bullet を追加してください。'- ' 始まり、各 bullet 1 事実。"
            "提供された全クラスター要約から未取込みの重要事項（数値・名前・決定・"
            "エラー・回避策）を抽出すること。既存 bullet の書き換えや削除は禁止、"
            "末尾追加のみ。JSON のみ返す。"
        )
    # English / default fallback
    return (
        "The page returned above is below the systematic floor (bullet count "
        "and/or body character count). Please append new bullets to that same "
        "page. Each new bullet MUST start with '- ' and MUST carry ONE atomic "
        "fact. Pull from every cluster summary that has details not yet on the "
        "page (numbers, names, decisions, errors, workarounds). Do NOT rewrite "
        "or drop existing bullets — only append. Reply as JSON: "
        "{\"pages\": [{...the same page with new bullets appended, "
        "keeping slug / title / summary unchanged...}]}."
    )


# ----------------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------------

_PROMPTS = {"zh": _ZH_PROMPT, "en": _EN_PROMPT, "ja": _JA_PROMPT}


def wiki_system_prompt(lang: object | None = None) -> str:
    """Return the Stage-4 wiki system prompt for the requested locale.

    Pass ``store.lang`` / behaviour lang through here. Falls back to
    Chinese (the default UI language) when ``lang`` is missing or
    unsupported. Result is cached so repeated calls in the same process
    don't re-tokenise the prompt.
    """
    key = normalise_lang(lang)
    return _PROMPTS[key]


# ----------------------------------------------------------------------------
# Body-floor measurement
# ----------------------------------------------------------------------------

_BULLET_LINE_RE = _re.compile(r"^\s*-\s+\S", _re.MULTILINE)


def count_bullets(body: str) -> int:
    """Count user-visible bullet items in a wiki body.

    Recognises both ``- `` (most wiki pages) and ``* `` bullets. Indented
    sub-bullets count too — they're still atomic facts the user wants
    to recall later. Numbered lists don't count here; the prompt asks
    for ``-`` bullets only.
    """
    if not body:
        return 0
    n = 0
    for line in body.splitlines():
        if line.lstrip().startswith(("- ", "* ")):
            n += 1
    return n


def meets_body_floor(body: str, *, min_bullets: int = MIN_BULLETS_PER_PAGE,
                     min_chars: int = MIN_BODY_CHARS) -> bool:
    """True when ``body`` clears both bullet and character floors."""
    body = body or ""
    if len(body) < min_chars:
        return False
    return count_bullets(body) >= min_bullets

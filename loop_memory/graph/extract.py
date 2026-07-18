"""Lightweight entity + co-occurrence extractor (zero-dep, heuristic).

We don't pull in spaCy here. This module produces two things:

* ``extract_entities(text)`` → list of (name, kind) candidates, deduped
  and filtered by stopwords.  Recognised entities include Capitalised
  Latin tokens, ``#hashtag`` style tokens, file paths / URLs, CamelCase
  product names, and short CJK noun-like runs (after stopword filter).

* ``pair_cooccurrence(texts, window=...)`` → list of (a, b, count)
  relations where ``a`` and ``b`` appear within ``window`` tokens of
  each other in any of the input texts.

This is intentionally tunable — for higher quality call out to an
LLM-backed extractor and feed the output through the same API.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

# --- tokenisation -----------------------------------------------------------

_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_]+"                  # Latin word
    r"|\#[\w\u4e00-\u9fff]+"                  # #hashtag (English / CJK mix)
    r"|https?://[^\s]+"                       # URL
    r"|[A-Za-z0-9_./-]+\.[A-Za-z0-9]{2,}"      # file.ext or domain.tld
    r"|[\u4e00-\u9fff]{2,8}"                  # CJK noun runs (2–8 chars)
)


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


# --- stopword lists ---------------------------------------------------------

_EN_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "this", "that", "these",
    "those", "you", "your", "are", "was", "were", "have", "has", "had",
    "but", "not", "any", "all", "can", "could", "would", "should", "will",
    "shall", "may", "might", "must", "what", "why", "how", "who", "where",
    "when", "then", "now", "also", "still", "just", "very", "more",
    "less", "most", "least", "out", "off", "per", "via", "i", "me",
    "my", "we", "us", "our", "they", "their", "them", "he", "she", "his",
    "her", "is", "am", "be", "been", "being", "do", "does", "did", "done",
    "doing", "of", "in", "on", "at", "to", "by", "as", "an", "or", "if",
    "no", "yes", "so", "it", "its", "about", "than", "there", "here",
    "above", "below", "under", "over", "again", "once", "each",
    "something", "anything", "everything", "nothing", "some",
    "which", "such", "after", "before",
}

# Common short CJK fragments that aren't real concepts.
_CJK_STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "你", "他", "她", "它",
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "为什么",
    "可以", "可能", "也许", "应该", "已经", "现在", "之前", "之后",
    "因为", "所以", "如果", "但是", "不过", "然后", "可是", "而且",
    "或者", "还有", "也", "都", "就", "才", "只", "再", "又", "很",
    "非常", "比较", "一点", "一下", "一直", "顺便", "帮我", "我用",
    "你用", "对她", "我对", "是不是",
    "用", "打", "做", "搞", "弄", "给", "让", "把", "被", "由",
    "从", "到", "向", "对", "跟", "比", "如", "若", "虽", "除非",
    "将", "会", "能", "须", "必", "得", "地", "着", "过", "如何", "怎样", "为啥", "的工具", "的项目", "的系统", "的代码", "的内容", "的功能",
    "一个", "一些", "这些", "那些", "今天",
    "明天", "昨天", "谢谢", "感谢", "麻烦", "请帮",
}

_STOPWORDS = _EN_STOPWORDS


# --- detection --------------------------------------------------------------




# Extra stopwords: tokens that come up very frequently as filler in
# extracted LLM-style text — they tend to drown out useful entities.
_GENERIC_TERMS = {
    "User", "Users", "Assistant", "Outcome", "Task", "Description",
    "Issues", "Issue", "Files", "File", "Strengths", "Added",
    "Required", "Implemented", "Complete", "Check", "Review",
    "Code", "Title", "Body", "Read", "Note", "Source", "Target", "Message", "Context", "Result", "Output", "Input", "Step", "Steps", "List", "Section",
    "Project", "Repository", "Repo", "Doc", "Documentation",
    "Docs", "URL", "Path", "Line", "Lines", "Type", "Method",
    "Function", "Class", "Module", "Package", "Import", "Export",
    "Example", "Examples", "Sample", "Demo", "Use", "Using", "Used",
    "Make", "Made", "Run", "Running", "Create", "Creates",
    "Created", "Add", "Adding", "Remove", "Removed",
    "Removing", "Update", "Updated", "Updating", "Change", "Changes",
    "Changed", "Show", "Showing", "Found", "Founding", "Missing",
    "First", "Second", "Third",
    "Test", "Tests", "Tested", "Testing", "Pass", "Passes",
    "Status", "OK", "Ok", "Notes", "Comments", "Comment",
    "Done", "Completed", "Completion",
}

def _looks_like_proper_noun(tok: str) -> bool:
    """True for tokens that may be useful named-entity material."""
    if len(tok) < 2:
        return False
    if tok.isupper() and len(tok) <= 5:
        return tok.isalpha()  # ACRONYM
    if tok[0].isupper() and any(c.islower() for c in tok[1:]):
        return True
    if any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok):
        return True  # CamelCase
    return False


def _kind_for(tok: str) -> str:
    if tok.startswith("#"):
        return "tag"
    if tok.startswith("http"):
        return "url"
    if "." in tok and "/" in tok:
        return "path"
    if tok.isupper():
        return "acronym"
    if any(ord(c) > 127 for c in tok):
        return "cjk"
    return "concept"


# Heuristic disqualifiers for CJK tokens: any of these characters in
# any position is a strong signal the n-gram is not a real concept.
_CJK_FRAGMENT_MARKERS = set("的了着过得把被让给向将从跟比和或而但所以因为")     | set("是吗呀啊嘛呢哦嗯哈呀嘛啊")


def _is_useful(tok: str) -> bool:
    if not tok:
        return False
    lower = tok.lower()
    if lower in _STOPWORDS:
        return False
    if tok in _CJK_STOPWORDS:
        return False
    if tok in _GENERIC_TERMS:
        return False
    # Latin: only proper-nounish tokens qualify.
    if ord(tok[0]) < 128:
        if not _looks_like_proper_noun(tok):
            return False
        return True
    # CJK token refinement
    if any(c in _CJK_FRAGMENT_MARKERS for c in tok):
        return False
    return True


def extract_entities(text: str, *, min_count: int = 1) -> list[tuple[str, str]]:
    if not text:
        return []
    counts: Counter = Counter()
    for tok in tokenize(text):
        tok = tok.strip("'\"`")
        if _is_useful(tok):
            counts[tok] += 1
    return [
        (tok, _kind_for(tok))
        for tok, n in counts.items()
        if n >= min_count
    ]


def pair_cooccurrence(
    texts: Iterable[str],
    *,
    window: int = 6,
    min_count: int = 1,
) -> list[tuple[str, str, int]]:
    pair_counts: Counter = Counter()
    for text in texts:
        toks = [tok for tok in tokenize(text) if _is_useful(tok)]
        seen_local: set = set()
        for i, tok in enumerate(toks):
            for other in toks[i + 1 : i + window]:
                if other == tok:
                    continue
                key = tuple(sorted([tok, other]))
                if key in seen_local:
                    continue
                seen_local.add(key)
                pair_counts[key] += 1
    return [(a, b, c) for (a, b), c in pair_counts.items() if c >= min_count]


def canonical(name: str) -> str:
    if not name:
        return name
    if ord(name[0]) >= 128:
        return name
    return name.strip()

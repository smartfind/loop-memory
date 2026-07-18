"""Evolution Consolidator — a 5-stage distillation pipeline.

Replaces the old single-pass LLM consolidator with a hierarchical,
feedback-driven loop. The pipeline is:

  Stage 1  Signal-Aware Scoring
           Each memory's importance is blended with its behavioural
           signals (recall_count, positive/negative feedback). This
           makes "things the user actually uses" float to the top.

  Stage 2  Semantic Batching
           Memories are embedded (or use a hashed fallback) and
           clustered into K buckets via greedy cosine clustering.
           Each cluster <= CLUSTER_MAX so the LLM never sees too much
           at once and the topic stays focused.

  Stage 3  Per-Cluster Distillation
           For each cluster we ask the LLM to produce:
             * a 1-sentence cluster summary
             * a refined importance per row
             * a list of "keep / drop / rewrite" actions
           This is the cheap-per-cluster pass that decides what's
           noise vs. signal.

  Stage 4  Hierarchical Wiki Synthesis
           Cluster summaries + the user's existing wiki pages are
           passed to the LLM, which produces/updates wiki pages
           grouped by *user-profile dimension*:
             - preferences   (how the user likes things done)
             - decisions     (concrete choices the user made)
             - projects      (ongoing work / topics)
             - domain        (technical knowledge to keep)
             - feedback      (corrections, dislikes, do/don't)
           Re-running merges with existing wiki pages by slug.

  Stage 5  Evolution Memo
           We persist a short "evolution memo" (which wiki pages
           changed, how much importance shifted, what signals were
           used). The next run's Stage 4 prompt includes the memo so
           the LLM keeps learning the user's preferences across runs
           without us having to retrain anything.

The consolidator is fully optional: the existing single-pass
``LLMConsolidator`` still works and is what the UI's "AI Consolidate"
button calls by default. ``EvolutionConsolidator`` is wired in as an
opt-in mode so the user can A/B compare.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import ChatHistory, LLMClient, Message
from ..storage.sqlite_store import MemoryStore, StoredMemory

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

CLUSTER_MAX = 15              # max memories per cluster (Stage 3 prompt size)
WIKI_INPUT_CLUSTERS = 8       # how many cluster summaries feed Stage 4
PROFILE_DIMS = ("preferences", "decisions", "projects", "domain", "feedback")

# ----------------------------------------------------------------------------
# Text noise cleaners (used by Stage 0 dedup + rule-based wiki fallback)
# ----------------------------------------------------------------------------

# Common assistant boilerplate / meta-narration to strip from raw memory text
# when synthesizing wiki bodies without an LLM. We keep the regex list small
# and targeted so a real, atomic fact still survives.
# Recognise the common "wrapper" prefixes the loop-memory hooks prepend
# to assistant text. We strip the whole wrapper so the atomic fact inside
# can survive, instead of being glued to cron ids / working-directory tags.
_NOISE_WRAPPER_PATTERNS = [
    # Cron-prompt headers: "[cron:UUID label] 请立即生成..."
    r"\[cron:[^\]]+\]",
    # Working-directory wrapper: "[Working directory: ~/.openclaw/workspace] 帮我..."
    r"\[Working directory:[^\]]*\]\s*",
    # Provider / source prefix: "[codex] ", "[claude] ", "[hermes] ", "[openclaw] "
    r"\[(?:codex|claude|hermes|openclaw|claude-code|chatgpt|hermes-cli)\][\s:：]+",
    # Outcome / Result / Status / Task / Latest / Output prefix
    r"^(?:Outcome|Result|Status|Task|Latest|Output|Reply|Response|Assistant|Human|User intent)\s*[:：]\s*",
    # "[thinking]" inline blocks: keep stripping
    r"\[thinking\][\s\S]*?\[/thinking\]",
    r"\[thinking\]\s*",
    # "You are an assistant..." tool-system narration
    r"You are an? [^.\n]{1,160}\.\s*",
    # "<cwd>", "<shell>", "<current_date>", "<permissions_instructions>" env tags
    r"<\w+>[\s\S]*?</\w+>",
    r"<\w+>\s*",
    # "**已完成**" / "**全部完成**" / "DONE" markers as standalone
    r"^\*\*(?:已完成|全部完成|DONE|DONE\.|OK|完成|完成\.)\*\*\s*[:：]?\s*",
]
_NOISE_PREFIX_RE = re.compile(
    "|".join(_NOISE_WRAPPER_PATTERNS),
    re.IGNORECASE,
)
_NOISE_CODE_FENCE_RE = re.compile(r"```[^`]*```", re.DOTALL)
_NOISE_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_NOISE_PATH_RE = re.compile(r"/Users/[^\s)\"<>]+")
_NOISE_URL_RE = re.compile(r"https?://[^\s)\"<>]+")
_NOISE_WS_RE = re.compile(r"[ \t]+")
_NOISE_NEWLINES_RE = re.compile(r"\n{2,}")
# Markdown table rows: "| col | col | col |" — never a fact
_NOISE_TABLE_ROW_RE = re.compile(r"(?:\|[^|\n]*)+\|")
# Markdown headings: "## Heading" or "### Heading"
_NOISE_HEADING_RE = re.compile(r"^#+\s+[^\n]+", re.MULTILINE)
# Markdown emphasis that wraps an entire short line: "**全部完成。**"
_NOISE_BOLD_LINE_RE = re.compile(r"^\*\*[^\n]{1,40}\*\*\s*[:：]?\s*", re.MULTILINE)
# Repeated punctuation runs (---, ====, *****)
_NOISE_RULE_RE = re.compile(r"^[-=*]{3,}\s*$", re.MULTILINE)


def _clean_noise(text: str, *, max_len: int = 240) -> str:
    """Strip the wrapper off a raw memory so the atomic fact can survive.

    The goal is *not* to summarize (the LLM does that); it is to remove the
    80% of characters that are pleasantries, code fences, narration, file
    paths, and tool chatter so a downstream bullet is readable on its own.

    Order matters: bigger wrappers (cron headers, env tags) first, then
    inline / line-level noise (markdown tables, bold-as-line, code).
    """
    if not text:
        return ""
    s = text
    # Drop code fences entirely (they are usually tool output, not a fact).
    s = _NOISE_CODE_FENCE_RE.sub(" ", s)
    # Markdown structural noise: tables, headings, hrules, whole-line bold
    s = _NOISE_TABLE_ROW_RE.sub(" ", s)
    s = _NOISE_RULE_RE.sub(" ", s)
    s = _NOISE_HEADING_RE.sub(" ", s)
    s = _NOISE_BOLD_LINE_RE.sub("", s)
    # Drop wrapper prefixes (cron, working directory, provider tags, [thinking],
    # <cwd> env tags, "Outcome: ", "User intent: ", etc.). Use a loop because
    # the same text can have multiple stacked wrappers ("[openclaw] [Working
    # directory: ...] 帮我...") and a single sub() only catches the first.
    for _ in range(4):
        prev_new = _NOISE_PREFIX_RE.sub("", s)
        if prev_new == s:
            break
        s = prev_new
    # Inline code: keep the inside, not the backticks.
    s = _NOISE_INLINE_CODE_RE.sub(r"\1", s)
    # Drop absolute home paths (these leak the user's filesystem, not a fact).
    s = _NOISE_PATH_RE.sub(" ", s)
    # Drop URLs (rarely a fact worth keeping in a wiki bullet).
    s = _NOISE_URL_RE.sub(" ", s)
    # Collapse whitespace.
    s = _NOISE_WS_RE.sub(" ", s)
    s = _NOISE_NEWLINES_RE.sub(" ", s)
    s = s.strip(" \t\n.,;:|/\u3000")
    # Final hard cap
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip(" ,;:|/") + "…"
    return s


# Patterns that mark a memory as pure status narration / completion ping
# with no atomic fact a wiki page should remember. Matched on the CLEANED
# (lowercased) form. The bar is: would a future user re-derive something
# they didn't already know? If the answer is no, drop it.
_LOW_SIGNAL_PATTERNS = (
    # English
    r"^tests? (is|are) green",
    r"^ci (passed|green|succeeded|is green)",
    r"^all (good|done|set|clear|green|passing|checks? pass)",
    r"^running\s*\.\.\.?\s*$",
    r"^done\.?$",
    r"^ok\.?$",
    r"^green\.?$",
    r"^passed\.?$",
    r"^succeeded\.?$",
    r"^pushed successfully",
    r"^now (commit|wait|push|run|deploy|test)",
    r"^everything is green",
    r"^all tests pass",
    r"^build (passed|succeeded|is green)",
    r"^lints? clean",
    r"^ruff (clean|is clean|ok)",
    r"^verified? (locally|and ready|ok)",
    # Chinese
    r"^全部完成",
    r"^已完成",
    r"^完成\.?",
    r"^已经完成",
    r"^都 (好|搞定|完成|通过了)",
    r"^测试 (通过|成功|过了|全过)",
    r"^跑通了?",
    r"^没问题",
    r"^好的",
    r"^已推送",
    r"^推送成功",
    r"^现在 (推送|提交|等待|运行|测试)",
    r"^任务完成",
    r"^看板 (已|现在) 显示",
    r"^一切 (正常|就绪|顺利)",
    r"^191/191",
    r"^\d+/\d+",
)

# Heuristic flags for RAW USER PROMPTS that should never become wiki
# bullets. These are conversational imperatives that wrap the user's
# request — useful for session replay, but they contain no atomic fact a
# future user (or model) would re-derive. Matched on the cleaned form.
_RAW_PROMPT_PATTERNS = (
    # "帮我..." (help me...)
    r"^帮我",
    r"\s帮我(?!知道|看一下|看看)",
    r"帮我(检查|梳理|分析|整理|写|生成|跑|修复|优化|测试|部署|调研|对比|总结|封装|压缩|转换|翻译)",
    # "请立即..." / "请..." (Please immediately)
    r"^请立即",
    r"^请(?!求|求你|告诉我)",
    # Catch short imperative + concrete action even after wrapper stripping
    r"^请立即生成",
    r"^请把",
    r"^请你",
    # "the user is asking..." / "the user wants..." - assistant narration
    r"the user (is asking|wants|wants? to|asked|wants me to|requested)",
    r"^the user ",
    # "如何..." / "How to..." / "怎么..." (how to)
    r"^(如何|怎么|怎样|怎么(样|做)|how (to|do|can|should))\b",
    # English imperatives the user types verbatim
    r"^(please\s+)?(help|write|create|make|build|find|fix|run|do|generate|check|analy[sz]e|review|explain|compare|summari[sz]e|optimi[sz]e|clean|deploy)\s+(me\s+|a\s+|an\s+|the\s+|this\s+|that\s+|my\s+|our\s+|some\s+|all\s+)?",
    # Verb-first project briefs
    r"^(thoroughly|completely|fully|deeply|carefully)\s+(explore|review|analy[sz]e|investigate|examine|rewrite|rebuild|redesign)\b",
    # "整理..." wrappers
    r"^整理[\"\u201c].*?(目录|文件|报告|教程|笔记)",
    r"^梳理.*?(市场|标的|行业|板块|投资|整个项目|项目|缺陷|代码|逻辑)",
    # More Chinese imperatives (verb-first) — match the verb alone so
    # "整理整个项目" / "分析下当前" / "运行测试" all flag.
    r"^(分析|调研|梳理|整理|检查|生成|写|制作|开发|修复|优化|跑通|跑|测试|运行|启动|部署|上传|下载|导出|导入|删除|添加|新增|更新|修改|重写|重构|封装|压缩|转换|翻译|读取|写入|抓取|爬取|搜索|查看|打开|关闭|重启|停止|暂停|继续|创建|构建|编译|操作|执行|移动|复制|粘贴|撤销|恢复|清理|清空|刷新|加载|渲染|展示)",
    # Verb + 下/一下/看看
    r"(帮我|请)?(看|处理|搞|弄|调整|改|写|跑|做|检查|分析|调研|梳理|整理|总结|测试|运行|启动|部署|搜索|查看)(一下|看|看看|下)",
    r"^(看|处理|搞|弄|调整|改|写|跑|做|检查|分析|调研|梳理|整理|总结|测试|运行|启动|部署|搜索|查看)(一下|看|看看|下)",
    # "我..." user-self imperative
    r"^我(想|需要|要|想请|想让|希望)",
    # "You are ..." assistant task instructions (colon or period)
    r"^you are (implementing|fixing|reviewing|writing|building|creating|checking|verifying|analy[sz]ing|investigating|designing|updating|modifying)\b",
    # Pure file-path / tool recipes
    r"^[/~][\w./_-]+/\w+\.(py|md|sh|json|yml|yaml|toml)\s*$",
    # "PDF was generated" / "I am now going to" type assistant narration
    r"^(pdf|the pdf|the file|the report|it) was (generated|created|saved|uploaded|sent)",
    r"^(i|now|next|then) (will|am going to|need to|should|want to)\b",
    # "let me ..." assistant narration
    r"^let me (check|look|run|try|verify|test|create|build|do|now)\b",
    r"^now let me ",
    # "I\'m going to ..." assistant narration
    r"^i.m going to ",
    # Verb-imperative-only (when text is 4-7 chars, no period, ends in noun)
    r"^[一-鿿]{2,4}[一-鿿a-z]",
    # "看下X" / "整理X" / etc. — verb + 下/一下/看/看看 with anything after
    r"^[看处理搞弄调整改写跑做检查分析调研梳理整理总结测试运行启动部署搜索查看](下|一下|看|看看)",
)


def _is_low_signal(text: str) -> bool:
    """Return True if a memory contains no actionable information worth a wiki
    bullet. Used to drop near-empty / pure-status memories before clustering.
    Operates on the cleaned form so "Outcome: tests are green" still matches."""
    if not text:
        return True
    s = _clean_noise(text, max_len=400).strip().lower()
    if len(s) < 12:
        return True
    for pat in _LOW_SIGNAL_PATTERNS:
        if re.match(pat, s):
            return True
    return False


def _looks_like_raw_prompt(text: str) -> bool:
    """Return True if a memory is *primarily* a raw user prompt — i.e. its
    atomic value is essentially the question / command itself, not a fact.

    The wiki must NOT show these: the user already remembers the question,
    and stuffing it into the wiki pollutes recall. We DO keep the memory in
    the store (so context can be replayed) but the rule-based wiki step
    drops the prompt form. A real LLM distillation would also drop these.
    """
    if not text:
        return True
    s = _clean_noise(text, max_len=400).strip().lower()
    # Threshold: very short texts (< 4 chars) are too ambiguous to
    # classify (a single Chinese verb is itself 2 chars, so 4 is the
    # smallest useful unit for an imperative).
    if len(s) < 4:
        return False
    for pat in _RAW_PROMPT_PATTERNS:
        if re.search(pat, s):
            return True
    # "Thoroughly explore the X project at I need to understand: 1. 2. 3. ..."
    # type run-on briefs. If the cleaned text still contains "i need to
    # understand" or "i want to" verbatim, it's a brief, not a fact.
    if re.search(r"i (need|want) to (understand|know|do|build|check|see|review|explore)", s):
        return True
    # Long imperative sentence (>140 cleaned chars) with no period in the
    # first 80 chars is almost always a user prompt, not an observation.
    head = s[:80]
    if len(s) > 140 and "." not in head and (" " in head[:20] or "\n" not in head):
        # Additional sanity: starts with a verb or 帮我 / 请 / how / please
        if re.match(r"^(please|how|why|what|when|where|can|could|would|should|do|does|is|are|was|were|i|you|we|let)", head):
            return True
    return False


def _title_from(text: str, fallback_kind: str = "", max_len: int = 60) -> str:
    """Make a real, noun-phrase title out of a raw memory string. Strips the
    wrapper, picks the first meaningful clause, and clamps length."""
    cleaned = _clean_noise(text, max_len=max_len * 2)
    if not cleaned:
        return (fallback_kind.title() if fallback_kind else "Cluster")[:max_len]
    # Prefer the part before the first sentence break
    head = re.split(r"[.!?。！？\n]", cleaned, maxsplit=1)[0].strip(" ,;:|/")
    if not head:
        head = cleaned
    if len(head) > max_len:
        head = head[: max_len - 1].rstrip(" ,;:|/") + "…"
    return head


# Stage 1 weight on signals. importance in [0,1] is the LLM/original value;
# we blend in recall_count and negative as dampeners.
W_RECALL = 0.10               # +0.10 per high-recall cluster, capped
W_NEGATIVE = 0.15             # -0.15 per negative feedback event, capped
RECALL_SATURATION = 5         # recall_count / 5 saturates the recall boost
NEGATIVE_SATURATION = 3       # 3 negative events = full dampener

# Stage 3 system prompt: per-cluster, keep/rewrite/drop actions.
_CLUSTER_SYSTEM = (
    "You are an assistant that tidies a small cluster of personal memory snippets. "
    "Treat every item as raw evidence and pull out the ATOMIC fact, never the wrapper.\n"
    "For EACH item return a JSON object with:\n"
    '  keep: boolean (true = the row contains real signal worth keeping long-term)\n'
    '  importance: number 0..1 (your new estimate of long-term importance for a '
    'user-profile knowledge base — be strict; routine tool chatter is <0.3)\n'
    '  distill: a SHORT rewritten version (<= 180 chars) capturing the core fact. '
    'STRIP ALL of the following before writing the distill: greeting/pleasantries, '
    'tool chatter ("Now let me check...", "Let me run..."), [thinking] blocks, '
    'code fences, raw user prompts repeated verbatim, "Outcome:" prefixes that just '
    "echo the user's question, file paths inside the user's home directory, "
    'console-style progress narration, and meta commentary about the assistant itself. '
    'Prefer a single declarative sentence. Empty string keeps the original.\n'
    '  tags: array of up to 5 lowercase tags (no duplicates, snake_case preferred).\n'
    'Set keep=false for: pure repetition of an earlier item, status pings with no '
    'fact ("Tests are green"), single-line acknowledgements, or text whose only '
    'information is a file path the user already knows.\n'
    'Reply with JSON: {"items": [...]}, no prose.'
)

# Stage 4 system prompt: build/update wiki pages from cluster summaries.
_WIKI_SYSTEM = (
    "You maintain a personal knowledge base for ONE user. You receive the user's "
    "existing wiki pages plus a batch of distilled cluster summaries (each already "
    "filtered for noise).\n"
    "GOAL: each page must be a CONCISE, ACTIONABLE atomic note the user would actually "
    "want to recall later — NOT a quote of the original conversation.\n"
    "ALWAYS bucket into one of these dimensions, and make the slug reflect the topic:\n"
    "  preferences-<topic>, decision-<topic>, project-<topic>, domain-<topic>, "
    "feedback-<topic>. Examples: 'prefers-dark-mode', 'decision-batch-size-50', "
    "'project-loop-memory', 'domain-crypto-swing-trades', 'feedback-no-mixed-lang'.\n"
    "Each page MUST have:\n"
    '  slug: lowercase, hyphen-separated, <= 60 chars, prefixed with the dimension\n'
    '  title: a real noun phrase, <= 60 chars (NEVER a truncated user prompt)\n'
    '  summary: 1-sentence definition, <= 200 chars, MUST stand on its own\n'
    '  body: bullet-point markdown, 3-8 short bullets, each starting with "- ". '
    'Each bullet states ONE atomic fact. No prose paragraphs. No "Outcome: ..." echoes. '
    'No code fences unless the fact is literally a command.\n'
    '  tags: 3-6 lowercase tags, snake_case\n'
    '  importance: 0..1 (1 = critical user preference/project, 0.3 = transient detail)\n'
    '  evidence_ids: list of memory ids that back this page (cite real ids from the input)\n'
    "SKIP a cluster summary if it is just a user prompt, status update, or repeats "
    "another cluster. PREFER updating an existing page (same slug) over creating a "
    "near-duplicate. Reply with JSON: {\"pages\": [...]}. If nothing adds new info, "
    "reply {\"pages\": []}. No prose, no markdown outside the JSON."
)


# ----------------------------------------------------------------------------
# Public dataclass
# ----------------------------------------------------------------------------


@dataclass
class EvolutionStats:
    scanned: int = 0
    rescored: int = 0
    dropped: int = 0
    deduped: int = 0           # near-duplicate memories collapsed
    resummarized: int = 0
    clusters: int = 0
    cluster_calls: int = 0
    wiki_calls: int = 0
    wiki_created: int = 0
    wiki_updated: int = 0
    wiki_retired: int = 0      # noisy legacy wiki pages removed
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "rescored": self.rescored,
            "dropped": self.dropped,
            "deduped": self.deduped,
            "resummarized": self.resummarized,
            "clusters": self.clusters,
            "cluster_calls": self.cluster_calls,
            "wiki_calls": self.wiki_calls,
            "wiki_created": self.wiki_created,
            "wiki_updated": self.wiki_updated,
            "wiki_retired": self.wiki_retired,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "notes": self.notes,
            "stages": self.stages,
        }


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _extract_json(text: str) -> Any | None:
    """Pull the first balanced JSON object out of an LLM reply."""
    import re
    if not text:
        return None
    # direct
    try:
        return json.loads(text)
    except Exception:
        pass
    # fenced ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # first {...} or first [...]
    for opener, closer in [("{", "}"), ("[", "]")]:
        i = text.find(opener)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i : j + 1])
                    except Exception:
                        break
    return None


def _hash_embed(text: str, dim: int = 128) -> list[float]:
    """Deterministic 128-dim embedding (feature hashing). Cheap fallback so
    semantic batching works even without sentence-transformers installed."""
    v = [0.0] * dim
    tokens = (text or "").lower().split()
    if not tokens:
        return v
    for tok in tokens:
        h = hashlib.md5(tok.encode("utf-8")).digest()
        idx = h[0] % dim
        sign = 1.0 if (h[1] & 1) else -1.0
        v[idx] += sign
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


# ----------------------------------------------------------------------------
# Consolidator
# ----------------------------------------------------------------------------


class EvolutionConsolidator:
    """5-stage distillation pipeline. Drop-in replacement for
    ``LLMConsolidator.run``."""

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMClient,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = dict(config or {})
        self._cache: dict[str, str] = {}
        self._cache_ttl = 300.0
        self._cache_ts: dict[str, float] = {}
        self._run_id: str | None = None

    # --- public ----------------------------------------------------------

    def set_run_id(self, run_id: str | None) -> None:
        self._run_id = run_id

    def run(
        self,
        memories: list[StoredMemory] | None = None,
        progress: Callable[[int, int], None] | None = None,
        limit: int = 300,
    ) -> EvolutionStats:
        t0 = time.time()
        stats = EvolutionStats()
        cfg = self.config
        dry_run = bool(cfg.get("dry_run", False))

        if memories is None:
            memories = self.store.list_memories(limit=limit)
        memories = list(memories)
        stats.scanned = len(memories)
        if not memories:
            stats.notes.append("no memories")
            stats.elapsed_ms = (time.time() - t0) * 1000
            return stats

        if progress:
            try:
                progress(0, len(memories))
            except Exception:
                pass

        # Stage 0: Pre-cluster cleanup. Drop pure-status noise and collapse
        # near-duplicate memories so the cluster LLM sees focused input.
        s0_t = time.time()
        pre_count = len(memories)
        memories = self._stage0_filter_noise(memories)
        memories = self._stage0_dedup_memories(memories, stats)
        s0 = {
            "in": pre_count,
            "out": len(memories),
            "ms": round((time.time() - s0_t) * 1000, 1),
            "note": f"noise-filtered {pre_count - stats.deduped - len(memories) + pre_count} · deduped {stats.deduped}",
            "evidence_ids": [m.id for m in memories[:200]],
        }
        stats.stages["clean"] = s0
        self._record_stage("clean", pre_count, len(memories), s0["note"], s0)
        if not memories:
            stats.notes.append("no memories after cleanup")
            stats.elapsed_ms = (time.time() - t0) * 1000
            return stats

        # Stage 1: Signal-aware rescoring
        s1_t = time.time()
        stage1_in = len(memories)
        rescored_map = self._stage1_signal_scoring(memories)
        stats.rescored = sum(1 for v in rescored_map.values() if v)
        # After stage 1 we run rescore_all to update the score column.
        # Capture how many actually changed.
        rescore_changed = 0
        try:
            rescore_changed = self.store.rescore_all(half_life_days=30.0)
        except Exception:
            pass
        s1 = {
            "in": stage1_in,
            "out": rescore_changed or stage1_in,
            "ms": round((time.time() - s1_t) * 1000, 1),
            "note": f"rescored {rescore_changed}/{stage1_in} memories",
            "evidence_ids": [m.id for m in memories[:200]],
        }
        stats.stages["score"] = s1
        self._record_stage("score", stage1_in, s1["out"], s1["note"], s1)
        if progress:
            try:
                progress(int(len(memories) * 0.2), len(memories))
            except Exception:
                pass

        # Stage 2: Semantic batching
        s2_t = time.time()
        clusters = self._stage2_cluster(memories, max_per_cluster=CLUSTER_MAX)
        stats.clusters = len(clusters)
        s2 = {
            "in": stage1_in,
            "out": len(clusters),
            "ms": round((time.time() - s2_t) * 1000, 1),
            "note": f"formed {len(clusters)} clusters",
        }
        stats.stages["cluster"] = s2
        self._record_stage("cluster", stage1_in, len(clusters), s2["note"], s2)
        if progress:
            try:
                progress(int(len(memories) * 0.4), len(memories))
            except Exception:
                pass

        # Stage 3: Per-cluster distillation
        s3_t = time.time()
        cluster_summaries: list[dict[str, Any]] = []
        kept_ids: set = set()
        dropped_ids: set = set()
        is_rule = self._echo_provider()
        for ci, cluster in enumerate(clusters):
            if is_rule:
                # No LLM -> keep everything as-is. Build a summary from
                # the top important items so Stage 4 still has signal.
                # We attach the cleaned, top-N items directly so Stage 4
                # can build real bullets (instead of one stitched blob).
                ranked = sorted(
                    cluster,
                    key=lambda m: -(float(getattr(m, "importance", 0.0) or 0.0)),
                )
                top = ranked[:7]
                summary_text = " / ".join((m.text or "")[:120] for m in top[:3])[:400]
                kinds = [m.kind for m in cluster if m.kind]
                kind = max(set(kinds), key=kinds.count) if kinds else ""
                all_tags = [t for m in cluster for t in (m.tags or []) if t]
                dom_tag = max(set(all_tags), key=all_tags.count) if all_tags else ""
                avg_imp = sum((m.importance or 0) for m in cluster) / max(1, len(cluster))
                summary = {
                    "text": summary_text,
                    "size": len(cluster),
                    "kept": len(cluster),
                    "dropped": 0,
                    "evidence_ids": [m.id for m in cluster][:50],
                    "kind": kind,
                    "dominating_tag": dom_tag,
                    "avg_importance": round(avg_imp, 3),
                    # Top-ranked items (cleaned) so Stage 4 can build
                    # real bullets, not raw concatenation.
                    "items": top,
                }
                actions = {m.id: {"keep": True, "importance": m.importance, "distill": "", "tags": list(m.tags or [])} for m in cluster}
            else:
                summary, actions = self._stage3_distill_cluster(cluster, cfg, stats)
            cluster_summaries.append(summary)
            if not dry_run:
                kept = self._apply_actions(cluster, actions, stats)
                kept_ids |= kept
                for m in cluster:
                    if m.id not in kept:
                        dropped_ids.add(m.id)
            if progress:
                try:
                    progress(int(len(memories) * (0.4 + 0.4 * (ci + 1) / max(1, len(clusters)))), len(memories))
                except Exception:
                    pass
        s3 = {
            "in": len(clusters),
            "out": len([s for s in cluster_summaries if s.get("text")]),
            "ms": round((time.time() - s3_t) * 1000, 1),
            "note": f"{stats.cluster_calls} LLM calls · {len(clusters)} clusters · {len(kept_ids)} kept / {len(dropped_ids)} dropped",
            "evidence_ids": list(kept_ids)[:200],
            "kept_ids": list(kept_ids)[:200],
            "dropped_ids": list(dropped_ids)[:200],
        }
        stats.stages["distill"] = s3
        self._record_stage("distill", len(clusters), s3["out"], s3["note"], s3)

        # Stage 4: Hierarchical wiki synthesis
        s4_t = time.time()
        wiki_pages = self._stage4_wiki_synthesis(cluster_summaries, cfg, stats)
        if not dry_run:
            stats.wiki_created = wiki_pages.get("created", 0)
            stats.wiki_updated = wiki_pages.get("updated", 0)
        # Collect evidence ids from the wiki pages so drill-down can list them
        wiki_evidence: list = []
        try:
            for pg in self.store.list_wiki_pages(limit=50):
                eids = pg.get("evidence_ids") or []
                if isinstance(eids, list):
                    wiki_evidence.extend([str(x) for x in eids])
        except Exception:
            pass
        s4 = {
            "in": len(cluster_summaries),
            "out": stats.wiki_created + stats.wiki_updated,
            "ms": round((time.time() - s4_t) * 1000, 1),
            "note": f"created={stats.wiki_created} updated={stats.wiki_updated}",
            "evidence_ids": wiki_evidence[:200],
        }
        stats.stages["wiki"] = s4
        self._record_stage("wiki", len(cluster_summaries), s4["out"], s4["note"], s4)

        # Stage 4.5: Retire noisy wiki pages. Conservative: only touches
        # pages whose title is a raw user prompt, body is glued fragments,
        # or body has no bullets. Well-formed LLM-authored pages never
        # match these heuristics.
        if not dry_run:
            try:
                retired = self._stage4_cleanup_wiki(stats)
                stats.wiki_retired = retired
            except Exception:
                pass
        if progress:
            try:
                progress(len(memories), len(memories))
            except Exception:
                pass

        # Stage 5: Evolution memo
        s5_t = time.time()
        self._stage5_memo(stats)
        s5 = {
            "in": stats.wiki_created + stats.wiki_updated,
            "out": 1,
            "ms": round((time.time() - s5_t) * 1000, 1),
            "note": "evolution memo updated",
        }
        stats.stages["memo"] = s5
        self._record_stage("memo", s5["in"], 1, s5["note"], s5)

        # Rescore from new importance
        if not dry_run and stats.rescored:
            try:
                self.store.rescore_all(half_life_days=30.0)
            except Exception:
                pass

        stats.elapsed_ms = (time.time() - t0) * 1000
        return stats

    # --- Stage 1: Signal-aware scoring ----------------------------------

    def _stage1_signal_scoring(
        self, memories: list[StoredMemory]
    ) -> dict[str, bool]:
        """Blend original importance with behavioural signals. We do not
        write back here — the per-cluster LLM pass is what writes the new
        importance. This stage just gives the LLM richer ranking input."""
        rescored: dict[str, bool] = {}
        for m in memories:
            sig = self.store.get_signal(m.id)
            boost = min(W_RECALL, W_RECALL * sig["recall_count"] / RECALL_SATURATION)
            damp = min(W_NEGATIVE, W_NEGATIVE * sig["negative"] / NEGATIVE_SATURATION)
            adj = (m.importance or 0.0) + boost - damp
            adj = max(0.0, min(1.0, adj))
            if abs(adj - (m.importance or 0.0)) > 0.05:
                rescored[m.id] = True
        return rescored

    # --- Stage 2: Semantic batching -------------------------------------

    def _stage2_cluster(
        self,
        memories: list[StoredMemory],
        max_per_cluster: int = CLUSTER_MAX,
    ) -> list[list[StoredMemory]]:
        """Greedy cosine clustering using a hashed embedding. Memories that
        lack enough text to embed fall into a 'misc' cluster of their own
        so we never lose them."""
        if not memories:
            return []

        # Sort by adjusted importance desc so high-signal memories seed clusters
        def _score(m: StoredMemory) -> float:
            sig = self.store.get_signal(m.id)
            boost = min(0.1, 0.1 * sig["recall_count"] / RECALL_SATURATION)
            damp = min(0.15, 0.15 * sig["negative"] / NEGATIVE_SATURATION)
            return (m.importance or 0.0) + boost - damp

        ranked = sorted(memories, key=_score, reverse=True)

        clusters: list[dict[str, Any]] = []  # {centroid, items}
        for m in ranked:
            text = (m.text or "").strip()
            if len(text) < 8:
                # super short items go to a misc cluster at the end
                clusters.append({"centroid": None, "items": [m], "misc": True})
                continue
            emb = _hash_embed(text)
            placed = False
            for cl in clusters:
                if cl.get("misc") or len(cl["items"]) >= max_per_cluster:
                    continue
                sim = _cos(emb, cl["centroid"])
                if sim >= 0.35:  # hashed embeddings are noisier, lower threshold
                    cl["items"].append(m)
                    # update centroid (running mean)
                    n = len(cl["items"])
                    cl["centroid"] = [
                        (cl["centroid"][i] * (n - 1) + emb[i]) / n for i in range(len(emb))
                    ]
                    placed = True
                    break
            if not placed:
                clusters.append({"centroid": emb, "items": [m], "misc": False})

        # Second pass: SESSION-AWARE MERGE. The user explicitly asked
        # that a single conversation not be fragmented into too many
        # knowledge pieces. After the cosine pass, we look for small
        # clusters (<= 8 items) that share session_ids with other
        # clusters and merge them so a single conversation produces
        # one wiki page, not many. We cap the merge to avoid giant
        # mixed clusters; only merge into the cluster with the highest
        # cumulative importance.
        def _session_counts(items):
            counts: dict[str, int] = {}
            for it in items:
                sid = getattr(it, "session_id", None)
                if sid:
                    counts[sid] = counts.get(sid, 0) + 1
            return counts

        # Build a list of (cluster, session_counts) for non-misc clusters
        non_misc = [c for c in clusters if not c.get("misc")]
        for c in non_misc:
            c["_sc"] = _session_counts(c["items"])
        # Repeat: find the smallest cluster, see if any other cluster
        # shares a session, and merge into the one with higher total
        # importance. Bound iterations to keep this O(N) not O(N²).
        for _ in range(20):
            merged = False
            non_misc.sort(key=lambda c: (len(c["items"]), -sum(getattr(it, "importance", 0) or 0 for it in c["items"])))
            for i, small in enumerate(non_misc):
                if len(small["items"]) >= 9:
                    continue  # only merge small clusters
                if not small["_sc"]:
                    continue
                best_j = -1
                best_overlap = 0
                for j, big in enumerate(non_misc):
                    if i == j:
                        continue
                    if len(big["items"]) >= max_per_cluster:
                        continue
                    overlap = sum(
                        min(small["_sc"].get(s, 0), big["_sc"].get(s, 0))
                        for s in small["_sc"]
                    )
                    if overlap >= 2 and overlap > best_overlap:
                        best_overlap = overlap
                        best_j = j
                if best_j >= 0:
                    big = non_misc[best_j]
                    big["items"].extend(small["items"])
                    # Recompute centroid (running mean)
                    n = len(big["items"])
                    if big["centroid"] is not None:
                        # Re-derive centroid from all members (cheap)
                        embs = [_hash_embed(getattr(m, "text", "") or "") for m in big["items"]]
                        dim = len(embs[0]) if embs else 128
                        cent = [0.0] * dim
                        for e in embs:
                            for k in range(dim):
                                cent[k] += e[k]
                        big["centroid"] = [x / n for x in cent]
                    big["_sc"] = _session_counts(big["items"])
                    non_misc.remove(small)
                    merged = True
                    break
            if not merged:
                break

        # Final sort: largest first, misc last
        clusters = non_misc + [c for c in clusters if c.get("misc")]
        clusters.sort(key=lambda c: (c.get("misc", False), -len(c["items"])))
        return [c["items"] for c in clusters]

    # --- Stage 3: Per-cluster distillation ------------------------------

    def _stage3_distill_cluster(
        self,
        cluster: list[StoredMemory],
        cfg: dict[str, Any],
        stats: EvolutionStats,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Return (cluster_summary, per-item actions)."""
        # Build user payload
        payload = []
        for m in cluster:
            sig = self.store.get_signal(m.id)
            payload.append({
                "id": m.id,
                "kind": m.kind,
                "tags": list(m.tags or []),
                "importance": round(m.importance or 0.0, 3),
                "recall_count": sig["recall_count"],
                "negative": sig["negative"],
                "text": (m.text or "")[:600],
            })
        user_prompt = json.dumps({"items": payload}, ensure_ascii=False)
        cache_key = hashlib.sha1(
            (user_prompt + "||" + (getattr(self.provider, "model", "?") or "?")
             + "||cluster||" + str(float(cfg.get("temperature") or 0.2))).encode()
        ).hexdigest()
        reply = self._cached_call(cache_key, _CLUSTER_SYSTEM, user_prompt, cfg, stats, kind="cluster")

        actions: dict[str, dict[str, Any]] = {}
        parsed = _extract_json(reply or "")
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            for it in parsed["items"]:
                if isinstance(it, dict) and "id" in it:
                    actions[str(it["id"])] = it

        # Build a 1-sentence cluster summary from the LLM reply (or fall
        # back to a stitched top-3 important items).
        cluster_text = ""
        if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
            cluster_text = parsed["summary"].strip()[:400]
        if not cluster_text:
            top = sorted(cluster, key=lambda m: -(m.importance or 0))[:3]
            cluster_text = " / ".join((m.text or "")[:120] for m in top)[:400]

        # Pull memory ids in this cluster so downstream wiki synthesis
        # can cite them as evidence. Use the kept ones when the LLM
        # classified them, otherwise all cluster items.
        kept_set = {m_id for m_id, a in actions.items() if a.get("keep") is True}
        evidence_ids = [m.id for m in cluster if (not kept_set or m.id in kept_set)]
        # Most-common kind and tag — used by the rule-based wiki step to
        # produce a meaningful title when no LLM is involved.
        kinds = [m.kind for m in cluster if m.kind]
        kind = max(set(kinds), key=kinds.count) if kinds else ""
        all_tags = [t for m in cluster for t in (m.tags or []) if t]
        dom_tag = max(set(all_tags), key=all_tags.count) if all_tags else ""
        avg_imp = sum((m.importance or 0) for m in cluster) / max(1, len(cluster))

        summary = {
            "text": cluster_text,
            "size": len(cluster),
            "kept": len(evidence_ids) if evidence_ids else sum(1 for a in actions.values() if a.get("keep") is True),
            "dropped": len(cluster) - (len(evidence_ids) if evidence_ids else sum(1 for a in actions.values() if a.get("keep") is True)),
            "evidence_ids": evidence_ids[:50],
            "kind": kind,
            "dominating_tag": dom_tag,
            "avg_importance": round(avg_imp, 3),
        }
        return summary, actions

    def _apply_actions(
        self,
        cluster: list[StoredMemory],
        actions: dict[str, dict[str, Any]],
        stats: EvolutionStats,
    ) -> set:
        """Apply keep / drop / rewrite actions to the DB. Returns the set of
        kept memory ids."""
        kept: set = set()
        for m in cluster:
            act = actions.get(m.id)
            if not act:
                # no LLM action => keep as-is
                kept.add(m.id)
                continue
            if act.get("keep") is False:
                try:
                    self.store.delete_memory(m.id)
                    stats.dropped += 1
                except Exception:
                    kept.add(m.id)
                continue
            new_text = (act.get("distill") or "").strip()
            new_importance = act.get("importance")
            new_tags = act.get("tags")
            updates: dict[str, Any] = {}
            try:
                if isinstance(new_importance, (int, float)):
                    ni = max(0.0, min(1.0, float(new_importance)))
                    if abs(ni - (m.importance or 0.0)) > 1e-3:
                        updates["importance"] = ni
                        stats.rescored += 1
            except Exception:
                pass
            if new_text and new_text != (m.text or "") and len(new_text) <= 400:
                updates["text"] = new_text
                stats.resummarized += 1
            if isinstance(new_tags, list):
                tags_clean = [str(t).strip().lower() for t in new_tags if t and str(t).strip()][:6]
                if tags_clean:
                    updates["tags"] = tags_clean
            if updates:
                try:
                    self.store.upsert_memory(
                        id=m.id,
                        kind=m.kind,
                        text=updates.get("text", m.text),
                        importance=updates.get("importance", m.importance),
                        source=m.source,
                        session_id=m.session_id,
                        created_at=m.created_at,
                        updated_at=time.time(),
                        ttl=m.ttl,
                        tags=updates.get("tags", m.tags),
                        embedding=m.embedding,
                    )
                except Exception:
                    pass
            kept.add(m.id)
        return kept

    # --- Stage 3.5: Wiki cleanup ---------------------------------------

    def _stage4_cleanup_wiki(self, stats: EvolutionStats) -> int:
        """Retire noisy legacy wiki pages produced by older rule-based runs.

        A page is considered noisy when:
          * its body contains the "/ " separator glue (old fallback signature)
          * its title is literally a truncated user prompt (>40 chars starting
            with [codex] / [claude] / User intent / Outcome: / 帮我 / 分析)
          * its importance is < 0.35 AND it has no evidence ids
          * it has zero bullets in its body (no real content)

        Returns the number of pages retired. We never retire pages with
        importance >= 0.5 even if they look messy — the user might still
        rely on them.
        """
        try:
            pages = self.store.list_wiki_pages(limit=200)
        except Exception:
            return 0
        retired = 0
        for p in pages:
            pid = p.get("id")
            if not pid:
                continue
            title = (p.get("title") or "").strip()
            body = (p.get("body") or "")
            evidence = p.get("evidence_ids") or []
            has_bullets = body.count("\n- ") + (1 if body.startswith("- ") else 0)
            # A title that is literally a session-source prefix followed by a
            # raw user prompt is ALWAYS a "list of recent user prompts" page
            # — there is no atomic knowledge here. Bail the early-skip.
            title_prefix_prompt = title.startswith((
                "[codex]", "[claude]", "[hermes]", "[openclaw]",
                "User intent:", "Outcome:", "You are ", "Review ",
                "帮我", "分析", "请立即", "如何", "How to", "Please ",
                "请", "你可以干嘛", "User said:",
            )) and len(title) > 20
            if title_prefix_prompt:
                noisy = True
            else:
                # Glue signature: lots of "/ " separators (old fallback glued raw text)
                if body.count(" / ") >= 2:
                    noisy = True
                # Title is a truncated user prompt
                if title.startswith(("[codex]", "[claude]", "[hermes]", "[openclaw]",
                                      "User intent:", "Outcome:", "帮我", "分析",
                                      "How to", "Please", "请", "你可以干嘛",
                                      "User said:")) and len(title) > 20:
                    noisy = True
                # No bullets and no evidence
                if has_bullets == 0 and not evidence:
                    noisy = True
                # Body has 0 newlines AND no bullets -> just a single glued blob
                if body.count("\n") < 2 and has_bullets == 0:
                    noisy = True
                # Bail only if the page has real bullets AND evidence AND a clean title
                if has_bullets >= 2 and len(evidence) >= 2:
                    continue
            if noisy:
                try:
                    self.store.delete_wiki_page(pid)
                    retired += 1
                    stats.notes.append(f"wiki retired: {title[:40]!r}")
                except Exception:
                    pass
        # Second sweep: REWRITE every existing page's bullets through
        # the new cleaner + raw-prompt filter. The previous run may have
        # produced bullets that look like raw user prompts (especially
        # legacy pages from before _looks_like_raw_prompt existed). We
        # rebuild the body from the surviving bullets, re-extract the
        # title, and re-summarize. This is the cheapest way to bring
        # legacy pages up to the new quality bar.
        for p2 in self.store.list_wiki_pages(limit=200):
            slug = (p2.get("slug") or "")
            if not slug:
                continue
            body = p2.get("body") or ""
            # Skip the auto-aggregator pages — handled below.
            if slug in ("auto-episode", "auto-fact"):
                continue
            # Collect surviving bullets after raw-prompt / low-signal filter
            bullet_lines = [b for b in body.split("\n") if b.startswith("- ")]
            surviving: list[str] = []
            seen: set[str] = set()
            for b in bullet_lines:
                txt = b[2:].strip()
                if _looks_like_raw_prompt(txt) or _is_low_signal(txt):
                    continue
                # Re-run the cleaner for the new max_len (slightly longer)
                clean = _clean_noise(txt, max_len=200)
                if not clean or len(clean) < 6:
                    continue
                key = clean[:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                surviving.append(f"- {clean}")
                if len(surviving) >= 8:
                    break
            if not surviving:
                # All bullets filtered out -> retire the page.
                try:
                    self.store.delete_wiki_page(p2["id"])
                    retired += 1
                    stats.notes.append(f"wiki retired (empty after re-clean): {slug}")
                except Exception:
                    pass
                continue
            # If we have new surviving bullets, rewrite the body.
            if len(surviving) != len(bullet_lines):
                new_body = "\n".join(surviving)
                evidence = p2.get("evidence_ids") or []
                if evidence:
                    new_body += f"\n\n_Source: {len(evidence)} memories (rewritten by Stage 4.6)_"
                # Re-extract title + summary from the new top bullet
                top = surviving[0].lstrip("- ")
                new_title = _title_from(top, fallback_kind="", max_len=58)
                new_summary = top[:200].rstrip(" .,;:")
                try:
                    self.store.upsert_wiki_page(
                        slug=slug,
                        title=new_title,
                        body=new_body,
                        summary=new_summary,
                        tags=p2.get("tags", []),
                        importance=p2.get("importance", 0.5),
                        evidence_ids=evidence,
                        run_id=self._run_id,
                    )
                    stats.notes.append(f"wiki re-cleaned: {slug} ({len(bullet_lines)}→{len(surviving)} bullets)")
                except Exception:
                    pass
        # Third sweep: rewrite auto-aggregator pages (auto-episode /
        # auto-fact) using fresh bullet bodies if their current body still
        # contains glued fragments. We never delete these — they back many
        # cross-session facts — but we DO clean their body.
        for p2 in self.store.list_wiki_pages(limit=20):
            slug = (p2.get("slug") or "")
            if slug not in ("auto-episode", "auto-fact"):
                continue
            body = p2.get("body") or ""
            # If the body has many "/ " separators or many duplicate
            # bullets, it's a candidate for a re-synthesis pass.
            glue = body.count(" / ")
            # Cheap dedupe: count repeated leading tokens.
            bullets = [b for b in body.split("\n") if b.startswith("- ")]
            seen = set()
            unique = []
            for b in bullets:
                key = b[:80].lower()
                if key in seen:
                    continue
                seen.add(key)
                unique.append(b)
            if glue >= 3 or len(unique) < len(bullets) * 0.7:
                # Replace body with the deduped + cleaned bullets.
                if unique:
                    new_body = "\n".join(unique[:25])
                else:
                    continue
                try:
                    self.store.upsert_wiki_page(
                        slug=slug,
                        title=p2.get("title", ""),
                        body=new_body,
                        summary=p2.get("summary", ""),
                        tags=p2.get("tags", []),
                        importance=p2.get("importance", 0.5),
                        evidence_ids=p2.get("evidence_ids", []),
                        run_id=self._run_id,
                    )
                    stats.notes.append(f"wiki re-synthesized: {slug}")
                except Exception:
                    pass
        return retired

    # --- Stage 4: Wiki synthesis ----------------------------------------

    def _stage4_wiki_synthesis(
        self,
        cluster_summaries: list[dict[str, Any]],
        cfg: dict[str, Any],
        stats: EvolutionStats,
    ) -> dict[str, int]:
        if not cluster_summaries:
            return {"created": 0, "updated": 0, "calls": 0}

        # Cap input
        clusters = cluster_summaries[:WIKI_INPUT_CLUSTERS]

        # Rule-based fast path: skip the wiki LLM call (it would just echo
        # back non-JSON) and synthesize wiki pages deterministically by
        # clustering by kind. Real wiki text comes from cluster summaries.
        if self._echo_provider():
            return self._stage4_rules(clusters, stats)

        existing = self.store.list_wiki_pages(limit=50)
        existing_payload = [
            {"slug": p.get("slug", ""), "title": p.get("title", ""),
             "summary": (p.get("summary") or "")[:200],
             "tags": list(p.get("tags") or []),
             "importance": round(p.get("importance") or 0, 2)}
            for p in existing
        ]
        # Evolution memo: last 3 runs
        memo = self.store.get_setting("evolution_memo", "") or ""

        user_payload = {
            "profile_dimensions": list(PROFILE_DIMS),
            "evolution_memo": memo[:1500] if isinstance(memo, str) else "",
            "existing_wiki": existing_payload,
            "cluster_summaries": [
                {"text": cs["text"], "size": cs["size"]}
                for cs in clusters
            ],
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False)
        cache_key = hashlib.sha1(
            (user_prompt + "||" + (getattr(self.provider, "model", "?") or "?")
             + "||wiki-evo||" + str(float(cfg.get("temperature") or 0.3))).encode()
        ).hexdigest()
        reply = self._cached_call(cache_key, _WIKI_SYSTEM, user_prompt, cfg, stats, kind="wiki")

        parsed = _extract_json(reply or "")
        if not isinstance(parsed, dict):
            # LLM unreachable or returned junk — fall back to rule-based
            # synthesis so the wiki still grows even without an LLM.
            stats.notes.append("wiki LLM reply was not valid JSON — falling back to rule-based synthesis")
            return self._stage4_rules(clusters, stats)
        pages = parsed.get("pages") or []
        if not isinstance(pages, list) or len(pages) == 0:
            # LLM produced no pages — fall back too so the user sees
            # real wiki content immediately.
            stats.notes.append("wiki LLM returned 0 pages — falling back to rule-based synthesis")
            return self._stage4_rules(clusters, stats)

        created = 0
        updated = 0
        for p in pages:
            if not isinstance(p, dict):
                continue
            slug = (p.get("slug") or "").strip().lower().replace(" ", "-")[:80]
            title = (p.get("title") or "").strip()
            body = (p.get("body") or "").strip()
            if not slug or not title or not body:
                continue
            tags = p.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip().lower() for t in tags if t and str(t).strip()][:8]
            try:
                importance = max(0.0, min(1.0, float(p.get("importance") or 0.5)))
            except Exception:
                importance = 0.5
            evidence = p.get("evidence_ids") or []
            if not isinstance(evidence, list):
                evidence = []
            evidence = [str(x) for x in evidence if x][:50]
            summary = (p.get("summary") or "").strip()[:400]
            existing_p = self.store.get_wiki_page_by_slug(slug)
            try:
                self.store.upsert_wiki_page(
                    slug=slug, title=title, body=body, summary=summary,
                    tags=tags, importance=importance, evidence_ids=evidence,
                    run_id=self._run_id,
                )
            except Exception as e:
                stats.notes.append(f"wiki upsert err: {e}")
                continue
            if existing_p is None:
                created += 1
            else:
                updated += 1
        return {"created": created, "updated": updated, "calls": 1}


    def _echo_provider(self) -> bool:
        return type(self.provider).__name__ == "RuleBasedProvider"

    def _stage4_rules(
        self,
        clusters: list[dict[str, Any]],
        stats: EvolutionStats,
    ) -> dict[str, int]:
        """Rule-based wiki synthesis: one page per cluster.

        Produces real, browsable wiki pages even when no LLM is
        configured (or when the configured LLM is failing). Each page
        has a real noun-phrase title, a 1-line summary, and a body of
        dynamic-length atomic bullets — never a glued-up concatenation of
        raw memory text. This is the fallback that drives the entire wiki
        when the user has not yet configured an API key.

        Bullet count is **dynamic** (2..8) based on cluster size and
        quality. We aggressively filter out raw user prompts and pure
        status pings so the wiki stays dense and useful.
        """
        import hashlib as _h
        created = 0
        updated = 0
        for i, cs in enumerate(clusters):
            memories: list = cs.get("items") or cs.get("memories") or []
            if not memories:
                continue
            ranked = sorted(
                memories,
                key=lambda m: -(float(getattr(m, "importance", 0.0) or 0.0)),
            )
            # Dynamic max bullets: small cluster -> few bullets, big -> more.
            n = len(ranked)
            if n <= 2:
                max_bullets = 2
            elif n <= 5:
                max_bullets = 3
            elif n <= 10:
                max_bullets = 5
            else:
                max_bullets = 7
            # Hard ceiling so a noisy session never produces a wall of bullets.
            max_bullets = min(max_bullets, 8)
            bullets: list[str] = []
            seen: set[str] = set()
            skipped_prompts = 0
            for m in ranked:
                txt = getattr(m, "text", "") or ""
                # Drop raw user prompts outright: they add zero atomic value.
                if _looks_like_raw_prompt(txt):
                    skipped_prompts += 1
                    continue
                bullet = _clean_noise(txt, max_len=160)
                if not bullet:
                    continue
                # Even after cleaning, a memory can still be pure status —
                # be conservative and skip if it's just a low-signal ping.
                if _is_low_signal(bullet):
                    continue
                # Bullet-length sanity: drop bullets that are still
                # "raw prompt in disguise" (long, imperative, no period).
                if len(bullet) > 140 and "." not in bullet[:80]:
                    continue
                key = bullet[:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                if len(bullet) < 6:
                    continue
                bullets.append(f"- {bullet}")
                if len(bullets) >= max_bullets:
                    break
            if not bullets:
                continue
            kind_hint = (cs.get("kind") or cs.get("dominating_tag") or "").lower().strip()
            # Use the FIRST surviving bullet for the topic + title — never
            # the cluster summary text, which can be a raw user prompt.
            topic_src = bullets[0].lstrip("- ")
            # Strip any lingering prompt words from the title.
            topic_src = re.sub(
                r"^(帮我|请立即|请|如何|怎么|thoroughly explore\s+the\s+|please\s+)\S*",
                "",
                topic_src,
                flags=re.I,
            ).strip(" ,;:|/。")
            if not topic_src:
                topic_src = bullets[0].lstrip("- ")
            words = re.findall(r"[a-z0-9一-鿿]+", topic_src.lower())
            topic = "-".join([w for w in words if len(w) > 1][:4]) or f"cluster-{i+1}"
            slug_src = f"{kind_hint}-{topic}" if kind_hint else topic
            slug = re.sub(r"[^a-z0-9一-鿿-]+", "-", slug_src).strip("-").lower()[:60]
            if not slug:
                slug = "auto-cluster-" + _h.md5(slug_src.encode("utf-8")).hexdigest()[:10]
            title = _title_from(topic_src, fallback_kind=kind_hint, max_len=58)
            summary = bullets[0].lstrip("- ")[:200].rstrip(" .,;:")
            if not summary:
                continue
            evidence_ids: list[str] = []
            for m in ranked:
                mid = getattr(m, "id", None)
                if mid and str(mid) not in evidence_ids:
                    evidence_ids.append(str(mid))
                if len(evidence_ids) >= 12:
                    break
            body_lines = list(bullets)
            if evidence_ids:
                body_lines.append(
                    f"\n_Source: {len(evidence_ids)} memories · importance-weighted top-{len(ranked)}_"
                )
            body = "\n".join(body_lines)
            tags = ["auto", "rule-based"]
            if kind_hint:
                tags.append(kind_hint)
            top_imp = [float(getattr(m, "importance", 0.0) or 0.0) for m in ranked[:5]]
            importance = max(0.35, sum(top_imp) / max(1, len(top_imp)))
            importance = round(min(1.0, importance), 2)
            existing_p = self.store.get_wiki_page_by_slug(slug)
            try:
                self.store.upsert_wiki_page(
                    slug=slug, title=title, body=body, summary=summary,
                    tags=tags[:6], importance=importance,
                    evidence_ids=evidence_ids, run_id=self._run_id,
                )
            except Exception:
                continue
            if existing_p is None:
                created += 1
            else:
                updated += 1
            if skipped_prompts:
                stats.notes.append(
                    f"rule-wiki {slug[:30]}: skipped {skipped_prompts} raw prompt(s)"
                )
        stats.wiki_calls += 1
        return {"created": created, "updated": updated, "calls": 1}


    # --- Stage 0: Pre-cluster cleanup -----------------------------------

    def _stage0_filter_noise(
        self, memories: list[StoredMemory]
    ) -> list[StoredMemory]:
        """Drop memories that carry no actionable information: short
        status pings, single-line acknowledgements, raw user prompts, etc.
        These dilute the cluster LLM and inflate the wiki page count
        without value. We keep raw user prompts in the *store* (for
        session replay and contradiction detection) but filter them from
        the distillation path."""
        kept: list[StoredMemory] = []
        for m in memories:
            text = (m.text or "").strip()
            if _is_low_signal(text):
                continue
            if len(text) < 16:
                continue
            # Hard filter: raw user prompts should not pollute the wiki.
            # We classify them as noise for the distillation pipeline.
            if _looks_like_raw_prompt(text):
                continue
            kept.append(m)
        return kept

    def _stage0_dedup_memories(
        self,
        memories: list[StoredMemory],
        stats: EvolutionStats,
    ) -> list[StoredMemory]:
        """Collapse near-duplicate memories into one. We hash-embed each
        memory (cheap, deterministic), then group by cosine similarity
        >= 0.85 within the same kind. Comparison is against EVERY member
        of the existing group (not just the centroid), so partial matches
        chain into the right cluster. The highest-importance memory
        survives; the rest are deleted from the store and the survivor's
        importance is bumped slightly so the merged fact rises above
        the noise."""
        if not memories:
            return memories
        ranked = sorted(
            memories,
            key=lambda m: -(float(getattr(m, "importance", 0.0) or 0.0)),
        )
        groups: list[list[StoredMemory]] = []
        group_embs: list[list[list[float]]] = []
        for m in ranked:
            text = (m.text or "").strip()
            if not text:
                continue
            emb = _hash_embed(text)
            placed = False
            for gi, grp in enumerate(groups):
                if grp[0].kind != m.kind:
                    continue
                # Compare against every existing member's embedding
                for prev_emb in group_embs[gi]:
                    if _cos(emb, prev_emb) >= 0.85:
                        grp.append(m)
                        group_embs[gi].append(emb)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                groups.append([m])
                group_embs.append([emb])
        merged = 0
        survivors: set[str] = set()
        for grp in groups:
            if not grp:
                continue
            head = grp[0]
            survivors.add(head.id)
            if len(grp) <= 1:
                continue
            head_imp = float(getattr(head, "importance", 0.0) or 0.0)
            for dup in grp[1:]:
                head_imp = min(1.0, head_imp + 0.02)
                try:
                    self.store.delete_memory(dup.id)
                    merged += 1
                except Exception:
                    pass
            if head_imp > float(getattr(head, "importance", 0.0) or 0.0):
                try:
                    self.store.upsert_memory(
                        id=head.id, kind=head.kind,
                        text=head.text, importance=head_imp,
                        source=head.source, session_id=head.session_id,
                        created_at=head.created_at, updated_at=time.time(),
                        ttl=head.ttl, tags=list(head.tags or []),
                        embedding=head.embedding,
                    )
                except Exception:
                    pass
        stats.deduped = merged
        return [m for m in memories if m.id in survivors]


    # --- Stage 5: Evolution memo ----------------------------------------

    def _stage5_memo(self, stats: EvolutionStats) -> None:
        """Persist a short memo describing what this run changed so the next
        run's wiki prompt can use it as evolution context."""
        memo = {
            "ts": time.time(),
            "rescored": stats.rescored,
            "dropped": stats.dropped,
            "resummarized": stats.resummarized,
            "clusters": stats.clusters,
            "wiki_created": stats.wiki_created,
            "wiki_updated": stats.wiki_updated,
            "notes": stats.notes[:3],
        }
        text = json.dumps(memo, ensure_ascii=False)
        try:
            self.store.set_setting("evolution_memo", text)
        except Exception:
            pass

    # --- utilities -------------------------------------------------------

    def _cached_call(
        self,
        cache_key: str,
        system: str,
        user_prompt: str,
        cfg: dict[str, Any],
        stats: EvolutionStats,
        *,
        kind: str = "",
    ) -> str:
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached is not None and (now - self._cache_ts.get(cache_key, 0)) < self._cache_ttl:
            return cached
        history = ChatHistory(system=system, messages=[Message(role="user", content=user_prompt)])
        try:
            reply = self.provider.complete(
                history,
                temperature=float(cfg.get("temperature") or 0.3),
                max_tokens=int(cfg.get("max_output_tokens") or 900),
            ) or ""
        except Exception as e:
            stats.notes.append(f"{kind} llm error: {type(e).__name__}: {e}")
            return ""
        self._cache[cache_key] = reply
        self._cache_ts[cache_key] = now
        if kind == "cluster":
            stats.cluster_calls += 1
        elif kind == "wiki":
            stats.wiki_calls += 1
        return reply

    def _record_stage(
        self,
        stage: str,
        in_count: int,
        out_count: int,
        note: str,
        stats: dict[str, Any],
    ) -> None:
        try:
            rid = self.store.start_pipeline_run(stage)
            self.store.finish_pipeline_run(
                rid, in_count=in_count, out_count=out_count, note=note, stats=stats,
            )
        except Exception:
            pass

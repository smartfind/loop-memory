"""LLM-driven consolidation: filter, score, summarize.

Three jobs share one pipeline:

    1. **filter**  — drop low-signal memories (greetings, "ok", duplicates).
    2. **score**   — ask the LLM to re-rate the importance of every
                     memory in [0, 1] using context the heuristic
                     scorer can't see (semantic relevance to the
                     user's recent intent, factual density, etc.).
    3. **summarize** — fold near-duplicate memories into a single
                     distilled fact.

The same ``LLMConsolidator`` runs all three. Each step is independent
and can be toggled off in the behaviour config. The LLM is called in
small batches (default 50) so we stay within context limits and the
HTTP call stays cheap.

A run is **idempotent against the same LLM config** within a 5-minute
window — we keep a tiny cache of (batch hash → LLM response) so
manual reruns don't burn the same tokens twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import ChatHistory, LLMClient, Message
from ..storage.sqlite_store import MemoryStore, StoredMemory

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON helpers - the LLM is asked to return strict JSON; we parse leniently
# ---------------------------------------------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str) -> Any | None:
    """Best-effort JSON extraction. Handles fenced code blocks, leading
    prose like 'Here is the JSON: {...}', and bare objects/arrays."""
    if not text:
        return None
    s = text.strip()
    m = _JSON_FENCE.search(s)
    if m:
        s = m.group(1).strip()
    # find first { or [
    for i, ch in enumerate(s):
        if ch in "[{":
            sub = s[i:]
            # walk to matching close, allowing nested quotes
            depth = 0
            in_str = False
            esc = False
            for j, c in enumerate(sub):
                if esc:
                    esc = False
                    continue
                if c == "\\\\":
                    esc = True
                    continue
                if c == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c in "[{":
                    depth += 1
                elif c in "]}":
                    depth -= 1
                    if depth == 0:
                        cand = sub[: j + 1]
                        try:
                            return json.loads(cand)
                        except Exception:
                            break
            # fall through: try whole string
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the memory curator of a long-term recall store.
You will be given a batch of memory records previously written by an AI
coding assistant during user sessions. Your job is to keep the store
*small, dense, and useful* — never grow it.

For each record, you decide:

  - ``keep`` (true|false): drop chit-chat, greetings, fragments,
    repeated filler, and anything that is not a durable fact about the
    user, their project, or a concrete conclusion from the session.
  - ``importance`` (0.0–1.0): how likely this memory is to be retrieved
    again in a future session about the same project. Score anchors:
        0.0   – pure noise (drop)
        0.2   – one-off turn detail
        0.4   – mild context, mildly useful
        0.6   – durable fact / preference / conclusion
        0.8   – load-bearing fact the assistant would benefit from
                remembering next session
        1.0   – identity, hard constraint, key project decision
  - ``tags`` (array of short lowercase strings, optional): refined tags.
  - ``distill`` (string, optional): if this memory overlaps with another
    record by the same fact, provide the *merged, single* statement here
    instead of the original. Empty string means "no change".

Respond with a strict JSON object:
{
  "items": [
    {"id": "<memory id>", "keep": true, "importance": 0.6,
     "tags": ["foo","bar"], "distill": ""},
    ...
  ]
}

No prose, no markdown outside the JSON, no trailing commentary.
"""


# ---------------------------------------------------------------------------
# Wiki synthesis prompt
# ---------------------------------------------------------------------------

WIKI_SYSTEM_PROMPT = """You are a knowledge curator. You will be given a
batch of memory records (already filtered for noise and rewritten for
clarity) from a developer\'s AI-coding sessions. Your job is to write a
small set of polished, durable *wiki pages* — long-form notes that
capture what the developer knows / is doing / has decided so far.

Each wiki page must:

  - Be self-contained and read well on its own (no "as mentioned above").
  - Cover ONE coherent topic; do not cram unrelated facts together.
  - Use a clear, neutral tone. Prefer present tense for current state,
    past tense for historical facts.
  - Include concrete details (file paths, function names, dollar
    amounts, dates) when they appear in the source memories.
  - Be 2-6 short paragraphs or a tight bulleted list — *not* an essay.
  - Cite the contributing memory ids in ``evidence_ids`` so the user
    can audit the source.

Output strict JSON of the form:
{
  "pages": [
    {
      "slug": "kebab-case-3-to-5-words",
      "title": "Short descriptive title",
      "summary": "One-sentence TL;DR",
      "body": "Markdown body. Use short paragraphs or bullets.",
      "tags": ["project-name", "topic"],
      "importance": 0.6,
      "evidence_ids": ["mem_id_1", "mem_id_2"]
    }
  ]
}

Hard rules:
  - Produce BETWEEN 1 AND 6 pages. If the source memories don\'t form a
    coherent topic, return an empty ``pages`` array.
  - Slugs must be unique within the response and kebab-case lowercase.
  - Do NOT invent details that aren\'t in the source memories.
  - If the same topic was already covered in a previous wiki page,
    still include it here — the system will merge by slug and bump the
    version.
  - No prose outside the JSON.
"""


# ---------------------------------------------------------------------------
# Public dataclass — what a run returns
# ---------------------------------------------------------------------------

@dataclass
class ConsolidateStats:
    scanned: int = 0
    kept: int = 0
    dropped: int = 0
    resummarized: int = 0
    importance_updated: int = 0
    batches: int = 0
    llm_calls: int = 0
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    wiki_pages_created: int = 0
    wiki_pages_updated: int = 0
    wiki_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "kept": self.kept,
            "dropped": self.dropped,
            "resummarized": self.resummarized,
            "importance_updated": self.importance_updated,
            "batches": self.batches,
            "llm_calls": self.llm_calls,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "notes": self.notes,
            "wiki_pages_created": self.wiki_pages_created,
            "wiki_pages_updated": self.wiki_pages_updated,
            "wiki_calls": self.wiki_calls,
        }


# ---------------------------------------------------------------------------
# The consolidator
# ---------------------------------------------------------------------------

# Heuristics used as a fast pre-filter so we don't even ask the LLM
# about the obvious junk.
_PURE_NOISE = re.compile(
    r"^\s*(ok|okay|sure|thanks|thank you|hi|hello|hey|好的|收到|明白|了解|嗯|哦|行)\s*[.!?,;。！,，；]?\s*$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")
_DIGIT_RE = re.compile(r"\d{2,}")
_PATH_RE = re.compile(r"(/[A-Za-z0-9_.-]+){2,}")


def _looks_like_noise(text: str) -> bool:
    """Cheap signal/noise test. Conservative — only flags obvious junk."""
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < 4:
        return True
    if _PURE_NOISE.match(t):
        return True
    return False


_RAW_TRANSCRIPT_PREFIXES = (
    "user said:", "user:", "assistant said:", "assistant:",
    "human:", "human said:", "ai said:", "ai:",
)


def _is_raw_transcript(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return any(t.startswith(p) for p in _RAW_TRANSCRIPT_PREFIXES)


def _info_density(text: str) -> float:
    """Cheap density score [0,1] — used as a tie-breaker and to decide
    whether to even send a memory to the LLM."""
    t = (text or "").strip()
    if not t:
        return 0.0
    score = 0.0
    if _URL_RE.search(t):
        score += 0.35
    if _PATH_RE.search(t):
        score += 0.15
    if _DIGIT_RE.search(t):
        score += 0.10
    # 1 CJK char ≈ 1 token; english words average 5 chars
    cjk = sum(1 for c in t if "一" <= c <= "鿿")
    words = len(t.split())
    if cjk >= 6 or words >= 8:
        score += 0.20
    if any(ch in t for ch in "（）()[]【】{}「」『』"):
        score += 0.05
    if any(kw in t for kw in ("TODO", "FIXME", "BUG", "决定", "结论", "约束", "不要", "always", "never")):
        score += 0.10
    return min(1.0, score)


class LLMConsolidator:
    """Filter / score / summarize a chunk of memories using an LLM.

    ``provider`` is any ``LLMClient`` (the providers module has
    OpenAI / Anthropic / Ollama / rule-based).
    ``store`` is a ``MemoryStore`` (used for reading memories and
    writing back importance / text / deletions).
    ``config`` is the ``behaviour`` sub-dict from settings.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMClient,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = dict(config or {})
        self._cache: dict[str, str] = {}  # batch hash -> LLM reply
        self._cache_ttl = 300.0
        self._cache_ts: dict[str, float] = {}

    # --- public -----------------------------------------------------------

    def run(
        self,
        memories: list[StoredMemory] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> ConsolidateStats:
        t0 = time.time()
        stats = ConsolidateStats()
        cfg = self.config
        batch_size = max(1, int(cfg.get("batch_size") or 50))
        max(80, int(cfg.get("max_text_chars") or 1200))
        enable_filter = bool(cfg.get("enable_filter", True))
        enable_score = bool(cfg.get("enable_score", True))
        enable_summarize = bool(cfg.get("enable_summarize", True))
        min_importance = float(cfg.get("min_importance") or 0.0)
        dry_run = bool(cfg.get("dry_run", False))

        if memories is None:
            memories = self.store.list_memories(limit=batch_size * 50)
        memories = list(memories)
        stats.scanned = len(memories)

        if not memories:
            stats.notes.append("no memories to process")
            stats.elapsed_ms = (time.time() - t0) * 1000
            return stats

        # Fire an initial progress event so the UI can show "0/N"
        # immediately instead of "0/0".
        if progress:
            try:
                progress(0, len(memories))
            except Exception:
                pass

        # Rule-based pre-filter (cheap, no LLM call)
        pre_drop: set = set()
        raw_drop = 0
        for m in memories:
            if _looks_like_noise(m.text):
                pre_drop.add(m.id)
                continue
            if min_importance and (m.importance or 0) < min_importance:
                pre_drop.add(m.id)
                continue
            if (m.importance or 0) < 0.05 and _info_density(m.text) < 0.05:
                pre_drop.add(m.id)
                continue
            if _is_raw_transcript(m.text):
                pre_drop.add(m.id)
                raw_drop += 1
                continue
            # Bare episode snippets with little information density
            # (auto-snippets like "[codex] 你可以做什么？").
            if (m.kind == "episode"
                    and (m.importance or 0) < 0.6
                    and _info_density(m.text) < 0.25
                    and len(m.text or "") < 120):
                pre_drop.add(m.id)
                continue
        if pre_drop:
            parts = [f"pre-filter dropped {len(pre_drop)} rows"]
            if raw_drop:
                parts.append(f"{raw_drop} raw transcripts")
            stats.notes.append(" · ".join(parts))

        # Process in batches
        slow_ms = int(os.environ.get('LOOP_MEMORY_SLOW_CONSOLIDATE_MS', '0') or '0')
        for batch_start in range(0, len(memories), batch_size):
            batch = memories[batch_start : batch_start + batch_size]
            self._process_batch(
                batch, pre_drop, enable_filter, enable_score, enable_summarize, dry_run, stats
            )
            stats.batches += 1
            if progress:
                try:
                    progress(min(batch_start + batch_size, len(memories)), len(memories))
                except Exception:
                    pass
            if slow_ms > 0:
                time.sleep(slow_ms / 1000.0)

        # Recompute scores from the new importance
        if enable_score and not dry_run and stats.importance_updated:
            self.store.rescore_all(half_life_days=30.0)

        # ------------------------------------------------------------------
        # Wiki synthesis: ask the LLM to distill the kept memories into a
        # small set of polished, durable wiki pages. These are upserted by
        # slug, so re-running consolidation merges naturally.
        # ------------------------------------------------------------------
        enable_wiki = bool(cfg.get("enable_wiki", True))
        if enable_wiki and not dry_run:
            try:
                wiki_stats = self._synth_wiki_pages(
                    memories=memories,
                    pre_drop=pre_drop,
                    cfg=cfg,
                    stats=stats,
                    run_id=getattr(self, "_run_id", None),
                )
                stats.wiki_pages_created += wiki_stats.get("created", 0)
                stats.wiki_pages_updated += wiki_stats.get("updated", 0)
                stats.wiki_calls += wiki_stats.get("calls", 0)
                if wiki_stats.get("notes"):
                    stats.notes.extend(wiki_stats["notes"])
            except Exception as e:
                log.exception("wiki synthesis failed: %s", e)
                stats.notes.append(f"wiki error: {type(e).__name__}")

        stats.kept = stats.scanned - stats.dropped
        stats.elapsed_ms = (time.time() - t0) * 1000
        return stats

    def set_run_id(self, run_id: str | None) -> None:
        """Stash the consolidation run id so wiki pages can record it."""
        self._run_id = run_id

    def _echo_provider(self) -> bool:
        """True when the configured provider is the rule-based ``echo``."""
        cls = type(self.provider).__name__
        return cls in ("RuleBasedProvider",)

    def _synth_wiki_pages(
        self,
        memories: list[StoredMemory],
        pre_drop: set,
        cfg: dict[str, Any],
        stats: ConsolidateStats,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Distill ``memories`` into durable wiki pages.

        Uses the LLM when one is configured; falls back to a
        deterministic rule-based pass when the provider is the
        built-in ``echo`` provider. The fallback still produces
        real wiki pages so the UI has something to show.

        Returns ``{"created": int, "updated": int, "calls": int,
        "notes": [str]}``.
        """
        # Build the candidate set: kept memories, importance >= 0.35,
        # non-trivial length.
        candidates: list[StoredMemory] = []
        for m in memories:
            if m.id in pre_drop:
                continue
            if (m.importance or 0) < 0.35:
                continue
            txt = (m.text or "").strip()
            if len(txt) < 20:
                continue
            candidates.append(m)

        if not candidates:
            return {"created": 0, "updated": 0, "calls": 0,
                    "notes": ["no candidates for wiki synthesis"]}

        # Echo / rule-based path: deterministic clustering of kept
        # memories into wiki pages grouped by kind.
        if self._echo_provider():
            return self._synth_wiki_pages_rules(
                candidates, run_id, stats
            )

        # Cap the prompt: ~25 memories is plenty for one synthesis pass.
        candidates = candidates[:25]


        # Build payload + cache key.
        user_payload = []
        for m in candidates:
            txt = (m.text or "").strip()
            if len(txt) > 600:
                txt = txt[:599] + "…"
            user_payload.append({
                "id": m.id,
                "kind": m.kind,
                "tags": list(m.tags or []),
                "importance": round(m.importance or 0.0, 3),
                "text": txt,
            })
        user_prompt = json.dumps({"memories": user_payload}, ensure_ascii=False)

        cache_blob = (
            user_prompt
            + "||"
            + (getattr(self.provider, "model", "?") or "?")
            + "||wiki"
        )
        cache_key = hashlib.sha1(cache_blob.encode("utf-8")).hexdigest()
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached is not None and (now - self._cache_ts.get(cache_key, 0)) < self._cache_ttl:
            reply = cached
        else:
            history = ChatHistory(
                system=WIKI_SYSTEM_PROMPT,
                messages=[Message(role="user", content=user_prompt)],
            )
            try:
                reply = self.provider.complete(
                    history,
                    temperature=float(cfg.get("temperature") or 0.3),
                    max_tokens=min(int(cfg.get("max_output_tokens") or 800), 2000),
                ) or ""
                self._cache[cache_key] = reply
                self._cache_ts[cache_key] = now
                stats.llm_calls += 1
            except Exception as e:
                stats.notes.append(f"wiki llm error: {type(e).__name__}: {e}")
                return {"created": 0, "updated": 0, "calls": 0,
                        "notes": [f"wiki llm error: {e}"]}

        obj = _extract_json(reply)
        if not obj:
            return {"created": 0, "updated": 0, "calls": 1,
                    "notes": ["wiki reply was not valid JSON"]}

        pages = obj.get("pages") or []
        if not isinstance(pages, list):
            return {"created": 0, "updated": 0, "calls": 1,
                    "notes": ["wiki reply missing 'pages' array"]}

        created = 0
        updated = 0
        for p in pages:
            if not isinstance(p, dict):
                continue
            slug = (p.get("slug") or "").strip()
            title = (p.get("title") or "").strip()
            body = (p.get("body") or "").strip()
            if not slug or not title or not body:
                continue
            slug = slug.lower().replace(" ", "-")[:80]
            tags = p.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip().lower() for t in tags if str(t).strip()][:8]
            importance = p.get("importance")
            try:
                importance = max(0.0, min(1.0, float(importance)))
            except Exception:
                importance = 0.5
            evidence = p.get("evidence_ids") or []
            if not isinstance(evidence, list):
                evidence = []
            evidence = [str(x) for x in evidence if x][:50]
            summary = (p.get("summary") or "").strip()[:400]

            existing = self.store.get_wiki_page_by_slug(slug)
            self.store.upsert_wiki_page(
                slug=slug, title=title, body=body, summary=summary,
                tags=tags, importance=importance, evidence_ids=evidence,
                run_id=run_id,
            )
            if existing is None:
                created += 1
            else:
                updated += 1

        return {"created": created, "updated": updated, "calls": 1,
                "notes": []}

    # --- rules-based wiki synthesis -------------------------------------

    def _synth_wiki_pages_rules(
        self,
        candidates: list[StoredMemory],
        run_id: str | None,
        stats: ConsolidateStats,
    ) -> dict[str, Any]:
        """Deterministic wiki synthesis when no LLM is configured.

        Groups candidates by ``kind`` and produces one wiki page per
        kind with the highest-importance memories as the body. This
        gives the user real, browsable wiki content even before they
        wire up a model.
        """
        groups: dict[str, list[StoredMemory]] = {}
        for m in candidates:
            kind = (m.kind or "misc").strip() or "misc"
            groups.setdefault(kind, []).append(m)
        for items in groups.values():
            items.sort(key=lambda x: (x.importance or 0), reverse=True)

        created = 0
        updated = 0
        title_map = {
            "fact": "已提炼的事实 (Facts)",
            "episode": "近期活动摘要 (Episodes)",
            "preference": "用户偏好 (Preferences)",
            "summary": "摘要 (Summaries)",
            "misc": "其他记忆 (Misc)",
        }
        for kind, items in groups.items():
            if not items:
                continue
            top = items[:8]
            slug = f"auto-{kind}"
            title = title_map.get(kind, f"{kind} 类记忆")
            lines = []
            for m in top:
                t = (m.text or "").strip()
                if len(t) > 280:
                    t = t[:279] + "…"
                imp = m.importance or 0
                lines.append(f"- [{imp:.2f}] {t}")
            body = "\n".join(lines) if lines else "(no memories)"
            summary = f"按 kind={kind} 自动聚类的 {len(items)} 条记忆浓缩而成"
            tags = sorted({t for m in items for t in (m.tags or []) if t})[:6]
            importance = round(
                sum(m.importance or 0 for m in items) / max(1, len(items)), 3
            )
            evidence = [m.id for m in items][:50]
            existing = self.store.get_wiki_page_by_slug(slug)
            self.store.upsert_wiki_page(
                slug=slug, title=title, body=body, summary=summary,
                tags=tags, importance=importance, evidence_ids=evidence,
                run_id=run_id,
            )
            if existing is None:
                created += 1
            else:
                updated += 1
        stats.llm_calls += 1
        return {
            "created": created, "updated": updated, "calls": 1,
            "notes": [f"echo-mode: {created+updated} wiki pages from {len(candidates)} candidates"],
        }

    # --- batch ------------------------------------------------------------

    def _process_batch(
        self,
        batch: list[StoredMemory],
        pre_drop: set,
        enable_filter: bool,
        enable_score: bool,
        enable_summarize: bool,
        dry_run: bool,
        stats: ConsolidateStats,
    ) -> None:
        cfg = self.config
        max_chars = int(cfg.get("max_text_chars") or 1200)

        # Build the user prompt - the LLM is the only place we apply
        # semantically-aware filtering; the rule-based pass above was
        # just to avoid wasting tokens.
        user_payload: list[dict[str, Any]] = []
        for m in batch:
            txt = (m.text or "").strip()
            if len(txt) > max_chars:
                txt = txt[: max_chars - 1] + "…"
            user_payload.append({
                "id": m.id,
                "kind": m.kind,
                "tags": list(m.tags or []),
                "importance": round(m.importance or 0.0, 3),
                "score": round(m.score or 0.0, 3),
                "source": m.source or "",
                "created_at": int(m.created_at),
                "text": txt,
            })
        user_prompt = json.dumps({"items": user_payload}, ensure_ascii=False)

        # Cache key: payload + provider.model + temperature
        cache_blob = (
            user_prompt
            + "||"
            + (getattr(self.provider, "model", "?") or "?")
            + "||"
            + str(float(cfg.get("temperature") or 0.3))
        )
        cache_key = hashlib.sha1(cache_blob.encode("utf-8")).hexdigest()
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached is not None and (now - self._cache_ts.get(cache_key, 0)) < self._cache_ttl:
            reply = cached
        else:
            history = ChatHistory(
                system=SYSTEM_PROMPT,
                messages=[Message(role="user", content=user_prompt)],
            )
            t0 = time.time()
            err = None
            try:
                reply = self.provider.complete(
                    history,
                    temperature=float(cfg.get("temperature") or 0.3),
                    max_tokens=int(cfg.get("max_output_tokens") or 800),
                ) or ""
                stats.llm_calls += 1
                self._cache[cache_key] = reply
                self._cache_ts[cache_key] = now
            except Exception as e:
                log.warning("LLM call failed: %s", e)
                stats.notes.append(f"llm error in batch: {type(e).__name__}: {e}")
                reply = ""
                err = f"{type(e).__name__}: {e}"
            # Record the call to the audit log (best effort)
            try:
                from ..storage.sqlite_store import LLMAuditStore
                if not hasattr(self, "_audit"):
                    self._audit = LLMAuditStore(self.store)
                latency_ms = int((time.time() - t0) * 1000)
                # Cheap token estimate: chars/4
                pt = max(1, len(SYSTEM_PROMPT) // 4) + max(1, len(user_prompt) // 4)
                ct = max(1, len(reply) // 4) if reply else 0
                self._audit.record(
                    provider=type(self.provider).__name__,
                    model=getattr(self.provider, "model", "?") or "?",
                    kind="consolidate",
                    prompt=history.to_prompt() if hasattr(history, "to_prompt") else user_prompt,
                    response=reply or "",
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    cost_usd=0.0,  # providers don't expose cost
                    latency_ms=latency_ms,
                    ok=err is None,
                    error=err,
                    run_id=getattr(self, "_run_id", None),
                )
            except Exception:
                log.exception("audit record failed (non-fatal)")

        parsed = _extract_json(reply) if reply else None
        actions: dict[str, dict[str, Any]] = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            for it in parsed["items"]:
                if isinstance(it, dict) and "id" in it:
                    actions[str(it["id"])] = it

        # Apply actions
        for m in batch:
            mid = m.id
            if mid in pre_drop:
                if not dry_run:
                    self.store.delete_memory(mid)
                stats.dropped += 1
                continue
            act = actions.get(mid)
            if not act:
                # LLM didn't return anything for this row - keep as-is
                continue
            if enable_filter and act.get("keep") is False:
                if not dry_run:
                    self.store.delete_memory(mid)
                stats.dropped += 1
                continue
            new_importance = act.get("importance")
            new_text = (act.get("distill") or "").strip()
            new_tags = act.get("tags")
            changed = False
            updates: dict[str, Any] = {}
            if enable_score and isinstance(new_importance, (int, float)):
                ni = max(0.0, min(1.0, float(new_importance)))
                if abs(ni - (m.importance or 0.0)) > 1e-3:
                    updates["importance"] = ni
                    stats.importance_updated += 1
                    changed = True
            if enable_summarize and new_text and new_text != m.text:
                updates["text"] = new_text[: max_chars]
                stats.resummarized += 1
                changed = True
            if enable_filter and isinstance(new_tags, list):
                tags_clean = [str(t).strip() for t in new_tags if t and str(t).strip()]
                tags_clean = tags_clean[:8]
                if tags_clean != (m.tags or []):
                    updates["tags"] = tags_clean
                    changed = True
            if changed and not dry_run:
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

    # --- helpers ----------------------------------------------------------

    def preview(
        self,
        memories: list[StoredMemory] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return a small 'what would happen' preview without writing."""
        if memories is None:
            memories = self.store.list_memories(limit=limit)
        out: list[dict[str, Any]] = []
        for m in memories[:limit]:
            out.append({
                "id": m.id,
                "text": (m.text or "")[:200],
                "kind": m.kind,
                "importance": m.importance,
                "noise": _looks_like_noise(m.text),
                "density": round(_info_density(m.text), 2),
                "would_drop": _looks_like_noise(m.text) or (
                    (m.importance or 0) < 0.05 and _info_density(m.text) < 0.05
                ),
            })
        return out

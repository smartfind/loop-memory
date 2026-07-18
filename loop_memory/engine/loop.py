"""The Loop Engine.

Each call to ``engine.turn(user_msg)`` runs the canonical four-stage loop:

    1. RETRIEVE   — pull relevant items from long-term memory, recent
                    episodes, and any open task plan.
    2. GENERATE   — build an augmented prompt and ask the LLM to
                    produce an answer.
    3. REFLECT    — turn the new exchange into durable facts.
    4. STORE      — persist facts, record an episode, push to short-term
                    scratchpad with periodic compaction + GC.

The engine is intentionally small and synchronous. It is the *contract*
the rest of the project depends on. Replace the LLM, reflector, embedder,
or vector store to specialize the loop without touching this file.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..backends.embedding import BaseEmbedder, IdentityEmbedder
from ..backends.vector_store import InMemoryVectorStore, VectorStore
from ..llm.base import ChatHistory, LLMClient, Message
from ..memory.types import (
    EpisodicMemory,
    LongTermMemory,
    MemoryItem,
    ProceduralMemory,
    ShortTermMemory,
)
from .reflect import Reflector, summarize_window

log = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "You are a thoughtful assistant with persistent memory.\n"
    "You will be given RELEVANT MEMORIES recalled from long-term storage, "
    "a brief EPISODE LOG of recent events, and any open TASK PLANS. "
    "Use them naturally when they help. If the user is mid-task, continue "
    "the plan instead of starting a new one. Never claim to remember "
    "something that isn't in the memory section."
)


@dataclass
class LoopResult:
    reply: str
    retrieved: list[MemoryItem] = field(default_factory=list)
    stored: list[MemoryItem] = field(default_factory=list)
    reflection: list[MemoryItem] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


class LoopEngine:
    def __init__(
        self,
        llm: LLMClient,
        embedder: BaseEmbedder | None = None,
        longterm: LongTermMemory | None = None,
        episodic: EpisodicMemory | None = None,
        procedural: ProceduralMemory | None = None,
        vector_store: VectorStore | None = None,
        short_capacity: int = 16,
        compact_at: int | None = None,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required")
        self.llm = llm
        self.embedder = embedder or IdentityEmbedder()
        self.short = ShortTermMemory(capacity=short_capacity)
        self.long = longterm or LongTermMemory()
        self.episodic = episodic or EpisodicMemory()
        self.procedural = procedural or ProceduralMemory()
        self.vector_store: VectorStore = vector_store or InMemoryVectorStore()
        self.compact_at = compact_at if compact_at is not None else short_capacity
        self.reflector: Callable[[str], list[MemoryItem]] = Reflector(
            llm=self.llm, use_llm=False
        ).extract

    # --- public hooks ------------------------------------------------------

    def set_reflector(self, fn: Callable[[str], list[MemoryItem]]) -> None:
        """Replace the reflection step. ``fn`` receives raw user text."""
        self.reflector = fn

    def push_plan(self, goal_text: str) -> MemoryItem:
        goal = MemoryItem(text=goal_text, importance=0.6, kind="plan", tags=["goal"])
        self.procedural.push(goal)
        return goal

    def complete_current_plan(self) -> MemoryItem | None:
        return self.procedural.complete_top()

    def recall(self, query: str, k: int = 5) -> list[MemoryItem]:
        q_emb = self.embedder.embed_query(query) if self.embedder.dim else None
        return self.long.search(query_embedding=q_emb, top_k=k)

    # --- the canonical loop ------------------------------------------------

    def turn(
        self,
        user_msg: str,
        *,
        k: int = 5,
        max_chars: int = 6000,
    ) -> LoopResult:
        if not isinstance(user_msg, str) or not user_msg.strip():
            raise ValueError("user_msg must be a non-empty string")

        # --- 1. RETRIEVE ---
        q_emb = self.embedder.embed_query(user_msg) if self.embedder.dim else None
        retrieved = self.long.search(query_embedding=q_emb, top_k=k)
        recent_episodes = self.episodic.recent(3)
        current_plan = self.procedural.current()

        memory_block = self._format_memory_block(retrieved, recent_episodes, current_plan)
        short_window = self.short.items()
        short_text = "\n".join(f"- [{m.kind}] {m.text}" for m in short_window)

        prompt = (
            f"{memory_block}\n\n"
            f"RECENT SCRATCHPAD:\n{short_text or '(empty)'}\n\n"
            f"USER: {user_msg}\n"
            f"ASSISTANT:"
        )
        # Hard cap so a runaway prompt never explodes the LLM context.
        if len(prompt) > max_chars:
            prompt = prompt[: max(0, max_chars - 1)].rstrip() + "…"

        # --- 2. GENERATE ---
        history = ChatHistory(
            system=SYSTEM_PROMPT,
            messages=[Message("user", prompt)],
        )
        t0 = time.time()
        try:
            reply = self.llm.complete(history)
        except Exception:  # pragma: no cover — exercised only with bad adapters
            log.exception("LLM call failed during turn()")
            raise
        gen_ms = (time.time() - t0) * 1000

        # --- 3. REFLECT ---
        try:
            reflection = self.reflector(user_msg) or []
        except Exception:
            log.exception("Reflector failed; continuing without storing new facts")
            reflection = []

        if reflection and self.embedder.dim:
            vecs = self.embedder.embed(reflection)
            for item, vec in zip(reflection, vecs, strict=False):
                item.embedding = vec
        added_ltm = self.long.extend(reflection)
        if reflection and self.embedder.dim:
            try:
                self.vector_store.add([it for it in added_ltm if it.embedding is not None])
            except Exception:
                log.exception("Vector store add failed; continuing with in-LTM list")

        # Record an episode for the exchange.
        episode = MemoryItem(
            text=f"user: {user_msg}\nassistant: {reply}",
            importance=0.4,
            kind="episode",
        )
        self.episodic.record(episode)

        # --- 4. STORE ---
        self.short.push(MemoryItem(text=f"user: {user_msg}", importance=0.4, kind="turn"))
        self.short.push(MemoryItem(text=f"assistant: {reply}", importance=0.4, kind="turn"))

        # Periodic compaction: roll the scratchpad into a single summary item.
        if len(self.short._items) >= self.compact_at:
            summary = MemoryItem(
                text=f"summary: {summarize_window(short_window)}",
                importance=0.55,
                kind="reflection",
            )
            if self.embedder.dim:
                summary.embedding = self.embedder.embed_text(summary.text)
            self.long.add(summary)
            self.short.clear()

        # Background GC: drop expired long-term items on every turn.
        gc_removed = self.long.gc()

        diagnostics = {
            "retrieved": len(retrieved),
            "stored": len(added_ltm),
            "reflection_candidates": len(reflection),
            "scratchpad_size": len(self.short._items),
            "long_term_size": len(self.long),
            "episodes": len(self.episodic._events),
            "open_plans": len(self.procedural.goals),
            "expired_dropped": gc_removed,
            "gen_ms": round(gen_ms, 1),
            "prompt_chars": min(len(prompt), max_chars),
        }
        return LoopResult(
            reply=reply,
            retrieved=retrieved,
            stored=added_ltm,
            reflection=added_ltm,
            diagnostics=diagnostics,
        )

    # --- helpers ------------------------------------------------------------

    def _format_memory_block(
        self,
        retrieved,
        recent_episodes,
        current_plan: MemoryItem | None,
    ) -> str:
        parts = ["RELEVANT MEMORIES:"]
        retrieved_list = list(retrieved)
        if retrieved_list:
            for i, m in enumerate(retrieved_list, 1):
                parts.append(f"  {i}. ({m.kind}, importance={m.importance:.2f}) {m.text}")
        else:
            parts.append("  (none)")

        parts.append("\nEPISODE LOG (most recent first):")
        eps = list(recent_episodes)
        if eps:
            for m in reversed(eps):
                parts.append(f"  - {m.text}")
        else:
            parts.append("  (none)")

        if current_plan is not None:
            parts.append(f"\nOPEN TASK PLAN: {current_plan.text}")
        return "\n".join(parts)

    def __repr__(self) -> str:
        return (
            f"LoopEngine(llm={self.llm.model}, "
            f"short={len(self.short._items)}/{self.short.capacity}, "
            f"long={len(self.long)}, episodes={len(self.episodic._events)}, "
            f"plans={len(self.procedural.goals)})"
        )

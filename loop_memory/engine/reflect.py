"""Reflection and summarization passes.

The reflection step is what turns a sequence of raw messages into
structured long-term memories. In production it's an LLM call; here
we use a deterministic heuristic extractor so the framework remains
usable without an API key. Plug in a smarter pass via
``engine.set_reflector(...)``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..llm.base import LLMClient
from ..memory.types import MemoryItem

_NAME_RE = re.compile(r"\b(?:my name is|i am|i'm|call me)\s+([A-Z][a-zA-Z'-]{1,30})")
_LIKE_RE = re.compile(r"\b(?:i (?:really )?(?:like|love|enjoy|prefer))\s+([^.!?\n]+)", re.IGNORECASE)
_DISLIKE_RE = re.compile(r"\b(?:i (?:really )?(?:dislike|hate|avoid))\s+([^.!?\n]+)", re.IGNORECASE)
_FACT_HINT = ("my ", "i ", "we ", "always", "never", "remember", "important")


def heuristic_extract(text: str) -> list[MemoryItem]:
    """Cheap, deterministic fact extractor. Used when no LLM reflector is set."""
    found: list[MemoryItem] = []
    name = _NAME_RE.search(text)
    if name:
        found.append(MemoryItem(text=f"User's name is {name.group(1)}.", importance=0.95, kind="fact", tags=["identity"]))
    for m in _LIKE_RE.finditer(text):
        found.append(MemoryItem(text=f"User likes: {m.group(1).strip()}.", importance=0.7, kind="fact", tags=["preference"]))
    for m in _DISLIKE_RE.finditer(text):
        found.append(MemoryItem(text=f"User dislikes: {m.group(1).strip()}.", importance=0.7, kind="fact", tags=["preference"]))
    if not found:
        lower = text.lower()
        if any(h in lower for h in _FACT_HINT) and len(text) < 240:
            found.append(MemoryItem(text=text.strip(), importance=0.5, kind="fact"))
    return found


def summarize_window(messages: Iterable[MemoryItem], max_chars: int = 240) -> str:
    """Compress a short-term window into a single roll-up line."""
    pieces = [m.text for m in messages]
    joined = " | ".join(pieces)
    if len(joined) <= max_chars:
        return joined
    return joined[: max_chars - 1].rstrip() + "…"


class Reflector:
    """Pluggable reflection pass.

    Default uses ``heuristic_extract``. Pass an LLM-backed reflector
    for higher-quality, fewer-false-positive fact extraction:

        def llm_reflect(text, llm):
            prompt = f"Extract durable user facts from: {text}\nReturn JSON list."
            return [MemoryItem(text=s, importance=0.8) for s in parse(llm.complete(...))]
        engine.set_reflector(llm_reflect)
    """

    def __init__(self, llm: LLMClient | None = None, use_llm: bool = False) -> None:
        self.llm = llm
        self.use_llm = use_llm

    def extract(self, text: str) -> list[MemoryItem]:
        if self.use_llm and self.llm is not None:
            return self._llm_extract(text)
        return heuristic_extract(text)

    def _llm_extract(self, text: str) -> list[MemoryItem]:
        # Contract: the LLM is asked to return one fact per line, no commentary.
        from ..llm.base import ChatHistory

        history = ChatHistory(
            system="Extract durable user facts. One per line. No numbering. No preamble.",
            messages=[{"role": "user", "content": text}],  # type: ignore[list-item]
        ) if False else ChatHistory(
            system="Extract durable user facts. One per line. No numbering. No preamble.",
            messages=[],
        )
        history.messages.append(__import__("loop_memory.llm.base", fromlist=["Message"]).Message("user", text))
        out = self.llm.complete(history)
        facts: list[MemoryItem] = []
        for line in out.splitlines():
            line = line.strip(" -•\t")
            if 2 <= len(line) <= 200:
                facts.append(MemoryItem(text=line, importance=0.75, kind="fact"))
        return facts or heuristic_extract(text)

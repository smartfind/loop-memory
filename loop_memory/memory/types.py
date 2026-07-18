"""Typed memory objects shared across tiers.

A `MemoryItem` is the atomic unit of memory. Each tier (short / long /
episodic / procedural) wraps a collection of items with its own
retention, retrieval, and update semantics.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class MemoryItem:
    """Atomic memory record.

    Attributes:
        text: human-readable content.
        embedding: optional numeric vector (set when an embedder is wired in).
        importance: 0..1 score influencing retention priority.
        created_at: unix timestamp.
        ttl: time-to-live in seconds; ``None`` means it never expires.
        kind: 'fact' | 'episode' | 'plan' | 'reflection' | free-form.
        tags: lightweight labels for filtering.
        source: optional pointer back to the originating event.
    """

    text: str
    embedding: list[float] | None = None
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    ttl: float | None = None
    kind: str = "fact"
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def is_expired(self, now: float | None = None) -> bool:
        if self.ttl is None:
            return False
        cur = now if now is not None else time.time()
        return (cur - self.created_at) > self.ttl

    def score(self, now: float | None = None) -> float:
        """Recency × importance score in [0, 1]."""
        cur = now if now is not None else time.time()
        age = max(0.0, cur - self.created_at)
        # half-life decay: drop by half every `decay_half_life` seconds
        decay_half_life = 60 * 60 * 24 * 7  # 1 week
        recency = 0.5 ** (age / decay_half_life)
        return max(0.0, min(1.0, self.importance * (0.25 + 0.75 * recency)))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(x * x for x in b)) or 1e-12
    return dot / (na * nb)


@dataclass
class ShortTermMemory:
    """Ephemeral scratchpad — last N turns, FIFO eviction.

    Mirrors the LLM's working context window. Optional summarization
    is handled by the engine's reflection step.
    """

    capacity: int = 16
    _items: list[MemoryItem] = field(default_factory=list)

    def push(self, item: MemoryItem) -> None:
        self._items.append(item)
        if len(self._items) > self.capacity:
            self._items = self._items[-self.capacity :]

    def extend(self, items: Iterable[MemoryItem]) -> None:
        for it in items:
            self.push(it)

    def items(self) -> list[MemoryItem]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()


@dataclass
class LongTermMemory:
    """Persistent, semantic facts.

    Vector-similarity retrieval when an embedder is provided; otherwise
    falls back to importance-weighted lexical scoring.
    """

    _items: list[MemoryItem] = field(default_factory=list)
    dedupe_threshold: float = 0.92  # cosine threshold for duplicate suppression

    def add(self, item: MemoryItem) -> bool:
        """Add an item. Returns False if it was deduplicated."""
        if item.embedding is not None:
            for existing in self._items:
                if existing.embedding is not None:
                    sim = cosine_similarity(item.embedding, existing.embedding)
                    if sim >= self.dedupe_threshold:
                        # boost existing importance instead of duplicating
                        existing.importance = max(existing.importance, item.importance)
                        existing.created_at = min(existing.created_at, item.created_at)
                        return False
        self._items.append(item)
        return True

    def extend(self, items: Iterable[MemoryItem]) -> list[MemoryItem]:
        added: list[MemoryItem] = []
        for it in items:
            if self.add(it):
                added.append(it)
        return added

    def search(
        self,
        query_embedding: list[float] | None = None,
        top_k: int = 5,
        now: float | None = None,
    ) -> list[MemoryItem]:
        scored: list[tuple[float, MemoryItem]] = []
        for it in self._items:
            if it.is_expired(now):
                continue
            base = it.score(now)
            if query_embedding is not None and it.embedding is not None:
                base = 0.7 * base + 0.3 * cosine_similarity(query_embedding, it.embedding)
            scored.append((base, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:top_k]]

    def forget(self, predicate) -> int:
        before = len(self._items)
        self._items = [it for it in self._items if not predicate(it)]
        return before - len(self._items)

    def gc(self, now: float | None = None) -> int:
        return self.forget(lambda it: it.is_expired(now))

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class EpisodicMemory:
    """Time-ordered event stream: what happened, in what order.

    Used to answer "what did we do recently?" rather than "what do we
    know?". Think of it as a transactional log that the reflection step
    periodically compacts into long-term facts.
    """

    max_events: int = 1000
    _events: list[MemoryItem] = field(default_factory=list)

    def record(self, event: MemoryItem) -> None:
        event.kind = event.kind or "episode"
        self._events.append(event)
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events :]

    def recent(self, n: int = 5) -> list[MemoryItem]:
        return list(self._events[-n:])

    def between(self, t0: float, t1: float) -> list[MemoryItem]:
        return [e for e in self._events if t0 <= e.created_at <= t1]


@dataclass
class ProceduralMemory:
    """Structured task plan / current-goal stack.

    Lets the engine track what the user is *trying* to do across turns
    so a single message can be interpreted as the next step of an open
    plan rather than a fresh request.
    """

    goals: list[MemoryItem] = field(default_factory=list)

    def push(self, goal: MemoryItem) -> None:
        goal.kind = "plan"
        self.goals.append(goal)

    def current(self) -> MemoryItem | None:
        return self.goals[-1] if self.goals else None

    def complete_top(self) -> MemoryItem | None:
        return self.goals.pop() if self.goals else None

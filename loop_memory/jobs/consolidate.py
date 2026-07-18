"""Background jobs — consolidate, rescore, GC.

In production you would run these on a cron / launchd timer. The CLI
exposes each on its own so you can wire them straight into your
scheduler of choice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..backends.embedding import BaseEmbedder, IdentityEmbedder
from ..storage.sqlite_store import MemoryStore


@dataclass
class ConsolidateReport:
    rescored: int
    gc_removed: int
    merged: int
    elapsed_ms: float


class Consolidator:
    def __init__(
        self,
        store: MemoryStore,
        embedder: BaseEmbedder | None = None,
        *,
        half_life_days: float = 30.0,
        merge_threshold: float = 0.92,
    ) -> None:
        self.store = store
        self.embedder = embedder or IdentityEmbedder()
        self.half_life_days = half_life_days
        self.merge_threshold = merge_threshold

    def run(self) -> ConsolidateReport:
        t0 = time.time()
        rescored = self.store.rescore_all(self.half_life_days)
        gc_removed = self.store.gc()
        merged = self.merge_near_duplicates() if self.embedder.dim else 0
        return ConsolidateReport(
            rescored=rescored,
            gc_removed=gc_removed,
            merged=merged,
            elapsed_ms=(time.time() - t0) * 1000,
        )

    # --- de-duplication -----------------------------------------------------

    def merge_near_duplicates(self) -> int:
        """Drop near-duplicate memories, keeping the higher-importance one.

        Uses cosine similarity over the stored embeddings. If no embedder
        with ``dim > 0`` is wired in this is a no-op (returns 0).
        """
        if not self.embedder.dim:
            return 0
        items = self.store.list_memories(limit=10_000)
        # Group by kind to avoid merging "user-quote" with "fact".
        by_kind: dict[str, list] = {}
        for it in items:
            if it.embedding is None:
                continue
            by_kind.setdefault(it.kind, []).append(it)

        merged = 0
        for items in by_kind.values():
            # Sort by importance desc so winners stay.
            items.sort(key=lambda x: x.importance, reverse=True)
            to_delete = set()
            for i, a in enumerate(items):
                if a.id in to_delete:
                    continue
                for b in items[i + 1 :]:
                    if b.id in to_delete:
                        continue
                    sim = _cosine(a.embedding, b.embedding)
                    if sim >= self.merge_threshold:
                        to_delete.add(b.id)
                        merged += 1
            for mid in to_delete:
                self.store.delete_memory(mid)
        return merged


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5 or 1e-12
    nb = sum(x * x for x in b) ** 0.5 or 1e-12
    return dot / (na * nb)

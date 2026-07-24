"""Memory compaction — bound the long-term store.

Codex/Claude/Hermes sessions produce a *lot* of low-signal memory rows
per session. Over weeks this drowns both the on-disk DB and the recall
context we hand back to clients (which is the bigger user-visible
cost: feeding hundreds of stale rows back into the assistant
inflates its context and slows down / crashes the conversation).

This module keeps the store bounded with three layered strategies:

  1. **Heuristic compaction** — group aged memory rows by session and
     collapse them into a single condensed "session digest" row, then
     delete the originals. No LLM. Cheap, predictable, runs on a
     schedule.
  2. **Aggressive prune** — drop memories that have decayed below the
     floor (``importance * score < floor``) and have never been
     recalled. Belt-and-braces after (1).
  3. **LLM compaction** *(optional)* — when a provider is wired up, ask
     the model to fuse clusters of related memories into one dense
     statement. Slower but higher quality; the consolidator already
     runs LLM-driven distillation into wiki pages, so this is a
     complementary pass on the *raw* memory layer.

The scheduler in :mod:`jobs.scheduler` calls ``Compactor.run`` on the
configured cadence. The dashboard exposes a manual trigger so the
user can force a compaction after a heavy ingest burst.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..storage.sqlite_store import MemoryStore, StoredMemory

log = logging.getLogger(__name__)


# Heuristic noise patterns - common low-signal fragments we never want
# to keep even after distillation. Kept conservative; the LLM pass is
# the one that actually decides what to drop semantically.
_NOISE_PATTERNS = (
    "thanks", "thank you", "ok", "okay", "好的", "是", "对", "嗯",
    "got it", "sure", "yes", "no", "yep", "nope", "好的", "继续",
    "继续", "好的", "明白", "知道了", "了解了", "let's continue",
    "继续吧", "go on", "keep going", "next",
)


def _looks_like_noise(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    # Single-word / very short snippets that are usually acknowledgements
    if len(t) <= 6:
        return any(t.startswith(p) for p in _NOISE_PATTERNS) or not t.isascii() and len(t) <= 4
    return any(t == p or t.startswith(p + " ") for p in _NOISE_PATTERNS if len(p) >= 4)


@dataclass
class CompactReport:
    """Outcome of a single compaction run."""
    digested_sessions: int = 0
    deleted_memories: int = 0
    inserted_digests: int = 0
    pruned_noise: int = 0
    pruned_decayed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    elapsed_ms: float = 0.0
    mode: str = "heuristic"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "digested_sessions": self.digested_sessions,
            "deleted_memories": self.deleted_memories,
            "inserted_digests": self.inserted_digests,
            "pruned_noise": self.pruned_noise,
            "pruned_decayed": self.pruned_decayed,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_saved": max(0, self.bytes_before - self.bytes_after),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "mode": self.mode,
        }
        if self.notes:
            d["notes"] = self.notes[:8]
        return d


class Compactor:
    """Bound the memory store by digesting aged rows.

    Tunables are class-level so tests can pin them. Defaults are
    deliberately gentle — the LLM-driven wiki consolidator is the
    primary quality lever; this module is the *safety net* that
    keeps the DB from growing forever.
    """

    # Memory rows older than this AND not recently recalled get folded
    # into a session digest.
    age_seconds: int = 60 * 60 * 24 * 14  # 14 days
    # Memories older than this are eligible for aggressive prune even
    # if their score is OK, as long as they have zero recalls.
    prune_age_seconds: int = 60 * 60 * 24 * 30  # 30 days
    # Floor: importance * score below this AND zero recalls → drop.
    score_floor: float = 0.04
    # Keep at least this many highest-scoring memories per session, so
    # we never empty a session completely.
    min_keep_per_session: int = 1
    # Hard cap on total memories after compaction. If still over, drop
    # the lowest-scoring rows until under the cap.
    max_memories_after: int = 8000
    # Max characters per inserted digest row.
    digest_max_chars: int = 600

    def __init__(self, store: MemoryStore, *, mode: str = "heuristic") -> None:
        self.store = store
        self.mode = mode

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        force: bool = False,
    ) -> CompactReport:
        """Run a single compaction pass.

        ``force=True`` ignores the age filter (useful for a one-shot
        "tidy up after a burst" trigger from the UI).
        """
        t0 = time.time()
        report = CompactReport(mode=self.mode)
        report.bytes_before = self.store.db_size_bytes()

        def _step(cur: int, total: int, msg: str) -> None:
            if progress:
                try:
                    progress(cur, total, msg)
                except Exception:
                    pass

        # Step 1: heuristic per-session digest.
        _step(0, 100, "digesting aged sessions")
        aged_cutoff = 0.0 if force else (time.time() - self.age_seconds)
        r1 = self._digest_aged_sessions(cutoff=aged_cutoff, force=force)
        report.digested_sessions = r1["sessions"]
        report.inserted_digests = r1["digests"]
        report.deleted_memories += r1["deleted"]
        _step(30, 100, f"digested {r1['sessions']} sessions")

        # Step 2: aggressive prune of zero-recall decayed rows.
        _step(35, 100, "pruning noise")
        report.pruned_noise = self._prune_noise()
        _step(50, 100, "pruning decayed")
        report.pruned_decayed = self._prune_decayed(force=force)

        # Step 3: hard cap — if we are still over the soft ceiling,
        # drop lowest-scoring rows. This is the last resort.
        _step(70, 100, "enforcing ceiling")
        dropped = self._enforce_ceiling()
        if dropped:
            report.notes.append(f"ceiling dropped {dropped} rows")
            report.deleted_memories += dropped

        # Step 4: optional LLM compaction. Off by default — the wiki
        # consolidator already does the high-quality work; running the
        # LLM twice is wasteful unless explicitly requested.
        if self.mode == "llm":
            _step(85, 100, "LLM fusion")
            try:
                pass  # LLM fuse optional
                fused = llm_fuse_pass(self.store, force=force)
                report.notes.append(f"llm-fused {fused} clusters")
            except Exception as e:
                report.notes.append(f"llm fuse skipped: {e}")

        report.bytes_after = self.store.db_size_bytes()
        report.elapsed_ms = (time.time() - t0) * 1000
        _step(100, 100, "done")
        log.info(
            "compaction done: digested=%s deleted=%s noise=%s decayed=%s bytes=%s->%s (%.1f ms)",
            report.digested_sessions, report.deleted_memories,
            report.pruned_noise, report.pruned_decayed,
            report.bytes_before, report.bytes_after, report.elapsed_ms,
        )
        return report

    # ------------------------------------------------------------------
    # Strategy 1: per-session digest
    # ------------------------------------------------------------------

    def _digest_aged_sessions(self, *, cutoff: float, force: bool) -> dict[str, int]:
        """Group aged memory rows by session and replace them with a
        single condensed digest row.

        Sessions that already have <= ``min_keep_per_session`` aged rows
        are skipped — we don't waste time digesting a single line.
        """
        deleted = 0
        digests = 0
        sessions = 0

        for session_id, rows in self._group_aged_by_session(cutoff=cutoff, force=force):
            if len(rows) <= self.min_keep_per_session:
                continue
            # Keep the top-scoring rows, digest the rest.
            rows.sort(key=lambda m: (m.score or 0) * (m.importance or 0), reverse=True)
            digest_from = rows[self.min_keep_per_session:]
            if not digest_from:
                continue
            digest_text = self._synthesize_digest(digest_from)
            if not digest_text:
                continue
            try:
                self.store.upsert_memory(
                    kind="digest",
                    text=digest_text,
                    importance=max((m.importance for m in digest_from), default=0.4),
                    source=digest_from[0].source,
                    session_id=session_id,
                    tags=["compacted", "session-digest"],
                )
                digests += 1
            except Exception:
                log.exception("digest upsert failed for session %s", session_id)
                continue
            for m in digest_from:
                try:
                    self.store.delete_memory(m.id)
                    deleted += 1
                except Exception:
                    pass
            sessions += 1
        return {"sessions": sessions, "digests": digests, "deleted": deleted}

    def _group_aged_by_session(
        self, *, cutoff: float, force: bool
    ) -> Iterable[tuple[str, list[StoredMemory]]]:
        # We pull all memories and bucket in Python — the table is small
        # enough (<= a few thousand rows for typical users) and SQLite
        # doesn't have an efficient "group-by session" that respects the
        # age filter without a temp index.
        all_rows = self.store.list_memories(limit=20_000)
        buckets: dict[str, list[StoredMemory]] = {}
        now = time.time()
        for m in all_rows:
            if m.kind == "digest":
                continue  # never re-digest a digest
            # "Aged" = old enough AND not recently recalled. The recall
            # gate is a cheap proxy for "the user still cares".
            age_ok = force or (now - float(m.created_at or 0)) >= cutoff
            if not age_ok:
                continue
            try:
                sig = self.store.get_signal(m.id) or {}
                recently_used = bool(sig.get("recall_count"))
            except Exception:
                recently_used = False
            if recently_used and not force:
                continue
            buckets.setdefault(m.session_id or "_orphan", []).append(m)
        yield from buckets.items()

    def _synthesize_digest(self, rows: list[StoredMemory]) -> str:
        """Heuristic digest: keep one canonical line per unique
        5-token-prefix. Cap at ``digest_max_chars`` characters.
        """
        seen: set[str] = []
        out: list[str] = []
        for m in rows:
            txt = (m.text or "").strip().replace("\n", " ")
            if not txt:
                continue
            fp = txt[:60].lower()
            if fp in seen:
                continue
            seen.append(fp)
            out.append(f"• {txt}")
        body = "\n".join(out)
        if len(body) > self.digest_max_chars:
            body = body[: self.digest_max_chars - 1].rstrip() + "…"
        return body

    # ------------------------------------------------------------------
    # Strategy 2: aggressive prune
    # ------------------------------------------------------------------

    def _prune_noise(self) -> int:
        rows = self.store.list_memories(limit=20_000)
        deleted = 0
        for m in rows:
            if _looks_like_noise(m.text):
                try:
                    self.store.delete_memory(m.id)
                    deleted += 1
                except Exception:
                    pass
        return deleted

    def _prune_decayed(self, *, force: bool) -> int:
        """Drop memories that have decayed below the floor AND have
        never been recalled. We keep them if they have any recall
        signal — those are at least demonstrably useful.
        """
        rows = self.store.list_memories(limit=20_000)
        now = time.time()
        deleted = 0
        for m in rows:
            if m.kind == "digest":
                continue
            score = (float(m.score or 0)) * (float(m.importance or 0))
            age_ok = force or (now - float(m.created_at or 0)) >= self.prune_age_seconds
            if not age_ok:
                continue
            if score > self.score_floor:
                continue
            try:
                sig = self.store.get_signal(m.id) or {}
                if int(sig.get("recall_count") or 0) > 0:
                    continue
            except Exception:
                pass
            try:
                self.store.delete_memory(m.id)
                deleted += 1
            except Exception:
                pass
        return deleted

    # ------------------------------------------------------------------
    # Strategy 3: hard ceiling
    # ------------------------------------------------------------------

    def _enforce_ceiling(self) -> int:
        total = self.store.count_memories()
        if total <= self.max_memories_after:
            return 0
        # Pull the lowest-scoring rows and drop until under cap.
        rows = self.store.list_low_value_memories(limit=target + 50)
        target = max(0, total - self.max_memories_after)
        deleted = 0
        for m in rows:
            if deleted >= target:
                break
            if m.kind == "digest":
                continue
            try:
                sig = self.store.get_signal(m.id) or {}
                if int(sig.get("recall_count") or 0) > 0:
                    continue  # never drop a row the user has ever used
            except Exception:
                pass
            try:
                self.store.delete_memory(m.id)
                deleted += 1
            except Exception:
                pass
        return deleted


__all__ = ["Compactor", "CompactReport", "_looks_like_noise"]

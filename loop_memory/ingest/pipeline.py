"""Ingest pipeline.

``MemoryPipeline.run(session)`` writes a **single, summarised memory
record per conversation** rather than one row per turn. This keeps
the long-term store from drowning in chat noise — a typical 50-turn
session becomes 3–5 high-signal entries.

The summarisation extracts:

    1. Session title (first user prompt, capped to 80 chars)
    2. The user's stated intent (first user turn verbatim)
    3. Up to ``max_facts`` durable facts extracted from user turns
       (delegates to the configured ``Reflector`` — heuristic by default
       and pluggable to an LLM-backed one)
    4. The session outcome — last assistant turn verbatim (truncated)

If the conversation carries no extractable fact (pure chitchat), only
the title is written so the loop memory still records *that the session
happened* without bloating the store with low-signal noise.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..backends.embedding import BaseEmbedder, IdentityEmbedder
from ..memory.types import MemoryItem
from ..storage.sqlite_store import MemoryStore, StoredMemory, StoredSession
from ..privacy import redact_text, strip_private_spans, RedactionSummary
from .loader import IngestedSession, IngestedTurn

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    session: StoredSession
    summary_items: list[StoredMemory]
    facts_count: int
    outcome_written: bool
    dropped: list[DroppedItem] = None  # items the WriteGuard rejected


@dataclass
class DroppedItem:
    """A pre-write check that vetoed a candidate memory."""
    kind: str           # "duplicate" | "too_short" | "too_long" | "low_signal"
    text_preview: str   # first 80 chars
    reason: str
    matched_id: str | None = None  # when kind=="duplicate"
    matched_score: float = 0.0


class WriteGuard:
    """Pre-write filters run before a candidate memory hits the store.

    Goal: stop the long-term store from drowning in chat noise / duplicates
    that the LLM-driven consolidator would have to clean up later anyway.
    Each check returns either ``None`` (accept) or a ``DroppedItem``
    describing the rejection.

    Thresholds are deliberately conservative — the consolidator is still
    the source of truth for *semantic* filtering; the guard only blocks
    patterns we know are pure waste (whitespace, near-duplicates).
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        min_chars: int = 25,
        max_chars: int = 1200,
        duplicate_threshold: float = 0.85,
        duplicate_window: int = 600,
    ) -> None:
        self.store = store
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.duplicate_threshold = duplicate_threshold
        self.duplicate_window = duplicate_window

    def check(self, *, kind: str, text: str, importance: float, source: str | None) -> DroppedItem | None:
        text = (text or "").strip()
        if not text:
            return DroppedItem(kind="too_short", text_preview="", reason="empty text")
        if len(text) < self.min_chars:
            return DroppedItem(
                kind="too_short",
                text_preview=text[:80],
                reason=f"text shorter than {self.min_chars} chars",
            )
        # Low-signal short episode with very low importance
        if kind == "episode" and importance < 0.4 and len(text) < 60:
            return DroppedItem(
                kind="low_signal",
                text_preview=text[:80],
                reason="episode with importance<0.4 and <60 chars",
            )
        # Length cap
        if len(text) > self.max_chars:
            return DroppedItem(
                kind="too_long",
                text_preview=text[:80],
                reason=f"text longer than {self.max_chars} chars — wikify first",
            )
        # Duplicate check via cheap shingle overlap
        dup = self._find_duplicate(text, source)
        if dup:
            return DroppedItem(
                kind="duplicate",
                text_preview=text[:80],
                reason=f"≥{int(self.duplicate_threshold*100)}% shingle overlap with existing memory",
                matched_id=dup["id"],
                matched_score=dup["score"],
            )
        return None

    def _shingles(self, text: str, n: int = 5) -> set[str]:
        text = re.sub(r"\s+", " ", text.lower()).strip()
        if len(text) < n:
            return {text}
        return {text[i:i+n] for i in range(0, len(text) - n + 1)}

    def _find_duplicate(self, text: str, source: str | None) -> dict | None:
        """Cheap near-duplicate detector: 5-char shingle Jaccard vs the
        last ``duplicate_window`` memories of the same source. We use
        Python sets rather than SQLite FTS because most memory tables
        are < 10k rows and this runs once per write — keeping it
        in-process avoids a roundtrip.
        """
        try:
            recent = self.store.list_memories(limit=self.duplicate_window, source=source)
        except Exception:
            return None
        if not recent:
            return None
        cand = self._shingles(text)
        if not cand:
            return None
        best = None
        for m in recent:
            existing = self._shingles(m.text or "")
            if not existing:
                continue
            inter = len(cand & existing)
            union = len(cand | existing)
            if union == 0:
                continue
            score = inter / union
            if score >= self.duplicate_threshold:
                if best is None or score > best["score"]:
                    best = {"id": m.id, "score": score}
        return best


ExtractFn = Callable[[list[IngestedTurn]], list[MemoryItem]]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class MemoryPipeline:
    """Pipeline: ``IngestedSession`` → ``(StoredSession, [summary items])``.

    Compared to v0.2 the per-turn ``MemoryItem`` rows are gone. The
    SQLite store's ``memories`` table is unchanged; we just feed it
    fewer, denser rows. Set ``max_facts=0`` to keep only the title + outcome.
    """

    def __init__(
        self,
        store: MemoryStore,
        embedder: BaseEmbedder | None = None,
        extractor: ExtractFn | None = None,
        half_life_days: float = 30.0,
        max_facts: int = 3,
        max_chars: int = 480,
        guard: WriteGuard | None = None,
        redact_enabled: bool | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder or IdentityEmbedder()
        self.half_life_days = half_life_days
        self.extractor = extractor or self._default_extractor
        self.max_facts = max(0, max_facts)
        self.max_chars = max_chars
        self.guard = guard or WriteGuard(store)
        # Privacy redaction: ON by default. ``LOOP_MEMORY_REDACT=0``
        # disables it for debugging. Explicit ``redact_enabled=`` arg
        # in the constructor overrides the env var. The summary
        # counter is per-pipeline-run and is read by the /admin
        # endpoints for live observability.
        import os as _os
        if redact_enabled is None:
            redact_enabled = _os.environ.get("LOOP_MEMORY_REDACT", "1") != "0"
        self.redact_enabled = bool(redact_enabled)
        self.redact_summary = RedactionSummary()
        # Lazy-initialised on first drop so the import path stays optional
        # for environments that don't pull sqlite_store.
        self._drop_store = None

    def _get_drop_store(self):
        if self._drop_store is None:
            try:
                from ..storage.sqlite_store import WriteGuardDropStore
                self._drop_store = WriteGuardDropStore(self.store)
            except Exception:
                self._drop_store = False  # sentinel: already attempted
        return self._drop_store or None

    # --- public -----------------------------------------------------------

    def run(self, session: IngestedSession) -> IngestResult:
        if not session.turns:
            raise ValueError("session has no turns")

        # Always recompute a clean title from the user turns; ignore
        # any system-preamble metadata the loader may have surfaced.
        clean_title = self._title_text(session) or f"{session.source} session"
        sis = self.store.upsert_session(
            source=session.source,
            external_id=session.external_id,
            title=clean_title,
            started_at=session.started_at,
            ended_at=session.ended_at,
            message_count=session.message_count,
            metadata={"ingested_at": time.time(), "kind": "summary"},
        )
        summary_items: list[StoredMemory] = []

        # Attach holder for the guard to record drops into
        result_holder = IngestResult(
            session=sis, summary_items=summary_items,
            facts_count=0, outcome_written=False, dropped=[],
        )
        self._in_flight_result = result_holder
        try:
            title_text = self._title_text(session)
            if title_text:
                m = self._write(
                    kind="episode",
                    text=f"[{session.source}] {title_text}",
                    importance=0.55,
                    session_id=sis.id,
                    created_at=session.started_at,
                    tags=[session.source, "title"],
                    source=session.source,
                    skip_guard=True,
                )
                if m is not None:
                    summary_items.append(m)

            first_user = self._first_user(session)
            if first_user and _norm(first_user) != _norm(title_text):
                m = self._write(
                    kind="fact",
                    text=f"User intent: {_norm(first_user)[:self.max_chars]}",
                    importance=0.7,
                    session_id=sis.id,
                    created_at=session.started_at,
                    tags=[session.source, "intent"],
                    source=session.source,
                )
                if m is not None:
                    summary_items.append(m)

            facts = self._safe_extract(session.turns)
            facts = facts[: self.max_facts] if self.max_facts else []
            for f in facts:
                m = self._write(
                    kind="fact",
                    text=_norm(f.text)[: self.max_chars],
                    importance=max(0.3, min(1.0, f.importance)),
                    session_id=sis.id,
                    created_at=f.created_at or session.ended_at or session.started_at,
                    tags=list({*f.tags, session.source, "extracted"}),
                    source=session.source,
                )
                if m is not None:
                    summary_items.append(m)
        finally:
            self._in_flight_result = None
        result_holder.facts_count = len(facts)
        result_holder.summary_items = summary_items

        outcome_written = False
        last_assistant = self._last_assistant(session)
        # Re-attach the same holder so the outcome write can record drops
        self._in_flight_result = result_holder
        try:
            if last_assistant:
                m = self._write(
                    kind="episode",
                    text=f"Outcome: {_norm(last_assistant)[:self.max_chars]}",
                    importance=0.55,
                    session_id=sis.id,
                    created_at=session.ended_at or time.time(),
                    tags=[session.source, "outcome"],
                    source=session.source,
                    skip_guard=True,
                )
                if m is not None:
                    summary_items.append(m)
                    outcome_written = True
        finally:
            self._in_flight_result = None
        result_holder.outcome_written = outcome_written
        result_holder.summary_items = summary_items
        result_holder.facts_count = len(facts)
        return result_holder

    # --- helpers ----------------------------------------------------------

    def _write(
        self,
        *,
        kind: str,
        text: str,
        importance: float,
        session_id: str,
        created_at: float,
        tags: list,
        source: str,
        skip_guard: bool = False,
    ) -> StoredMemory | None:
        # Pre-write guard. Structural writes (title / outcome / intent) are
        # always allowed through so the long-term store still records *that*
        # a session happened, even if the body is short.
        if self.guard is not None and not skip_guard:
            drop = self.guard.check(kind=kind, text=text, importance=importance, source=source)
            if drop is not None:
                log.info(
                    "WriteGuard rejected %s (%s): %s — %r",
                    drop.kind, source, drop.reason, drop.text_preview,
                )
                # Stash the drop on the in-flight result if available
                res = getattr(self, "_in_flight_result", None)
                if res is not None and res.dropped is not None:
                    res.dropped.append(drop)
                ds = self._get_drop_store()
                if ds is not None:
                    try:
                        ds.record(source=source, kind=drop.kind,
                                  text_preview=drop.text_preview,
                                  matched_id=drop.matched_id,
                                  matched_score=drop.matched_score)
                    except Exception:
                        pass
                return None
        # Privacy redaction. Runs before embedding so the vector
        # stored in SQLite never sees a leaked secret. We also
        # honour ``<private>...</private>`` user markers — the body
        # becomes a single ``[PRIVATE:redacted]`` token.
        if self.redact_enabled:
            text = strip_private_spans(text)
            if text.strip():
                text = redact_text(text, summary=self.redact_summary)
        emb = None
        if self.embedder.dim:
            try:
                emb = self.embedder.embed_query(text)
            except Exception:
                emb = None
        return self.store.upsert_memory(
            kind=kind,
            text=text,
            importance=importance,
            source=source,
            session_id=session_id,
            created_at=created_at,
            tags=tags,
            embedding=emb,
        )

    def _safe_extract(self, turns) -> list[MemoryItem]:
        try:
            return self.extractor(list(turns)) or []
        except Exception:
            log.exception("extractor failed; continuing without facts")
            return []

    _PREAMBLE_RE = re.compile(r"<\s*(environment_context|system-prompt|instructions)[^>]*>", re.I)

    def _first_user(self, session: IngestedSession) -> str | None:
        """First *substantive* user turn — skip empty / XML preambles."""
        for t in session.turns:
            if t.role != "user":
                continue
            txt = _norm(t.text)
            if not txt:
                continue
            if len(txt) < 6:
                continue
            if self._PREAMBLE_RE.match(txt):
                continue
            return txt
        # fall back to whatever came first
        for t in session.turns:
            if t.role == "user" and t.text:
                return _norm(t.text)
        return None

    def _last_assistant(self, session: IngestedSession) -> str | None:
        for t in reversed(session.turns):
            if t.role == "assistant" and t.text:
                txt = _norm(t.text)
                if txt:
                    return txt
        return None

    def _title_text(self, session: IngestedSession) -> str:
        if session.title and not self._PREAMBLE_RE.match(_norm(session.title)):
            return _norm(session.title)[:80]
        first = self._first_user(session)
        if first:
            return first[:80]
        return f"{session.source} session"

    # --- default extractor ----------------------------------------------

    def _default_extractor(self, turns: list[IngestedTurn]) -> list[MemoryItem]:
        """Pull *durable*, *specific* facts from user turns.

        Heuristic: skip turns shorter than 12 chars or longer than 240,
        skip greetings, de-duplicate by fingerprint, cap at 6 candidates.
        Replace with an LLM-backed reflector via the constructor for
        higher quality.
        """
        GREETING = re.compile(r"^(hi|hey|hello|thanks|thank you|ok|okay|好的|是|对|嗯)[.! ]*$", re.I)
        candidates: list[MemoryItem] = []
        seen: set[str] = set()
        for turn in turns:
            if turn.role != "user":
                continue
            text = _norm(turn.text)
            if len(text) < 12 or len(text) > 240:
                continue
            if GREETING.match(text):
                continue
            fp = text.lower()[:60]
            if fp in seen:
                continue
            seen.add(fp)
            candidates.append(MemoryItem(
                text=f"User said: {text}",
                importance=0.55,
                kind="fact",
                created_at=turn.created_at or time.time(),
                tags=["user-quote"],
            ))
            if len(candidates) >= 6:
                break
        return candidates

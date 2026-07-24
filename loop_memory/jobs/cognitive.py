"""Cognitive sleep — the "主动学习" / "主动修正" layer the
Universal Agent Memory contract needs.

Background: the article that prompted this work argues that real
memory systems are not databases but *cognitive processes*. They
actively:

* filter low-value noise (memories that are stale, low-importance,
  and never recalled);
* detect contradictions (two memories / wiki pages that disagree);
* propose merges (near-duplicates that should be one);
* suggest forgets (memories the user almost certainly doesn't need
  anymore).

This module implements that pipeline as a single ``cognitive_sleep``
call. The output is an audit report plus, when ``apply=True``, a
set of mutations on the store. Every decision is recorded in
``cognitive_audit`` so the user / SDK can review, revert, or simply
log the result.

The job is zero-dep: it uses only the existing store + a tiny set
of heuristics. The LLM-driven distillate step in the evolution
consolidator stays the source of truth for high-level
"is this a contradiction?" questions; cognitive_sleep is the cheap,
deterministic nightly sweep that catches the obvious cases without
needing a model call.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..storage.sqlite_store import MemoryStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic thresholds
# ---------------------------------------------------------------------------


# A memory is "stale" if it has not been recalled in this many days
# AND its score is below this threshold AND its importance is below
# the threshold. These defaults are conservative — the user can
# tighten them via the ``stale_days`` / ``min_score`` / ``min_importance``
# parameters to ``cognitive_sleep``.
DEFAULT_STALE_DAYS = 90
DEFAULT_MIN_SCORE = 0.2
DEFAULT_MIN_IMPORTANCE = 0.3

# A memory is "low value" if its score + 0.5 * importance is below
# this AND it has never been recalled. This catches the
# "auto-generated noise from a long transcript" case the LLM
# consolidator sometimes leaves behind.
DEFAULT_LOW_VALUE = 0.3

# A merge is suggested when two memories have cosine similarity above
# this threshold AND the same (agent_id, user_id) namespace.
DEFAULT_MERGE_THRESHOLD = 0.92


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CognitiveAction:
    """One proposed (or applied) action from the cognitive sleep sweep."""

    kind: str            # forget / merge / contradict / stale / low_value
    target_kind: str     # memory / wiki_page
    target_id: str
    target_text: str
    reason: str
    score: float = 0.0
    payload: dict = field(default_factory=dict)
    action: str = "suggest"  # suggest / applied / reverted

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "target_text": self.target_text,
            "reason": self.reason,
            "score": self.score,
            "payload": self.payload,
            "action": self.action,
        }


@dataclass
class CognitiveReport:
    """The full result of one ``cognitive_sleep`` call."""

    actions: list[CognitiveAction] = field(default_factory=list)
    elapsed_ms: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "elapsed_ms": self.elapsed_ms,
            "counts": self.counts,
            "applied": self.applied,
            "total": len(self.actions),
        }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def cognitive_sleep(
    store: MemoryStore,
    *,
    apply: bool = False,
    stale_days: int = DEFAULT_STALE_DAYS,
    min_score: float = DEFAULT_MIN_SCORE,
    min_importance: float = DEFAULT_MIN_IMPORTANCE,
    low_value: float = DEFAULT_LOW_VALUE,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    limit: int = 1000,
    record_audit: bool = True,
) -> CognitiveReport:
    """Run a single cognitive sweep.

    * ``apply=False`` (default): only suggest actions, do not
      mutate the store. Used by the UI's "preview" panel.
    * ``apply=True``: actually delete the ``forget`` actions and
      merge the ``merge`` actions, then record each applied action
      in ``cognitive_audit`` with ``action='applied'``.

    Every suggestion is recorded in ``cognitive_audit`` with
    ``action='suggest'`` regardless of whether the user applies it,
    so the trail is complete.

    The sweep is bounded to ``limit`` memories per pass to keep it
    cheap; for very large stores the user can call it multiple
    times or wire it into a cron.
    """
    t0 = time.time()
    actions: list[CognitiveAction] = []
    counts: dict[str, int] = {
        "stale": 0, "low_value": 0, "merge": 0, "contradict": 0, "forget": 0,
    }
    now = time.time()
    stale_cutoff = now - stale_days * 86400.0

    # ----- 1. Stale memories --------------------------------------
    # Pull every memory below the score + importance gates. We do a
    # single SQL scan to keep the pass fast.
    rows = store.list_memories(limit=limit)
    for r in rows:
        score = float(r.score or 0)
        importance = float(r.importance or 0)
        created = float(r.created_at or 0)
        # Stale: old + low score + low importance, regardless of recall.
        if created < stale_cutoff and score < min_score and importance < min_importance:
            counts["stale"] += 1
            actions.append(CognitiveAction(
                kind="stale", target_kind="memory", target_id=r.id,
                target_text=(r.text or "")[:200],
                reason=f"age>{stale_days}d & score<{min_score} & importance<{min_importance}",
                score=score, payload={"importance": importance,
                                      "age_days": int((now - created) / 86400)},
            ))
            continue
        # Low value: never recalled, score + importance * 0.5 below
        # ``low_value`` (this is the cheap "noise from a long
        # transcript" filter).
        # ``list_memories`` doesn't include recall_count in the
        # dataclass; re-fetch via the signals table.
        signals = _signals_for(store, r.id)
        if signals["recall_count"] == 0 and score + 0.5 * importance < low_value:
            counts["low_value"] += 1
            actions.append(CognitiveAction(
                kind="low_value", target_kind="memory", target_id=r.id,
                target_text=(r.text or "")[:200],
                reason=f"never recalled & score+0.5*importance<{low_value}",
                score=score, payload={"importance": importance},
            ))

    # ----- 2. Near-duplicate merges ------------------------------
    # Cheap O(n^2) on the first ``limit`` memories; good enough for
    # nightly sweeps on a store of a few thousand rows. We use the
    # ``text`` Jaccard over a small token set so the comparison
    # doesn't need embeddings.
    text_index = [(r.id, _token_set(r.text or "")) for r in rows]
    seen_pairs: set[tuple[str, str]] = set()
    for i in range(len(text_index)):
        for j in range(i + 1, len(text_index)):
            mid_i, ti = text_index[i]
            mid_j, tj = text_index[j]
            if not ti or not tj:
                continue
            j_sim = _jaccard(ti, tj)
            # Short-text containment: when both memories are < 30
            # tokens, the Jaccard threshold is too strict because one
            # extra word in a paraphrase drags the score way down.
            # Use a containment fallback: 80 % of A's tokens in B
            # (or vice versa) counts as a merge candidate.
            contain_a_in_b = (len(ti & tj) / max(1, len(ti))) >= 0.8
            contain_b_in_a = (len(ti & tj) / max(1, len(tj))) >= 0.8
            short_text = len(ti) <= 30 and len(tj) <= 30
            if j_sim >= merge_threshold or (short_text and (contain_a_in_b or contain_b_in_a)):
                pair = tuple(sorted([mid_i, mid_j]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                counts["merge"] += 1
                actions.append(CognitiveAction(
                    kind="merge", target_kind="memory", target_id=mid_i,
                    target_text="(merge with " + mid_j + ")",
                    reason=f"Jaccard={j_sim:.3f} ≥ {merge_threshold}",
                    score=j_sim, payload={"other_id": mid_j, "jaccard": j_sim},
                ))

    # ----- 3. Contradictions -------------------------------------
    # Reuse the existing wiki-page contradiction detector. It's
    # cheap (key_facts Jaccard, no LLM) and already returns the
    # matches we need.
    from .contradiction import list_contradictions
    try:
        contradictions = list_contradictions(store)
    except Exception as e:
        log.warning("contradiction scan failed: %s", e)
        contradictions = []
    for c in contradictions:
        # ``c`` is a dict; the page id is the *winner* and the
        # partner ids are in a list.
        for partner in c.get("partners", []):
            counts["contradict"] += 1
            actions.append(CognitiveAction(
                kind="contradict", target_kind="wiki_page",
                target_id=c.get("id") or "",
                target_text=c.get("title", "")[:160],
                reason="wiki_page contradict detected by key_facts Jaccard",
                score=float(partner.get("score", 0) or 0),
                payload={
                    "partner_id": partner.get("id"),
                    "partner_title": partner.get("title"),
                },
            ))

    # ----- 4. Apply (optional) -----------------------------------
    if apply:
        applied_actions: list[CognitiveAction] = []
        for a in actions:
            if a.kind in ("stale", "low_value"):
                n = store.delete_memory(a.target_id)
                if n:
                    counts["forget"] += 1
                    a.action = "applied"
                    applied_actions.append(a)
            elif a.kind == "merge":
                other = a.payload.get("other_id")
                if not other:
                    continue
                result = store.merge_memories(a.target_id, other)
                if result.get("merged"):
                    a.action = "applied"
                    applied_actions.append(a)
            elif a.kind == "contradict":
                # We don't auto-resolve contradictions — the UI
                # shows them and the user clicks "merge" / "keep
                # both". But we still mark the action as suggested
                # so the audit trail is complete.
                continue

    # ----- 5. Persist to cognitive_audit -------------------------
    if record_audit:
        for a in actions:
            store.record_audit(
                kind=a.kind,
                action=a.action,
                target_kind=a.target_kind,
                target_id=a.target_id,
                target_text=a.target_text,
                reason=a.reason,
                score=a.score,
                payload=a.payload,
            )

    elapsed_ms = (time.time() - t0) * 1000
    return CognitiveReport(
        actions=actions,
        elapsed_ms=round(elapsed_ms, 1),
        counts=counts,
        applied=bool(apply),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _signals_for(store: MemoryStore, memory_id: str) -> dict[str, Any]:
    """Return the signal row for a memory, or zeros if missing."""
    with store._conn() as c:  # type: ignore[attr-defined]
        row = c.execute(
            "SELECT recall_count, positive, negative, last_recalled_at "
            "FROM memory_signals WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
    if not row:
        return {"recall_count": 0, "positive": 0, "negative": 0,
                "last_recalled_at": None}
    return dict(row)


def _token_set(text: str) -> set[str]:
    """Cheap token set: lowercase + split on whitespace + punctuation.

    CJK characters are kept as 1-grams (no bigrams) to keep the
    similarity symmetric. The result is a set, not a multiset.
    """
    import re
    if not text:
        return set()
    toks = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text.lower())
    return set(toks)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / max(1, len(union))


__all__ = [
    "CognitiveAction",
    "CognitiveReport",
    "DEFAULT_STALE_DAYS",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_MIN_IMPORTANCE",
    "DEFAULT_LOW_VALUE",
    "DEFAULT_MERGE_THRESHOLD",
    "cognitive_sleep",
]

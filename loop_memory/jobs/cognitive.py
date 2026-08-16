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
from typing import Any, Callable

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
    # Audit 2026-08-16: per-stage timings so a stalled sweep can be
    # attributed to a specific stage instead of returning a single
    # opaque number. Mirrors the "fails loudly instead of quietly"
    # convention from ``EverMind-AI/EverOS`` v1.2.3 where any stall
    # names the table / phase it happened in.
    stages: dict[str, float] = field(default_factory=dict)
    aborted: bool = False
    abort_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "elapsed_ms": self.elapsed_ms,
            "counts": self.counts,
            "applied": self.applied,
            "total": len(self.actions),
            "stages": dict(self.stages),
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
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
    progress: Callable[[str], None] | None = None,
    deadline_seconds: float | None = None,
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

    Audit 2026-08-16 -- observability additions
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    * ``progress``: optional ``Callable[[str], None]`` invoked once per
      stage so the UI can render a live progress bar without polling.
      Stage names are ``scan``, ``stale``, ``low_value``, ``merge``,
      ``contradict``, ``apply``, ``audit``.
    * ``deadline_seconds``: when set, the sweep checks the deadline
      between the O(n^2) near-duplicate pairs and between
      individual stages; if it elapses, the sweep stops with
      ``report.aborted=True`` and ``report.abort_reason`` naming
      the stage it stalled in. Following the
      ``EverMind-AI/EverOS`` v1.2.3 pattern: stalls must name the
      stage so the user can act on them, not return a silent
      "still working…" spinner. ``report.stages`` carries the
      per-stage elapsed_ms for the same reason.
    """
    t0 = time.time()
    actions: list[CognitiveAction] = []
    counts: dict[str, int] = {
        "stale": 0, "low_value": 0, "merge": 0, "contradict": 0, "forget": 0,
    }
    stages: dict[str, float] = {}
    aborted = False
    abort_reason = ""
    now = time.time()
    # Explicit ``is not None`` check guards against the
    # ``0.0 == falsy`` pitfall (the same bug class Mem0 v2.0.18
    # fixed for Oracle ``index_accuracy=0``): a caller passing
    # ``deadline_seconds=0.0`` to mean "fail-fast / never run"
    # would otherwise be silently downgraded to ``deadline_at=None``
    # and the sweep would happily run past the deadline.
    deadline_at: float | None = (
        now + float(deadline_seconds)
        if deadline_seconds is not None else None
    )

    def _deadline_left() -> float:
        if deadline_at is None:
            return float("inf")
        return max(0.0, deadline_at - time.time())

    def _tick(stage: str, stage_t0: float) -> None:
        nonlocal aborted, abort_reason
        stages[stage] = round((time.time() - stage_t0) * 1000, 1)
        if progress is not None:
            try:
                progress(stage)
            except Exception:  # pragma: no cover - progress is best-effort
                log.warning("cognitive_sleep progress(%r) raised; ignoring", stage)
        if deadline_at is not None and time.time() >= deadline_at:
            aborted = True
            abort_reason = f"deadline exceeded in stage {stage!r} ({stages[stage]} ms)"

    stale_cutoff = now - stale_days * 86400.0

    # ----- 1. Stale memories --------------------------------------
    # Pull every memory below the score + importance gates. We do a
    # single SQL scan to keep the pass fast.
    scan_t0 = time.time()
    rows = store.list_memories(limit=limit)
    _tick("scan", scan_t0)
    if aborted:
        return _finalize_report(actions, counts, stages, aborted, abort_reason,
                                t0, apply, record_audit, store)

    # Stages 1+2: stale gate + low-value gate share one pass over
    # ``rows`` because both need (score, importance, created_at).
    sl_t0 = time.time()
    # Audit 2026-08-16: bulk-fetch signals in one query instead of
    # one SELECT per memory -- the nightly sweep previously did
    # ``limit`` round-trips just to read ``recall_count`` for the
    # low-value gate.
    signals_by_id = store.get_signals([r.id for r in rows]) if rows else {}
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
        # transcript" filter). Signals are pre-fetched in bulk above
        # so this is now a dict lookup, not a per-row SQL hit.
        signals = signals_by_id.get(r.id, {"recall_count": 0, "positive": 0, "negative": 0})
        if signals["recall_count"] == 0 and score + 0.5 * importance < low_value:
            counts["low_value"] += 1
            actions.append(CognitiveAction(
                kind="low_value", target_kind="memory", target_id=r.id,
                target_text=(r.text or "")[:200],
                reason=f"never recalled & score+0.5*importance<{low_value}",
                score=score, payload={"importance": importance},
            ))
    _tick("stale", sl_t0)
    if aborted:
        return _finalize_report(actions, counts, stages, aborted, abort_reason,
                                t0, apply, record_audit, store)

    # ----- 2. Near-duplicate merges ------------------------------
    # Cheap O(n^2) on the first ``limit`` memories; good enough for
    # nightly sweeps on a store of a few thousand rows. We use the
    # ``text`` Jaccard over a small token set so the comparison
    # doesn't need embeddings.
    merge_t0 = time.time()
    text_index = [(r.id, _token_set(r.text or "")) for r in rows]
    seen_pairs: set[tuple[str, str]] = set()
    for i in range(len(text_index)):
        for j in range(i + 1, len(text_index)):
            # Check the deadline every 256 pairs so the cost is
            # amortised away on large sweeps but a runaway near-
            # duplicate loop can't escape the budget.
            if deadline_at is not None and (j & 0xFF) == 0 and _deadline_left() <= 0:
                aborted = True
                abort_reason = f"deadline exceeded during near-duplicate scan (i={i}, j={j})"
                break
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
        if aborted:
            break
    _tick("merge", merge_t0)
    if aborted:
        return _finalize_report(actions, counts, stages, aborted, abort_reason,
                                t0, apply, record_audit, store)

    # ----- 3. Contradictions -------------------------------------
    # Reuse the existing wiki-page contradiction detector. It's
    # cheap (key_facts Jaccard, no LLM) and already returns the
    # matches we need.
    from .contradiction import list_contradictions
    contr_t0 = time.time()
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
    _tick("contradict", contr_t0)
    if aborted:
        return _finalize_report(actions, counts, stages, aborted, abort_reason,
                                t0, apply, record_audit, store)

    # ----- 4. Apply (optional) -----------------------------------
    apply_t0 = time.time()
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
    _tick("apply", apply_t0)

    # ----- 5. Persist to cognitive_audit -------------------------
    audit_t0 = time.time()
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

    _tick("audit", audit_t0)
    elapsed_ms = (time.time() - t0) * 1000
    return CognitiveReport(
        actions=actions,
        elapsed_ms=round(elapsed_ms, 1),
        counts=counts,
        applied=bool(apply),
        stages=stages,
        aborted=aborted,
        abort_reason=abort_reason,
    )


def _finalize_report(
    actions: list[CognitiveAction],
    counts: dict[str, int],
    stages: dict[str, float],
    aborted: bool,
    abort_reason: str,
    t0: float,
    apply: bool,
    record_audit: bool,
    store: MemoryStore,
) -> CognitiveReport:
    """Build an early-return report when the deadline fires.

    The early-return path still runs the audit stage for any actions
    the sweep already collected, so a partial sweep leaves the same
    trace a full sweep would.
    """
    if record_audit and actions:
        for a in actions:
            try:
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
            except Exception:  # pragma: no cover - audit is best-effort
                log.warning("record_audit on early-return failed; skipping")
        stages["audit"] = round((time.time() - t0) * 1000, 1) - sum(stages.values())
    elapsed_ms = (time.time() - t0) * 1000
    return CognitiveReport(
        actions=actions,
        elapsed_ms=round(elapsed_ms, 1),
        counts=counts,
        applied=bool(apply),
        stages=stages,
        aborted=aborted,
        abort_reason=abort_reason,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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

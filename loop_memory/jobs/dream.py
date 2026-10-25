"""Dream — the sleep-time consolidation pass.

Audit 2026-10-25 (loop-memory 0.4.16).

Background
----------
``cognitive_sleep`` (added 2026-08) already sweeps a store for stale
rows, low-value rows, near-duplicates and wiki contradictions. What it
could not do was *forget* without destroying: applying a ``stale`` or
``low_value`` action calls ``store.delete_memory()``, which removes
the row outright. A hard delete is the wrong tool for a memory store:

* history is gone -- ``recall_as_of()`` can no longer answer "what did
  we believe last month?";
* the audit trail points at a row that no longer exists;
* a single mis-scored row silently vanishes.

Mem0 v2 calls its offline consolidation pass the "Dream" (mem0ai/mem0
v2.2.x, released 2026-09-25): a background job that consolidates,
resolves contradictions and distills. The academic literature agrees on
the shape -- "In Defense of Forgotten: TTL-aware Memory for
Long-Horizon Agents" (arXiv 2605.27366) argues that eviction should
be *utility-aware* rather than purely age-based, and Mem0's temporal
post (2026-05) makes the same point: a fact's validity window, not
its age, decides whether it is still true.

What this job does
------------------
``Dreamer.run()`` is a deterministic, zero-dependency, three-stage
pass:

1. **Decay.** Rescore every row with the existing exponential
   half-life model so recency-sensitive ordering stays honest. A dry
   run computes the new scores and reports how many rows *would*
   change, but writes nothing (audit 2026-10-25, fix).

2. **Utility-aware selective forgetting.** A row is a *forgetting
   candidate* once it is past its own TTL (or past the sweep's
   ``ttl_days`` horizon when the row has no TTL of its own). Candidates
   are then judged on **utility** -- a blend of declared importance,
   how often the row has actually been recalled, and how recent it is.
   Low-utility candidates are **soft-expired**: ``invalid_at`` is
   stamped, the row stays, the history stays. High-utility candidates
   are *protected* and reported as such.

3. **Near-duplicate consolidation.** Rows that say the same thing are
   merged through ``store.merge_memories()``, which appends the loser
   into the winner and writes a ``superseded_by`` pointer instead of
   deleting. The result is a supersession chain that
   ``recall(latest_only=True)`` can collapse to one answer.

Everything is opt-in: ``apply=False`` (the default) produces a dry-run
report and **changes no memory data** -- no expiry, no supersession,
no rescore. The only write a dry run performs is one
``cognitive_audit`` row per proposed action (with ``action="suggest"``
instead of ``"applied"``), because that row *is* the reviewable plan:
it is what lets the dashboard show what a real run would do before
you commit to it. Use ``record_audit=False`` to suppress even that.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..storage.sqlite_store import MemoryStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# How long a row without an explicit TTL survives before it becomes a
# forgetting candidate. 90 days matches ``cognitive_sleep``'s
# ``DEFAULT_STALE_DAYS`` so the two sweeps agree on what "old" means.
DEFAULT_TTL_DAYS = 90.0

# Half-life used for the recency term of the utility blend and for the
# decay stage.
DEFAULT_HALF_LIFE_DAYS = 30.0

# A row whose utility falls below this threshold is expired (when it
# is also past its deadline). The threshold is deliberately low: the
# dream pass should only forget rows that are *both* old and useless.
DEFAULT_UTILITY_THRESHOLD = 0.30

# Token-Jaccard similarity above which two rows are considered the
# same statement. 0.92 is tight on purpose -- a loose threshold would
# merge "the deploy failed" with "the deploy failed on staging".
DEFAULT_SIMILARITY = 0.92

# Upper bound on the number of rows inspected in one pass. The
# near-duplicate stage is O(n^2) inside each kind bucket, so the
# candidate set has to be bounded for a large store.
DEFAULT_LIMIT = 2000

# How many recalls make a row "fully used" in the utility blend.
_USAGE_SATURATION = 10.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DreamAction:
    """One decision the dream pass made (or would make)."""

    kind: str            # expire / keep / supersede
    target_id: str
    target_text: str
    reason: str
    utility: float = 0.0
    score: float = 0.0
    payload: dict = field(default_factory=dict)
    action: str = "suggest"   # suggest / applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "target_text": self.target_text,
            "reason": self.reason,
            "utility": round(self.utility, 4),
            "score": round(self.score, 4),
            "payload": self.payload,
            "action": self.action,
        }


@dataclass
class DreamReport:
    """The full result of one ``Dreamer.run()`` call."""

    actions: list[DreamAction] = field(default_factory=list)
    considered: int = 0
    rescored: int = 0
    expired: int = 0
    kept: int = 0
    superseded: int = 0
    applied: bool = False
    elapsed_ms: float = 0.0
    stages: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "considered": self.considered,
            "rescored": self.rescored,
            "expired": self.expired,
            "kept": self.kept,
            "superseded": self.superseded,
            "applied": self.applied,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "stages": dict(self.stages),
            "total": len(self.actions),
        }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def memory_utility(
    *,
    importance: float,
    recall_count: int,
    created_at: float,
    now: float,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Score how much a memory is still worth keeping (0.0 - 1.0).

    Three terms, in descending weight:

    * **importance** (0.45) -- what the writer declared.
    * **usage** (0.30) -- how often the memory actually surfaced.
      This is the term that protects a rarely-stated-but-constantly-
      recalled fact, which is exactly the case a pure age-based TTL
      gets wrong (arXiv 2605.27366).
    * **recency** (0.25) -- exponential decay with the same half-life
      the scorer uses, so the utility term and the stored score agree.

    The result is deliberately *not* the stored ``score``: the score
    is a ranking signal, the utility is a retention signal.
    """
    half_life_seconds = max(1e-9, float(half_life_days) * 86400.0)
    age = max(0.0, float(now) - float(created_at))
    recency = 0.5 ** (age / half_life_seconds)
    usage = min(1.0, max(0, int(recall_count or 0)) / _USAGE_SATURATION)
    value = (
        0.45 * max(0.0, min(1.0, float(importance or 0.0)))
        + 0.30 * usage
        + 0.25 * recency
    )
    return max(0.0, min(1.0, value))


def _token_set(text: str) -> set[str]:
    """Token set used for the near-duplicate stage.

    Same shape as ``cognitive_sleep._token_set``: ASCII words plus
    CJK unigrams, lower-cased. The set is intentionally coarse -- the
    similarity threshold does the fine work.
    """
    import re

    if not text:
        return set()
    return set(re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


class Dreamer:
    """Deterministic sleep-time consolidation for a :class:`MemoryStore`.

    Parameters mirror the CLI / HTTP flags so every surface exposes the
    same knobs:

    * ``half_life_days`` -- decay + recency term of the utility blend.
    * ``ttl_days`` -- how long a row without its own TTL survives.
    * ``utility_threshold`` -- below this a past-deadline row is
      expired; at or above it the row is protected.
    * ``similarity`` -- token-Jaccard threshold for near-duplicates.
    * ``limit`` -- upper bound on the rows inspected in one pass.
    * ``apply`` -- when False (default) nothing is written.
    * ``record_audit`` -- when True every decision lands in
      ``cognitive_audit`` so the dashboard / CLI can show and revert.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        ttl_days: float = DEFAULT_TTL_DAYS,
        utility_threshold: float = DEFAULT_UTILITY_THRESHOLD,
        similarity: float = DEFAULT_SIMILARITY,
        limit: int = DEFAULT_LIMIT,
        apply: bool = False,
        record_audit: bool = True,
    ) -> None:
        self.store = store
        self.half_life_days = float(half_life_days)
        self.ttl_days = float(ttl_days)
        self.utility_threshold = float(utility_threshold)
        self.similarity = float(similarity)
        self.limit = int(limit)
        self.apply = bool(apply)
        self.record_audit = bool(record_audit)

    # -- public API ----------------------------------------------------

    def run(self) -> DreamReport:
        t0 = time.time()
        report = DreamReport(applied=self.apply)
        now = time.time()

        self._stage_decay(report)
        candidates = self._candidates(now)
        self._stage_forget(report, now, candidates)
        self._stage_consolidate(report, now, candidates)

        if self.record_audit:
            self._stage_audit(report)

        report.elapsed_ms = (time.time() - t0) * 1000
        return report

    # -- stages --------------------------------------------------------

    def _stage_decay(self, report: DreamReport) -> None:
        t = time.time()
        try:
            # Audit 2026-10-25 (fix) -- the decay stage used to write
            # unconditionally, so a *dry run* still rewrote the
            # ``score`` column of every memory. That broke this
            # module's own contract ("nothing is written unless
            # apply=True") and the README / CHANGELOG wording. The
            # store call now runs in dry-run mode, which computes the
            # new scores and reports how many rows *would* change
            # without touching the database.
            report.rescored = self.store.rescore_all(
                self.half_life_days, dry_run=not self.apply
            )
        except Exception:  # pragma: no cover - decay must never abort the pass
            log.warning("dream: rescore_all failed; continuing", exc_info=True)
        report.stages["decay"] = round((time.time() - t) * 1000, 1)

    def _candidates(self, now: float) -> list[dict]:
        """Live, non-expired rows that the forgetting stage judges."""
        # Oldest-first: the rows worth judging are the ones closest to
        # (or past) their deadline.
        rows = self.store.list_memories(limit=self.limit, order="asc")
        out: list[dict] = []
        for m in rows:
            if m.superseded_by:
                # A superseded row is history, not a live fact. It is
                # never a forgetting candidate and never a merge
                # candidate -- the chain already resolved it.
                continue
            out.append({
                "id": m.id,
                "kind": m.kind,
                "text": m.text or "",
                "importance": float(m.importance or 0.0),
                "score": float(m.score or 0.0),
                "created_at": float(m.created_at or 0.0),
                "updated_at": float(m.updated_at or 0.0),
                "ttl": m.ttl,
                "agent_id": m.agent_id,
                "user_id": m.user_id,
                "subject_key": m.subject_key,
                "superseded_by": m.superseded_by,
            })
        return out

    def _stage_forget(
        self, report: DreamReport, now: float, rows: list[dict]
    ) -> None:
        """Utility-aware selective forgetting (soft expiry, never delete)."""
        t = time.time()
        report.considered = len(rows)
        signals = self._signal_map([r["id"] for r in rows])
        default_ttl = self.ttl_days * 86400.0

        for row in rows:
            deadline = None
            if row["ttl"] is not None:
                deadline = row["created_at"] + float(row["ttl"])
            elif default_ttl > 0:
                deadline = row["created_at"] + default_ttl
            if deadline is None or now < deadline:
                continue

            recall_count = signals.get(row["id"], 0)
            utility = memory_utility(
                importance=row["importance"],
                recall_count=recall_count,
                created_at=row["created_at"],
                now=now,
                half_life_days=self.half_life_days,
            )
            if utility >= self.utility_threshold:
                report.kept += 1
                report.actions.append(DreamAction(
                    kind="keep",
                    target_id=row["id"],
                    target_text=row["text"][:160],
                    reason=(
                        f"past deadline but utility {utility:.3f} >= "
                        f"{self.utility_threshold:.3f}"
                    ),
                    utility=utility,
                    score=row["score"],
                    payload={"recall_count": recall_count},
                    action="suggest",
                ))
                continue

            report.expired += 1
            action = DreamAction(
                kind="expire",
                target_id=row["id"],
                target_text=row["text"][:160],
                reason=(
                    f"past deadline and utility {utility:.3f} < "
                    f"{self.utility_threshold:.3f}"
                ),
                utility=utility,
                score=row["score"],
                payload={"recall_count": recall_count},
                action="applied" if self.apply else "suggest",
            )
            if self.apply:
                if self.store.expire_memory(row["id"], at=now):
                    action.action = "applied"
                else:
                    action.action = "suggest"
            row["expired"] = True
            report.actions.append(action)

        report.stages["forget"] = round((time.time() - t) * 1000, 1)

    def _signal_map(self, ids: list[str]) -> dict[str, int]:
        if not ids:
            return {}
        try:
            raw = self.store.get_signals(ids)
        except Exception:  # pragma: no cover - signals are best-effort
            log.warning("dream: could not read signals; treating as unused")
            return {}
        return {
            mid: int((sig or {}).get("recall_count") or 0)
            for mid, sig in raw.items()
        }

    def _stage_consolidate(
        self, report: DreamReport, now: float, rows: list[dict]
    ) -> None:
        """Merge near-duplicates into a supersession chain (no deletes)."""
        t = time.time()
        # Rows already expired by the forgetting stage are gone from
        # the current view -- merging them would resurrect a stale
        # fact into the live chain.
        rows = [r for r in rows if not r.get("expired")]
        # Bucket by (kind, agent_id, user_id) so a fact is never merged
        # across namespaces and never merged with a different kind.
        buckets: dict[tuple, list[dict]] = {}
        for row in rows:
            buckets.setdefault(
                (row["kind"], row["agent_id"], row["user_id"]), []
            ).append(row)

        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            # Tokenise once per row.
            prepared = [
                (row, _token_set(row["text"]))
                for row in bucket
            ]
            # Rows already consumed as a merge loser must not be
            # merged again (that would build a chain out of a pair
            # that is already resolved).
            dead: set[str] = set()
            for i, (row_a, tokens_a) in enumerate(prepared):
                if row_a["id"] in dead or not tokens_a:
                    continue
                for row_b, tokens_b in prepared[i + 1:]:
                    if row_b["id"] in dead or not tokens_b:
                        continue
                    sim = _jaccard(tokens_a, tokens_b)
                    if sim < self.similarity:
                        continue
                    winner, loser = self._pick_winner(row_a, row_b)
                    if self.apply:
                        try:
                            merged = self.store.merge_memories(winner["id"], loser["id"])
                        except Exception:  # pragma: no cover - defensive
                            log.warning("dream: merge failed", exc_info=True)
                            continue
                        if not merged.get("merged"):
                            continue
                    dead.add(loser["id"])
                    report.superseded += 1
                    report.actions.append(DreamAction(
                        kind="supersede",
                        target_id=loser["id"],
                        target_text=loser["text"][:160],
                        reason=(
                            f"near-duplicate of {winner['id']} "
                            f"(similarity {sim:.3f} >= {self.similarity:.3f})"
                        ),
                        utility=0.0,
                        score=loser["score"],
                        payload={
                            "winner_id": winner["id"],
                            "similarity": round(sim, 4),
                        },
                        action="applied" if self.apply else "suggest",
                    ))
                    break

        report.stages["consolidate"] = round((time.time() - t) * 1000, 1)

    @staticmethod
    def _pick_winner(a: dict, b: dict) -> tuple[dict, dict]:
        """Higher score wins; ties break on recency then id."""
        ka = (float(a["score"] or 0), float(a["updated_at"] or 0), str(a["id"]))
        kb = (float(b["score"] or 0), float(b["updated_at"] or 0), str(b["id"]))
        return (a, b) if ka >= kb else (b, a)

    def _stage_audit(self, report: DreamReport) -> None:
        t = time.time()
        for action in report.actions:
            try:
                self.store.record_audit(
                    kind=action.kind,
                    action=action.action,
                    target_kind="memory",
                    target_id=action.target_id,
                    target_text=action.target_text,
                    reason=action.reason,
                    score=action.utility or action.score,
                    payload=action.payload,
                )
            except Exception:  # pragma: no cover - audit is best-effort
                log.warning("dream: record_audit failed; skipping")
        report.stages["audit"] = round((time.time() - t) * 1000, 1)


def dream(
    store: MemoryStore,
    *,
    apply: bool = False,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ttl_days: float = DEFAULT_TTL_DAYS,
    utility_threshold: float = DEFAULT_UTILITY_THRESHOLD,
    similarity: float = DEFAULT_SIMILARITY,
    limit: int = DEFAULT_LIMIT,
    record_audit: bool = True,
) -> DreamReport:
    """Convenience wrapper around :class:`Dreamer`.

    ``apply=False`` (default) runs a dry sweep and returns the report
    without touching a single row.
    """
    return Dreamer(
        store,
        half_life_days=half_life_days,
        ttl_days=ttl_days,
        utility_threshold=utility_threshold,
        similarity=similarity,
        limit=limit,
        apply=apply,
        record_audit=record_audit,
    ).run()


__all__ = [
    "DreamAction",
    "DreamReport",
    "Dreamer",
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_LIMIT",
    "DEFAULT_SIMILARITY",
    "DEFAULT_TTL_DAYS",
    "DEFAULT_UTILITY_THRESHOLD",
    "dream",
    "memory_utility",
]

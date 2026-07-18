"""Evolution Consolidator — a 5-stage distillation pipeline.

Replaces the old single-pass LLM consolidator with a hierarchical,
feedback-driven loop. The pipeline is:

  Stage 1  Signal-Aware Scoring
           Each memory's importance is blended with its behavioural
           signals (recall_count, positive/negative feedback). This
           makes "things the user actually uses" float to the top.

  Stage 2  Semantic Batching
           Memories are embedded (or use a hashed fallback) and
           clustered into K buckets via greedy cosine clustering.
           Each cluster <= CLUSTER_MAX so the LLM never sees too much
           at once and the topic stays focused.

  Stage 3  Per-Cluster Distillation
           For each cluster we ask the LLM to produce:
             * a 1-sentence cluster summary
             * a refined importance per row
             * a list of "keep / drop / rewrite" actions
           This is the cheap-per-cluster pass that decides what's
           noise vs. signal.

  Stage 4  Hierarchical Wiki Synthesis
           Cluster summaries + the user's existing wiki pages are
           passed to the LLM, which produces/updates wiki pages
           grouped by *user-profile dimension*:
             - preferences   (how the user likes things done)
             - decisions     (concrete choices the user made)
             - projects      (ongoing work / topics)
             - domain        (technical knowledge to keep)
             - feedback      (corrections, dislikes, do/don't)
           Re-running merges with existing wiki pages by slug.

  Stage 5  Evolution Memo
           We persist a short "evolution memo" (which wiki pages
           changed, how much importance shifted, what signals were
           used). The next run's Stage 4 prompt includes the memo so
           the LLM keeps learning the user's preferences across runs
           without us having to retrain anything.

The consolidator is fully optional: the existing single-pass
``LLMConsolidator`` still works and is what the UI's "AI Consolidate"
button calls by default. ``EvolutionConsolidator`` is wired in as an
opt-in mode so the user can A/B compare.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import ChatHistory, LLMClient, Message
from ..storage.sqlite_store import MemoryStore, StoredMemory

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

CLUSTER_MAX = 15              # max memories per cluster (Stage 3 prompt size)
WIKI_INPUT_CLUSTERS = 8       # how many cluster summaries feed Stage 4
PROFILE_DIMS = ("preferences", "decisions", "projects", "domain", "feedback")

# Stage 1 weight on signals. importance in [0,1] is the LLM/original value;
# we blend in recall_count and negative as dampeners.
W_RECALL = 0.10               # +0.10 per high-recall cluster, capped
W_NEGATIVE = 0.15             # -0.15 per negative feedback event, capped
RECALL_SATURATION = 5         # recall_count / 5 saturates the recall boost
NEGATIVE_SATURATION = 3       # 3 negative events = full dampener

# Stage 3 system prompt: per-cluster, keep/rewrite/drop actions.
_CLUSTER_SYSTEM = (
    "You are an assistant that tidies a small cluster of personal memory snippets. "
    "For EACH item return a JSON object with:\n"
    '  keep: boolean (true = the row contains real signal, keep it)\n'
    '  importance: number 0..1 (your new estimate of long-term importance)\n'
    '  distill: a SHORT rewritten version (<= 200 chars) that captures the core '
    'fact, OR empty string to keep the original. Strip pleasantries, tool chatter, '
    'code fences, and meta commentary.\n'
    '  tags: array of up to 5 lowercase tags.\n'
    'Reply with JSON: {"items": [...]}, no prose.'
)

# Stage 4 system prompt: build/update wiki pages from cluster summaries.
_WIKI_SYSTEM = (
    "You maintain a personal knowledge base for one user. You receive the user's "
    "current wiki pages plus a batch of recently distilled cluster summaries. "
    "Update or create pages that capture the user's profile. ALWAYS bucket into "
    "one of these dimensions: preferences, decisions, projects, domain, feedback. "
    "Use a slug like 'preferences-time-display', 'project-loop-memory', "
    "'decision-batch-size-50', etc. Each page must have:\n"
    '  slug, title, summary (<= 280 chars), body (concise markdown, 3-8 lines), '
    'tags (3-6), importance 0..1, evidence_ids (memory ids that back this page).\n'
    "If a cluster summary does not add new info, skip it. Prefer to UPDATE existing "
    "pages (slug match) rather than create duplicates. Reply with JSON: "
    '{"pages": [...]}, no prose. If nothing is worth adding, reply {"pages": []}.'
)


# ----------------------------------------------------------------------------
# Public dataclass
# ----------------------------------------------------------------------------


@dataclass
class EvolutionStats:
    scanned: int = 0
    rescored: int = 0
    dropped: int = 0
    resummarized: int = 0
    clusters: int = 0
    cluster_calls: int = 0
    wiki_calls: int = 0
    wiki_created: int = 0
    wiki_updated: int = 0
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "rescored": self.rescored,
            "dropped": self.dropped,
            "resummarized": self.resummarized,
            "clusters": self.clusters,
            "cluster_calls": self.cluster_calls,
            "wiki_calls": self.wiki_calls,
            "wiki_created": self.wiki_created,
            "wiki_updated": self.wiki_updated,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "notes": self.notes,
            "stages": self.stages,
        }


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _extract_json(text: str) -> Any | None:
    """Pull the first balanced JSON object out of an LLM reply."""
    import re
    if not text:
        return None
    # direct
    try:
        return json.loads(text)
    except Exception:
        pass
    # fenced ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # first {...} or first [...]
    for opener, closer in [("{", "}"), ("[", "]")]:
        i = text.find(opener)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i : j + 1])
                    except Exception:
                        break
    return None


def _hash_embed(text: str, dim: int = 128) -> list[float]:
    """Deterministic 128-dim embedding (feature hashing). Cheap fallback so
    semantic batching works even without sentence-transformers installed."""
    v = [0.0] * dim
    tokens = (text or "").lower().split()
    if not tokens:
        return v
    for tok in tokens:
        h = hashlib.md5(tok.encode("utf-8")).digest()
        idx = h[0] % dim
        sign = 1.0 if (h[1] & 1) else -1.0
        v[idx] += sign
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


# ----------------------------------------------------------------------------
# Consolidator
# ----------------------------------------------------------------------------


class EvolutionConsolidator:
    """5-stage distillation pipeline. Drop-in replacement for
    ``LLMConsolidator.run``."""

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMClient,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = dict(config or {})
        self._cache: dict[str, str] = {}
        self._cache_ttl = 300.0
        self._cache_ts: dict[str, float] = {}
        self._run_id: str | None = None

    # --- public ----------------------------------------------------------

    def set_run_id(self, run_id: str | None) -> None:
        self._run_id = run_id

    def run(
        self,
        memories: list[StoredMemory] | None = None,
        progress: Callable[[int, int], None] | None = None,
        limit: int = 300,
    ) -> EvolutionStats:
        t0 = time.time()
        stats = EvolutionStats()
        cfg = self.config
        dry_run = bool(cfg.get("dry_run", False))

        if memories is None:
            memories = self.store.list_memories(limit=limit)
        memories = list(memories)
        stats.scanned = len(memories)
        if not memories:
            stats.notes.append("no memories")
            stats.elapsed_ms = (time.time() - t0) * 1000
            return stats

        if progress:
            try:
                progress(0, len(memories))
            except Exception:
                pass

        # Stage 1: Signal-aware rescoring
        s1_t = time.time()
        stage1_in = len(memories)
        rescored_map = self._stage1_signal_scoring(memories)
        stats.rescored = sum(1 for v in rescored_map.values() if v)
        # After stage 1 we run rescore_all to update the score column.
        # Capture how many actually changed.
        rescore_changed = 0
        try:
            rescore_changed = self.store.rescore_all(half_life_days=30.0)
        except Exception:
            pass
        s1 = {
            "in": stage1_in,
            "out": rescore_changed or stage1_in,
            "ms": round((time.time() - s1_t) * 1000, 1),
            "note": f"rescored {rescore_changed}/{stage1_in} memories",
            "evidence_ids": [m.id for m in memories[:200]],
        }
        stats.stages["score"] = s1
        self._record_stage("score", stage1_in, s1["out"], s1["note"], s1)
        if progress:
            try:
                progress(int(len(memories) * 0.2), len(memories))
            except Exception:
                pass

        # Stage 2: Semantic batching
        s2_t = time.time()
        clusters = self._stage2_cluster(memories, max_per_cluster=CLUSTER_MAX)
        stats.clusters = len(clusters)
        s2 = {
            "in": stage1_in,
            "out": len(clusters),
            "ms": round((time.time() - s2_t) * 1000, 1),
            "note": f"formed {len(clusters)} clusters",
        }
        stats.stages["cluster"] = s2
        self._record_stage("cluster", stage1_in, len(clusters), s2["note"], s2)
        if progress:
            try:
                progress(int(len(memories) * 0.4), len(memories))
            except Exception:
                pass

        # Stage 3: Per-cluster distillation
        s3_t = time.time()
        cluster_summaries: list[dict[str, Any]] = []
        kept_ids: set = set()
        dropped_ids: set = set()
        is_rule = self._echo_provider()
        for ci, cluster in enumerate(clusters):
            if is_rule:
                # No LLM -> keep everything as-is. Build a summary from
                # the top-3 important items so Stage 4 still has signal.
                top = sorted(cluster, key=lambda m: -(m.importance or 0))[:3]
                summary_text = " / ".join((m.text or "")[:120] for m in top)[:400]
                kinds = [m.kind for m in cluster if m.kind]
                kind = max(set(kinds), key=kinds.count) if kinds else ""
                all_tags = [t for m in cluster for t in (m.tags or []) if t]
                dom_tag = max(set(all_tags), key=all_tags.count) if all_tags else ""
                avg_imp = sum((m.importance or 0) for m in cluster) / max(1, len(cluster))
                summary = {
                    "text": summary_text,
                    "size": len(cluster),
                    "kept": len(cluster),
                    "dropped": 0,
                    "evidence_ids": [m.id for m in cluster][:50],
                    "kind": kind,
                    "dominating_tag": dom_tag,
                    "avg_importance": round(avg_imp, 3),
                }
                actions = {m.id: {"keep": True, "importance": m.importance, "distill": "", "tags": list(m.tags or [])} for m in cluster}
            else:
                summary, actions = self._stage3_distill_cluster(cluster, cfg, stats)
            cluster_summaries.append(summary)
            if not dry_run:
                kept = self._apply_actions(cluster, actions, stats)
                kept_ids |= kept
                for m in cluster:
                    if m.id not in kept:
                        dropped_ids.add(m.id)
            if progress:
                try:
                    progress(int(len(memories) * (0.4 + 0.4 * (ci + 1) / max(1, len(clusters)))), len(memories))
                except Exception:
                    pass
        s3 = {
            "in": len(clusters),
            "out": len([s for s in cluster_summaries if s.get("text")]),
            "ms": round((time.time() - s3_t) * 1000, 1),
            "note": f"{stats.cluster_calls} LLM calls · {len(clusters)} clusters · {len(kept_ids)} kept / {len(dropped_ids)} dropped",
            "evidence_ids": list(kept_ids)[:200],
            "kept_ids": list(kept_ids)[:200],
            "dropped_ids": list(dropped_ids)[:200],
        }
        stats.stages["distill"] = s3
        self._record_stage("distill", len(clusters), s3["out"], s3["note"], s3)

        # Stage 4: Hierarchical wiki synthesis
        s4_t = time.time()
        wiki_pages = self._stage4_wiki_synthesis(cluster_summaries, cfg, stats)
        if not dry_run:
            stats.wiki_created = wiki_pages.get("created", 0)
            stats.wiki_updated = wiki_pages.get("updated", 0)
        # Collect evidence ids from the wiki pages so drill-down can list them
        wiki_evidence: list = []
        try:
            for p in self.store.list_wiki_pages(limit=50):
                eids = p.get("evidence_ids") or []
                if isinstance(eids, list):
                    wiki_evidence.extend([str(x) for x in eids])
        except Exception:
            pass
        s4 = {
            "in": len(cluster_summaries),
            "out": stats.wiki_created + stats.wiki_updated,
            "ms": round((time.time() - s4_t) * 1000, 1),
            "note": f"created={stats.wiki_created} updated={stats.wiki_updated}",
            "evidence_ids": wiki_evidence[:200],
        }
        stats.stages["wiki"] = s4
        self._record_stage("wiki", len(cluster_summaries), s4["out"], s4["note"], s4)
        if progress:
            try:
                progress(len(memories), len(memories))
            except Exception:
                pass

        # Stage 5: Evolution memo
        s5_t = time.time()
        self._stage5_memo(stats)
        s5 = {
            "in": stats.wiki_created + stats.wiki_updated,
            "out": 1,
            "ms": round((time.time() - s5_t) * 1000, 1),
            "note": "evolution memo updated",
        }
        stats.stages["memo"] = s5
        self._record_stage("memo", s5["in"], 1, s5["note"], s5)

        # Rescore from new importance
        if not dry_run and stats.rescored:
            try:
                self.store.rescore_all(half_life_days=30.0)
            except Exception:
                pass

        stats.elapsed_ms = (time.time() - t0) * 1000
        return stats

    # --- Stage 1: Signal-aware scoring ----------------------------------

    def _stage1_signal_scoring(
        self, memories: list[StoredMemory]
    ) -> dict[str, bool]:
        """Blend original importance with behavioural signals. We do not
        write back here — the per-cluster LLM pass is what writes the new
        importance. This stage just gives the LLM richer ranking input."""
        rescored: dict[str, bool] = {}
        for m in memories:
            sig = self.store.get_signal(m.id)
            boost = min(W_RECALL, W_RECALL * sig["recall_count"] / RECALL_SATURATION)
            damp = min(W_NEGATIVE, W_NEGATIVE * sig["negative"] / NEGATIVE_SATURATION)
            adj = (m.importance or 0.0) + boost - damp
            adj = max(0.0, min(1.0, adj))
            if abs(adj - (m.importance or 0.0)) > 0.05:
                rescored[m.id] = True
        return rescored

    # --- Stage 2: Semantic batching -------------------------------------

    def _stage2_cluster(
        self,
        memories: list[StoredMemory],
        max_per_cluster: int = CLUSTER_MAX,
    ) -> list[list[StoredMemory]]:
        """Greedy cosine clustering using a hashed embedding. Memories that
        lack enough text to embed fall into a 'misc' cluster of their own
        so we never lose them."""
        if not memories:
            return []

        # Sort by adjusted importance desc so high-signal memories seed clusters
        def _score(m: StoredMemory) -> float:
            sig = self.store.get_signal(m.id)
            boost = min(0.1, 0.1 * sig["recall_count"] / RECALL_SATURATION)
            damp = min(0.15, 0.15 * sig["negative"] / NEGATIVE_SATURATION)
            return (m.importance or 0.0) + boost - damp

        ranked = sorted(memories, key=_score, reverse=True)

        clusters: list[dict[str, Any]] = []  # {centroid, items}
        for m in ranked:
            text = (m.text or "").strip()
            if len(text) < 8:
                # super short items go to a misc cluster at the end
                clusters.append({"centroid": None, "items": [m], "misc": True})
                continue
            emb = _hash_embed(text)
            placed = False
            for cl in clusters:
                if cl.get("misc") or len(cl["items"]) >= max_per_cluster:
                    continue
                sim = _cos(emb, cl["centroid"])
                if sim >= 0.35:  # hashed embeddings are noisier, lower threshold
                    cl["items"].append(m)
                    # update centroid (running mean)
                    n = len(cl["items"])
                    cl["centroid"] = [
                        (cl["centroid"][i] * (n - 1) + emb[i]) / n for i in range(len(emb))
                    ]
                    placed = True
                    break
            if not placed:
                clusters.append({"centroid": emb, "items": [m], "misc": False})

        # Sort clusters: largest first, misc last
        clusters.sort(key=lambda c: (c.get("misc", False), -len(c["items"])))
        return [c["items"] for c in clusters]

    # --- Stage 3: Per-cluster distillation ------------------------------

    def _stage3_distill_cluster(
        self,
        cluster: list[StoredMemory],
        cfg: dict[str, Any],
        stats: EvolutionStats,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Return (cluster_summary, per-item actions)."""
        # Build user payload
        payload = []
        for m in cluster:
            sig = self.store.get_signal(m.id)
            payload.append({
                "id": m.id,
                "kind": m.kind,
                "tags": list(m.tags or []),
                "importance": round(m.importance or 0.0, 3),
                "recall_count": sig["recall_count"],
                "negative": sig["negative"],
                "text": (m.text or "")[:600],
            })
        user_prompt = json.dumps({"items": payload}, ensure_ascii=False)
        cache_key = hashlib.sha1(
            (user_prompt + "||" + (getattr(self.provider, "model", "?") or "?")
             + "||cluster||" + str(float(cfg.get("temperature") or 0.2))).encode()
        ).hexdigest()
        reply = self._cached_call(cache_key, _CLUSTER_SYSTEM, user_prompt, cfg, stats, kind="cluster")

        actions: dict[str, dict[str, Any]] = {}
        parsed = _extract_json(reply or "")
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            for it in parsed["items"]:
                if isinstance(it, dict) and "id" in it:
                    actions[str(it["id"])] = it

        # Build a 1-sentence cluster summary from the LLM reply (or fall
        # back to a stitched top-3 important items).
        cluster_text = ""
        if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
            cluster_text = parsed["summary"].strip()[:400]
        if not cluster_text:
            top = sorted(cluster, key=lambda m: -(m.importance or 0))[:3]
            cluster_text = " / ".join((m.text or "")[:120] for m in top)[:400]

        # Pull memory ids in this cluster so downstream wiki synthesis
        # can cite them as evidence. Use the kept ones when the LLM
        # classified them, otherwise all cluster items.
        kept_set = {m_id for m_id, a in actions.items() if a.get("keep") is True}
        evidence_ids = [m.id for m in cluster if (not kept_set or m.id in kept_set)]
        # Most-common kind and tag — used by the rule-based wiki step to
        # produce a meaningful title when no LLM is involved.
        kinds = [m.kind for m in cluster if m.kind]
        kind = max(set(kinds), key=kinds.count) if kinds else ""
        all_tags = [t for m in cluster for t in (m.tags or []) if t]
        dom_tag = max(set(all_tags), key=all_tags.count) if all_tags else ""
        avg_imp = sum((m.importance or 0) for m in cluster) / max(1, len(cluster))

        summary = {
            "text": cluster_text,
            "size": len(cluster),
            "kept": len(evidence_ids) if evidence_ids else sum(1 for a in actions.values() if a.get("keep") is True),
            "dropped": len(cluster) - (len(evidence_ids) if evidence_ids else sum(1 for a in actions.values() if a.get("keep") is True)),
            "evidence_ids": evidence_ids[:50],
            "kind": kind,
            "dominating_tag": dom_tag,
            "avg_importance": round(avg_imp, 3),
        }
        return summary, actions

    def _apply_actions(
        self,
        cluster: list[StoredMemory],
        actions: dict[str, dict[str, Any]],
        stats: EvolutionStats,
    ) -> set:
        """Apply keep / drop / rewrite actions to the DB. Returns the set of
        kept memory ids."""
        kept: set = set()
        for m in cluster:
            act = actions.get(m.id)
            if not act:
                # no LLM action => keep as-is
                kept.add(m.id)
                continue
            if act.get("keep") is False:
                try:
                    self.store.delete_memory(m.id)
                    stats.dropped += 1
                except Exception:
                    kept.add(m.id)
                continue
            new_text = (act.get("distill") or "").strip()
            new_importance = act.get("importance")
            new_tags = act.get("tags")
            updates: dict[str, Any] = {}
            try:
                if isinstance(new_importance, (int, float)):
                    ni = max(0.0, min(1.0, float(new_importance)))
                    if abs(ni - (m.importance or 0.0)) > 1e-3:
                        updates["importance"] = ni
                        stats.rescored += 1
            except Exception:
                pass
            if new_text and new_text != (m.text or "") and len(new_text) <= 400:
                updates["text"] = new_text
                stats.resummarized += 1
            if isinstance(new_tags, list):
                tags_clean = [str(t).strip().lower() for t in new_tags if t and str(t).strip()][:6]
                if tags_clean:
                    updates["tags"] = tags_clean
            if updates:
                try:
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
                except Exception:
                    pass
            kept.add(m.id)
        return kept

    # --- Stage 4: Wiki synthesis ----------------------------------------

    def _stage4_wiki_synthesis(
        self,
        cluster_summaries: list[dict[str, Any]],
        cfg: dict[str, Any],
        stats: EvolutionStats,
    ) -> dict[str, int]:
        if not cluster_summaries:
            return {"created": 0, "updated": 0, "calls": 0}

        # Cap input
        clusters = cluster_summaries[:WIKI_INPUT_CLUSTERS]

        # Rule-based fast path: skip the wiki LLM call (it would just echo
        # back non-JSON) and synthesize wiki pages deterministically by
        # clustering by kind. Real wiki text comes from cluster summaries.
        if self._echo_provider():
            return self._stage4_rules(clusters, stats)

        existing = self.store.list_wiki_pages(limit=50)
        existing_payload = [
            {"slug": p.get("slug", ""), "title": p.get("title", ""),
             "summary": (p.get("summary") or "")[:200],
             "tags": list(p.get("tags") or []),
             "importance": round(p.get("importance") or 0, 2)}
            for p in existing
        ]
        # Evolution memo: last 3 runs
        memo = self.store.get_setting("evolution_memo", "") or ""

        user_payload = {
            "profile_dimensions": list(PROFILE_DIMS),
            "evolution_memo": memo[:1500] if isinstance(memo, str) else "",
            "existing_wiki": existing_payload,
            "cluster_summaries": [
                {"text": cs["text"], "size": cs["size"]}
                for cs in clusters
            ],
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False)
        cache_key = hashlib.sha1(
            (user_prompt + "||" + (getattr(self.provider, "model", "?") or "?")
             + "||wiki-evo||" + str(float(cfg.get("temperature") or 0.3))).encode()
        ).hexdigest()
        reply = self._cached_call(cache_key, _WIKI_SYSTEM, user_prompt, cfg, stats, kind="wiki")

        parsed = _extract_json(reply or "")
        if not isinstance(parsed, dict):
            # LLM unreachable or returned junk — fall back to rule-based
            # synthesis so the wiki still grows even without an LLM.
            stats.notes.append("wiki LLM reply was not valid JSON — falling back to rule-based synthesis")
            return self._stage4_rules(clusters, stats)
        pages = parsed.get("pages") or []
        if not isinstance(pages, list) or len(pages) == 0:
            # LLM produced no pages — fall back too so the user sees
            # real wiki content immediately.
            stats.notes.append("wiki LLM returned 0 pages — falling back to rule-based synthesis")
            return self._stage4_rules(clusters, stats)

        created = 0
        updated = 0
        for p in pages:
            if not isinstance(p, dict):
                continue
            slug = (p.get("slug") or "").strip().lower().replace(" ", "-")[:80]
            title = (p.get("title") or "").strip()
            body = (p.get("body") or "").strip()
            if not slug or not title or not body:
                continue
            tags = p.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip().lower() for t in tags if t and str(t).strip()][:8]
            try:
                importance = max(0.0, min(1.0, float(p.get("importance") or 0.5)))
            except Exception:
                importance = 0.5
            evidence = p.get("evidence_ids") or []
            if not isinstance(evidence, list):
                evidence = []
            evidence = [str(x) for x in evidence if x][:50]
            summary = (p.get("summary") or "").strip()[:400]
            existing_p = self.store.get_wiki_page_by_slug(slug)
            try:
                self.store.upsert_wiki_page(
                    slug=slug, title=title, body=body, summary=summary,
                    tags=tags, importance=importance, evidence_ids=evidence,
                    run_id=self._run_id,
                )
            except Exception as e:
                stats.notes.append(f"wiki upsert err: {e}")
                continue
            if existing_p is None:
                created += 1
            else:
                updated += 1
        return {"created": created, "updated": updated, "calls": 1}


    def _echo_provider(self) -> bool:
        return type(self.provider).__name__ == "RuleBasedProvider"

    def _stage4_rules(
        self,
        clusters: list[dict[str, Any]],
        stats: EvolutionStats,
    ) -> dict[str, int]:
        """Rule-based wiki synthesis: one page per cluster.

        Produces real, browsable wiki pages even when no LLM is
        configured (or when the configured LLM is failing). The pages
        group memories by the dominant tag or by an extracted topic,
        with a real title (not a verbatim prompt), a short summary,
        and the actual evidence ids of the contributing memories.
        """
        import hashlib as _h
        import re as _re
        created = 0
        updated = 0
        for i, cs in enumerate(clusters):
            text = (cs.get("text") or "").strip()
            if not text or len(text) < 12:
                continue
            cleaned = _re.sub(
                r"^(User intent|You are|Assistant|Human):\s*",
                "", text, flags=_re.IGNORECASE
            )
            cleaned = _re.sub(
                r"^\[cron:[a-f0-9-]+\s*[^\]]*\]\s*",
                "", cleaned
            )
            cleaned = _re.sub(r"<[^>]+>", "", cleaned)
            cleaned = _re.sub(r"\s+", " ", cleaned).strip()
            if len(cleaned) < 8:
                continue
            # Slug: prefer kind/topic over a hashed blob
            kind_hint = (cs.get("kind") or cs.get("dominating_tag") or "").lower().strip()
            words = _re.findall(r"[A-Za-z0-9一-鿿]+", cleaned.lower())
            topic_words = "-".join(words[:4]) or f"cluster-{i+1}"
            slug_src = f"{kind_hint}-{topic_words}" if kind_hint else topic_words
            slug = (slug_src[:60] or f"cluster-{i+1}").lower()
            slug = _re.sub(r"[^a-z0-9一-鿿-]+", "-", slug).strip("-")
            if not slug:
                slug = "auto-cluster-" + _h.md5(slug_src.encode("utf-8")).hexdigest()[:10]
            title = cleaned[:80].rstrip(" .,;:") or f"Cluster {i+1}"
            evidence_ids = cs.get("evidence_ids") or cs.get("kept_ids") or []
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            evidence_ids = [str(x) for x in evidence_ids if x][:50]
            body_lines = [cleaned]
            if cs.get("size"):
                body_lines.append(f"\n_Grouped from {cs['size']} memory entries._")
            if evidence_ids:
                body_lines.append(
                    f"\n_Source memories: {len(evidence_ids)} (e.g. "
                    f"{', '.join(evidence_ids[:4])})_"
                )
            body = "\n".join(body_lines)
            summary = cleaned[:280]
            tags = ["auto", "rule-based"]
            if kind_hint:
                tags.append(kind_hint)
            importance = 0.5
            try:
                if "avg_importance" in cs:
                    importance = float(cs["avg_importance"])
            except Exception:
                pass
            existing_p = self.store.get_wiki_page_by_slug(slug)
            try:
                self.store.upsert_wiki_page(
                    slug=slug, title=title, body=body, summary=summary,
                    tags=tags[:6], importance=importance,
                    evidence_ids=evidence_ids, run_id=self._run_id,
                )
            except Exception:
                continue
            if existing_p is None:
                created += 1
            else:
                updated += 1
        stats.wiki_calls += 1
        return {"created": created, "updated": updated, "calls": 1}


    # --- Stage 5: Evolution memo ----------------------------------------

    def _stage5_memo(self, stats: EvolutionStats) -> None:
        """Persist a short memo describing what this run changed so the next
        run's wiki prompt can use it as evolution context."""
        memo = {
            "ts": time.time(),
            "rescored": stats.rescored,
            "dropped": stats.dropped,
            "resummarized": stats.resummarized,
            "clusters": stats.clusters,
            "wiki_created": stats.wiki_created,
            "wiki_updated": stats.wiki_updated,
            "notes": stats.notes[:3],
        }
        text = json.dumps(memo, ensure_ascii=False)
        try:
            self.store.set_setting("evolution_memo", text)
        except Exception:
            pass

    # --- utilities -------------------------------------------------------

    def _cached_call(
        self,
        cache_key: str,
        system: str,
        user_prompt: str,
        cfg: dict[str, Any],
        stats: EvolutionStats,
        *,
        kind: str = "",
    ) -> str:
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached is not None and (now - self._cache_ts.get(cache_key, 0)) < self._cache_ttl:
            return cached
        history = ChatHistory(system=system, messages=[Message(role="user", content=user_prompt)])
        try:
            reply = self.provider.complete(
                history,
                temperature=float(cfg.get("temperature") or 0.3),
                max_tokens=int(cfg.get("max_output_tokens") or 900),
            ) or ""
        except Exception as e:
            stats.notes.append(f"{kind} llm error: {type(e).__name__}: {e}")
            return ""
        self._cache[cache_key] = reply
        self._cache_ts[cache_key] = now
        if kind == "cluster":
            stats.cluster_calls += 1
        elif kind == "wiki":
            stats.wiki_calls += 1
        return reply

    def _record_stage(
        self,
        stage: str,
        in_count: int,
        out_count: int,
        note: str,
        stats: dict[str, Any],
    ) -> None:
        try:
            rid = self.store.start_pipeline_run(stage)
            self.store.finish_pipeline_run(
                rid, in_count=in_count, out_count=out_count, note=note, stats=stats,
            )
        except Exception:
            pass

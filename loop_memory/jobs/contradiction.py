"""Wiki-page contradiction detection.

When the consolidator writes a new or updated wiki page, we don't just
stop at "it's a paragraph about X". Two pages about the *same* topic
might disagree — "user prefers tabs" vs. "user prefers spaces" — and
the UI should surface that for the user to merge. We do this cheaply
by comparing ``key_facts`` rather than full bodies: a page's key
facts are short, single-sentence bullets produced by the LLM, so
high Jaccard similarity over them strongly suggests "same topic".

Why not whole-body similarity? Bodies are long and chatty; LLM
paraphrasing inflates their divergence even when the underlying
meaning matches. ``key_facts`` are constrained to single-sentence
statements which makes them a much sharper comparable unit.

Detection runs as a post-write hook in the consolidator pipeline,
*and* on demand via the API so the UI can re-scan after the user
adds new wiki pages by hand.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..storage.sqlite_store import MemoryStore

log = logging.getLogger(__name__)


# --- tokenization ------------------------------------------------------

# We split on whitespace + ASCII punctuation, but keep CJK unigrams
# + 2-grams so "偏好 Tab 缩进" and "使用 Tab 缩进" still produce
# overlapping tokens. We lowercase ASCII; CJK is left as-is to keep
# 2-grams intact.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    raw = _TOKEN_RE.findall(text)
    grams: set[str] = set()
    for i, t in enumerate(raw):
        grams.add(t)
        if i + 1 < len(raw) and len(t) == 1 and len(raw[i + 1]) == 1:
            # CJK bigram
            grams.add(t + raw[i + 1])
    return grams


def _fact_set(key_facts: list[str] | None) -> set[str]:
    out: set[str] = set()
    for f in key_facts or []:
        out |= _tokenize(f)
    return out


# --- scoring -----------------------------------------------------------

# A page is considered a contradiction candidate when its key_facts
# Jaccard overlap with another page is at or above this threshold AND
# the page shares at least one tag in common. Both gates together
# dramatically reduce false positives (the store has many overlapping
# but non-conflicting facts).
DEFAULT_THRESHOLD = 0.45


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


@dataclass
class ContradictionMatch:
    a_id: str
    b_id: str
    score: float
    shared_tags: list[str]
    a_title: str
    b_title: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "a_id": self.a_id,
            "b_id": self.b_id,
            "score": round(self.score, 3),
            "shared_tags": self.shared_tags,
            "a_title": self.a_title,
            "b_title": self.b_title,
        }


def detect_for_page(
    store: MemoryStore,
    page_id: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_candidates: int = 5,
) -> list[ContradictionMatch]:
    """Find contradiction candidates for a single wiki page.

    Returns up to ``max_candidates`` matches ordered by descending
    similarity. The candidates are NOT written to the store — the
    caller decides whether to persist via ``write_contradicting_ids``.
    """
    target = store.get_wiki_page(page_id)
    if not target:
        return []
    target_facts = _fact_set(target.get("key_facts") or [])
    if not target_facts:
        return []
    target_tags = set(target.get("tags") or [])
    target_scope = target.get("scope") or "global"

    candidates: list[ContradictionMatch] = []
    # Pull pages in the same scope first (cheaper and more relevant);
    # fall back to global if nothing surfaces.
    pool = list(store.list_wiki_pages(limit=500, scope=target_scope))
    if len(pool) < 5:
        pool = list(store.list_wiki_pages(limit=500))

    for other in pool:
        if other["id"] == page_id:
            continue
        if (other.get("contradicting_ids") or []):
            # already has a partner — skip so we don't duplicate work
            if page_id in (other.get("contradicting_ids") or []):
                continue
        other_facts = _fact_set(other.get("key_facts") or [])
        if not other_facts:
            continue
        other_tags = set(other.get("tags") or [])
        shared = sorted(target_tags & other_tags)
        if not shared and target_scope != "global":
            # Different topic — skip unless we're in global scope
            continue
        score = _jaccard(target_facts, other_facts)
        if score >= threshold:
            candidates.append(ContradictionMatch(
                a_id=page_id,
                b_id=other["id"],
                score=score,
                shared_tags=shared,
                a_title=target.get("title") or "",
                b_title=other.get("title") or "",
            ))
    candidates.sort(key=lambda m: m.score, reverse=True)
    return candidates[:max_candidates]


def write_contradicting_ids(
    store: MemoryStore,
    matches: Iterable[ContradictionMatch],
) -> int:
    """Persist the symmetric ``contradicting_ids`` columns on both
    sides of every match. Returns the number of pages updated."""
    by_page: dict[str, set[str]] = {}
    for m in matches:
        by_page.setdefault(m.a_id, set()).add(m.b_id)
        by_page.setdefault(m.b_id, set()).add(m.a_id)
    n = 0
    for page_id, ids in by_page.items():
        cur = store.get_wiki_page(page_id)
        if not cur:
            continue
        existing = set(cur.get("contradicting_ids") or [])
        merged = sorted(existing | ids)
        if sorted(existing) == merged:
            continue
        # Re-upsert by slug so we don't depend on upsert supporting
        # the page_id kwarg (it doesn't — slug is the lookup key).
        store.upsert_wiki_page(
            slug=cur.get("slug") or "",
            title=cur.get("title") or "",
            body=cur.get("body") or "",
            summary=cur.get("summary") or "",
            tags=cur.get("tags") or [],
            importance=float(cur.get("importance") or 0.5),
            evidence_ids=cur.get("evidence_ids") or [],
            run_id=cur.get("run_id"),
            scope=cur.get("scope") or "global",
            key_facts=cur.get("key_facts") or [],
            contradicting_ids=merged,
        )
        n += 1
    return n


def scan_all(
    store: MemoryStore,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    progress: Any = None,
) -> dict[str, Any]:
    """Re-scan every wiki page for contradictions.

    Used after the user has manually edited pages or imported new
    ones. Cheap enough to run interactively for stores with < 1000
    pages.
    """
    t0 = time.time()
    pages = store.list_wiki_pages(limit=2000)
    all_matches: list[ContradictionMatch] = []
    for i, p in enumerate(pages):
        if progress:
            try:
                progress(i, len(pages), p.get("title") or p["id"])
            except Exception:
                pass
        m = detect_for_page(store, p["id"], threshold=threshold)
        all_matches.extend(m)
    # Symmetric write — pass unique pair tuples (sorted ids so each
    # pair only gets written once).
    seen_pairs: set[tuple[str, str]] = set()
    deduped: list[ContradictionMatch] = []
    for m in all_matches:
        key = tuple(sorted([m.a_id, m.b_id]))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        deduped.append(m)
    n_updated = write_contradicting_ids(store, deduped)
    return {
        "pages_scanned": len(pages),
        "matches": [m.to_dict() for m in deduped],
        "pages_updated": n_updated,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "threshold": threshold,
    }


def list_contradictions(store: MemoryStore) -> list[dict[str, Any]]:
    """Return every wiki page that has at least one contradicting id.

    Each entry carries the page metadata plus a list of partner
    summaries so the UI can render a one-row-per-conflict table.
    """
    pages = store.list_wiki_pages(limit=2000)
    out: list[dict[str, Any]] = []
    for p in pages:
        cids = p.get("contradicting_ids") or []
        if not cids:
            continue
        partners = []
        for cid in cids:
            other = store.get_wiki_page(cid)
            if other:
                partners.append({
                    "id": cid,
                    "title": other.get("title") or "",
                    "summary": (other.get("summary") or "")[:200],
                    "importance": float(other.get("importance") or 0),
                    "scope": other.get("scope") or "global",
                })
        out.append({
            "id": p["id"],
            "title": p.get("title") or "",
            "summary": (p.get("summary") or "")[:200],
            "importance": float(p.get("importance") or 0),
            "scope": p.get("scope") or "global",
            "key_facts": p.get("key_facts") or [],
            "partners": partners,
        })
    return out


__all__ = [
    "detect_for_page",
    "write_contradicting_ids",
    "scan_all",
    "list_contradictions",
    "ContradictionMatch",
    "DEFAULT_THRESHOLD",
]

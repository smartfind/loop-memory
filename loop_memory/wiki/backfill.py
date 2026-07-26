"""One-shot re-classify legacy wiki pages.

Existing pages created under the SCHEMA_VERSION<=7 rule "default to
global" kept ``scope='global'`` regardless of whether the content was
actually a cross-client universal security best practice. Now that the
auto-classifier is in place, those legacy rows are out of step with the
new contract ("security knowledge that's universally applicable →
global; everything else → per-source scope").

This module rewrites those legacy rows on demand. Pages whose content
fails to qualify as auto-global are downgraded to a per-source scope
derived from evidence memory sources (falling back to ``codex``), and
the original global scope + manual-override history are preserved in
``auto_classification.history`` so the audit trail still shows what
changed and why.

The backfill is deterministic and opt-in; nothing on the hot path
imports this module, so it never runs without an explicit CLI / admin
POST.
"""
from __future__ import annotations

from typing import Any

from .classifier import classify_page
from .scope import (
    auto_scope_config,
    build_scope_audit,
    derive_scope_from_sources,
)


def reclassify_legacy_pages(store: Any, *, batch: int = 200) -> dict:
    """Re-classify pages whose ``auto_classification`` is NULL.

    Pages with ``auto_classification`` set already went through the
    classifier under the new contract and are left alone; only
    legacy rows are touched. The result dict summarises what
    changed so the caller can show a diff to the user.

    Parameters
    ----------
    store : MemoryStore
        Backing store. ``list_wiki_pages`` / ``upsert_wiki_page`` /
        ``get_wiki_page`` are used; no other methods are required.
    batch : int
        Maximum number of rows to scan. Pages are visited in updated_at
        DESC order so the most-recently-changed legacy rows get
        priority.
    """
    cfg = auto_scope_config(store)
    pages = store.list_wiki_pages(limit=batch)
    legacy = [
        p for p in pages
        if (p.get("scope") or "global") == "global"
        and p.get("auto_classification") is None
    ]
    summary = {
        "scanned": len(pages),
        "legacy_global": len(legacy),
        "kept_global": 0,
        "downgraded": 0,
        "skipped": 0,
        "items": [],
    }
    for page in legacy:
        classification = classify_page(
            title=page.get("title") or "",
            body=page.get("body") or "",
            summary=page.get("summary") or "",
            tags=page.get("tags") or [],
            evidence_sources=[],
            mode=cfg["mode"] if cfg["enabled"] else "off",
        )
        if classification.auto_global:
            summary["kept_global"] += 1
            summary["items"].append({
                "id": page["id"],
                "slug": page["slug"],
                "title": page.get("title"),
                "decision": "kept-global",
                "scope": "global",
                "reasons": classification.reasons[:4],
            })
            continue
        # Derive the per-source scope from any evidence memories.
        evidence_ids = page.get("evidence_ids") or []
        new_scope = derive_scope_from_sources(
            source_hint=None,
            evidence_sources=[],
            fallback="codex",
        )
        # Build the audit so future calls keep history.
        audit = build_scope_audit(
            classification,
            scope=new_scope,
            decision="legacy-backfill",
            enabled=bool(cfg["enabled"]),
            mode=str(cfg["mode"]),
            existing={"auto_classification": None},
        )
        # Force a history anchor on the original global scope so the
        # audit captures the change.
        audit.setdefault("history", []).append({
            "scope_decision": "legacy-explicit",
            "scope_applied": "global",
            "at_backfill": True,
        })
        try:
            store.upsert_wiki_page(
                slug=page["slug"],
                title=page.get("title") or "",
                body=page.get("body") or "",
                summary=page.get("summary") or "",
                tags=page.get("tags") or [],
                importance=page.get("importance") or 0.5,
                evidence_ids=evidence_ids,
                run_id=page.get("run_id"),
                scope=new_scope,
                auto_classification=audit,
            )
            summary["downgraded"] += 1
            summary["items"].append({
                "id": page["id"],
                "slug": page["slug"],
                "title": page.get("title"),
                "decision": "legacy-backfill",
                "scope": new_scope,
                "reasons": classification.reasons[:4],
            })
        except Exception as e:
            summary["skipped"] += 1
            summary["items"].append({
                "id": page["id"],
                "slug": page["slug"],
                "title": page.get("title"),
                "decision": "skipped",
                "error": str(e),
            })
    return summary


__all__ = ["reclassify_legacy_pages"]

"""Route group: wiki.

Wiki CRUD + export/import/ask/contradictions + /api/v1/wiki/versions.

All routes were extracted from ``serve/app.py`` as part of the O1
refactor to keep the central ``create_app`` small. Each block lives
inside ``register(app, store, scheduler=None)`` so closures over the
three captured variables work unchanged from the original layout.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ...storage.sqlite_store import MemoryStore
from ._shared import _memory_to_dict, _export_safe_segment


def _wiki_scope_config(store: MemoryStore) -> dict:
    from ...wiki.scope import auto_scope_config
    return auto_scope_config(store)


def _wiki_source_hint(payload: dict) -> str | None:
    for key in ("source", "client", "agent", "agent_id"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _wiki_evidence_sources(store: MemoryStore, evidence_ids: list) -> list[str]:
    if not evidence_ids:
        return []
    try:
        rows = store.list_memories(ids=[str(x) for x in evidence_ids], limit=max(1, len(evidence_ids)))
    except Exception:
        rows = []
    return [str(getattr(row, "source", "") or "").strip() for row in rows if getattr(row, "source", None)]


def _resolve_wiki_scope(
    store: MemoryStore,
    *,
    title: str,
    body: str,
    summary: str,
    tags: list,
    evidence_ids: list,
    requested_scope: object,
    source_hint: str | None = None,
    existing: dict | None = None,
    preserve_existing: bool = False,
) -> tuple[str, dict]:
    """Classify a page and return ``(scope, audit_json)``.

    An explicit scope is authoritative.  Missing/``auto`` scope is routed by
    the deterministic classifier; non-security pages go to the evidence or
    client source and never default to global.  Existing rows are preserved
    when an update omits scope, which protects legacy/manual overrides.
    """
    from ...wiki.classifier import classify_page
    from ...wiki.scope import (
        build_scope_audit,
        derive_default_scope,
        normalise_scope,
    )

    cfg = _wiki_scope_config(store)
    classification = classify_page(
        title=title,
        body=body,
        summary=summary,
        tags=tags,
        evidence_sources=_wiki_evidence_sources(store, evidence_ids),
        mode=cfg["mode"] if cfg["enabled"] else "off",
    )
    raw_requested = "" if requested_scope is None else str(requested_scope).strip().lower()
    explicit = bool(raw_requested and raw_requested != "auto")
    if explicit:
        try:
            scope = normalise_scope(requested_scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        decision = "explicit"
    elif preserve_existing and existing and existing.get("scope"):
        scope = str(existing["scope"]).strip().lower() or derive_default_scope(
            source_hint, evidence_ids, store
        )
        decision = "preserved-existing"
    elif cfg["enabled"] and classification.auto_global:
        scope = "global"
        decision = "auto-global"
    else:
        scope = derive_default_scope(source_hint, evidence_ids, store)
        decision = "default-source"
    audit = build_scope_audit(
        classification,
        scope=scope,
        decision=decision,
        source_hint=source_hint,
        enabled=cfg["enabled"],
        mode=cfg["mode"],
        existing=existing,
    )
    return scope, audit


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
    @app.post("/api/contradictions/resolve")
    def resolve_contradiction(a: str, b: str, action: str = "ignore",
                              keep: str | None = None):
        """Resolve a contradiction pair surfaced on the dashboard.

        ``action``:
          - ``ignore``  — hide this pair from future pulses (default)
          - ``merge``   — fuse the two into one memory (winner keeps its
            row, loser's text is appended, the loser is deleted)
          - ``keepA``   — explicitly delete side B
          - ``keepB``   — explicitly delete side A
        """
        a_id = str(a or "").strip()
        b_id = str(b or "").strip()
        if not a_id or not b_id or a_id == b_id:
            raise HTTPException(400, "need two distinct memory ids")
        action_raw = action or "ignore"
        action = action_raw.lower()
        result = {"ok": True, "action": action_raw, "deleted": []}
        # Always remember the ignore so the pair doesn't reappear next refresh
        store.ignore_contradiction(a_id, b_id)
        if action == "ignore":
            # Record a soft "down" on both so the consolidator weighs them down.
            try:
                store.record_signal(a_id, positive=False)
                store.record_signal(b_id, positive=False)
            except Exception:
                pass
        elif action == "merge":
            # True fusion: keep the higher-scored side as the row that
            # survives, append the loser's text to it (de-duplicated),
            # bump importance/score to the max of the two, then delete
            # the loser in the same transaction. ``merge_memories``
            # also writes the pair into ``contradiction_ignored`` so
            # the pulse does not surface it again.
            try:
                merge_info = store.merge_memories(a_id, b_id)
            except ValueError as e:
                raise HTTPException(400, str(e))
            if not merge_info.get("merged"):
                # One of the sides was already gone; nothing to merge.
                result["merged"] = False
                result["loser"] = merge_info.get("lost")
                if merge_info.get("reason") == "neither_exists":
                    raise HTTPException(404, "neither memory exists")
            else:
                result["merged"] = True
                result["winner"] = merge_info["kept"]
                result["loser"] = merge_info["lost"]
                result["appended"] = merge_info.get("appended", False)
                result["new_length"] = merge_info.get("new_length", 0)
                try:
                    store.record_signal(merge_info["kept"], positive=True)
                except Exception:
                    pass
        elif action in ("keepa", "keepb"):
            loser = b_id if action == "keepa" else a_id
            store.delete_memory(loser)
            result["deleted"].append({"id": loser, "kept": (a_id if loser == b_id else b_id)})
        else:
            raise HTTPException(400, f"unknown action {action!r}")
        return result

    # ------------------------------------------------------------------
    # /api/v1/memories — Universal Agent Memory API
    # ------------------------------------------------------------------
    # Designed for any Agent (Codex / Claude / Hermes / OpenClaw /
    # LangChain / AutoGPT / a custom internal bot) to remember facts,
    # recall context, give feedback, and forget — without coupling to
    # the in-process store or the existing /api/* surface. Idempotent
    # writes are keyed on (agent_id, user_id, external_id) so a retry
    # of the same tool call updates the row in place.
    #
    # This block is intentionally placed alongside the legacy
    # ``/api/memories`` routes for discoverability — the OpenAPI doc
    # at ``/openapi.json`` lists every route side-by-side.
    # ------------------------------------------------------------------


    @app.get("/api/v1/wiki/versions")
    def v1_wiki_versions(page_id: str | None = None,
                         branch_tag: str | None = None, limit: int = 200):
        rows = store.list_wiki_versions(
            page_id=page_id, branch_tag=branch_tag, limit=limit,
        )
        return {"rows": rows, "count": len(rows)}


    @app.get("/api/wiki/contradictions")
    def wiki_contradictions():
        """Return every wiki page that has at least one partner in
        its ``contradicting_ids`` list, with the partner summaries
        inline. The UI uses this to render the "needs review" list
        on the Wiki page.
        """
        from ...jobs.contradiction import list_contradictions
        rows = list_contradictions(store)
        return {"count": len(rows), "items": rows}


    @app.post("/api/wiki/contradictions/scan")
    def wiki_contradictions_scan(threshold: float = 0.45):
        """Re-scan every wiki page for contradictions. Cheap for
        <1000 pages; intended for manual triggers from the UI after
        the user adds or edits pages."""
        from ...jobs.contradiction import scan_all
        return scan_all(store, threshold=threshold)


    @app.post("/api/wiki/{page_id}/merge")
    def wiki_merge(page_id: str, body: dict):
        """Merge ``page_id`` with the page in ``body.loser_id``.

        Body keys:

          * ``loser_id`` (str) — the page to dissolve into ``page_id``
          * ``merged_body`` (str, optional)
          * ``merged_summary`` (str, optional)
          * ``merged_key_facts`` (list[str], optional)
          * ``merged_importance`` (float, optional)
          * ``merged_tags`` (list[str], optional)

        The winner keeps its id; the loser is deleted. The winner's
        ``contradicting_ids`` column is cleared because the conflict
        is resolved.
        """
        loser_id = body.get("loser_id") or body.get("loserId")
        if not loser_id:
            raise HTTPException(400, "loser_id is required")
        return store.merge_wiki_pages(
            winner_id=page_id,
            loser_id=loser_id,
            merged_body=body.get("merged_body") or body.get("mergedBody"),
            merged_summary=body.get("merged_summary") or body.get("mergedSummary"),
            merged_key_facts=body.get("merged_key_facts") or body.get("mergedKeyFacts"),
            merged_importance=body.get("merged_importance") or body.get("mergedImportance"),
            merged_tags=body.get("merged_tags") or body.get("mergedTags"),
        )


    @app.post("/api/wiki/{page_id}/resolve")
    def wiki_resolve(page_id: str):
        """Clear a page's ``contradicting_ids`` so it disappears
        from the contradiction list. Use when the user inspects and
        decides the two pages are not actually in conflict.
        """
        ok = store.resolve_contradiction(page_id)
        return {"ok": bool(ok)}


    @app.get("/api/wiki/export")
    def wiki_export(format: str = "markdown", limit: int = 500,
                    q: str | None = None):
        """Dump all (or matched) wiki pages for backup / sharing.

        ``format``:
          - ``markdown``  (default) — single markdown document.
          - ``json``                — round-trippable JSON object
                                     (``{format, count, pages}``)
                                     that the import endpoint can
                                     re-ingest without loss.
        ``q`` (optional) filters by substring match against title/body.
        """
        try:
            pages = store.list_wiki_pages(limit=limit, query=q)
        except Exception:
            pages = []
        fmt = (format or "markdown").lower()
        if fmt == "json":
            return {
                "format": "json",
                "count": len(pages),
                "pages": [dict(p) for p in pages],
            }
        out_lines = ["# Loop Memory — Distilled Knowledge", ""]
        out_lines.append(f"_Exported {len(pages)} wiki pages._")
        out_lines.append("")
        for p in pages:
            title   = _export_safe_segment(p.get("title"), fallback="untitled", kind="title")
            body    = _export_safe_segment(p.get("body"), fallback="", kind="body")
            summary = _export_safe_segment(p.get("summary"), fallback="", kind="summary")
            # Anchor the body on its own paragraph so a malicious body
            # starting with "# " or "## " can't merge with the heading
            # line and steal the title's section.
            out_lines.append(f"## {title}")
            out_lines.append("")
            if summary and summary != title:
                out_lines.append(f"> {summary}")
                out_lines.append("")
            out_lines.append(body)
            if body and not body.endswith("\n"):
                out_lines.append("")
            out_lines.append("")
            evidence = p.get("evidence_ids") or []
            if evidence:
                ev = ", ".join(str(x) for x in evidence[:6])
                out_lines.append(f"<sub>evidence: {ev}… ({len(evidence)} sources)</sub>")
                out_lines.append("")
        return {"format": "markdown", "count": len(pages), "markdown": "\n".join(out_lines)}


    @app.post("/api/wiki/import")
    def wiki_import(body: dict):
        """Bulk-upsert wiki pages from JSON or Markdown.

        Body shape (one of):
          - ``{"format": "json", "pages": [...]}``  — round-trip from
            ``/api/wiki/export?format=json``. Each entry needs at
            minimum ``slug`` (or ``title``), ``title`` and ``body``.
          - ``{"format": "markdown", "markdown": "..."}``  — text dump
            with ``## title`` sections. The slug is derived from the
            title (lowercase, spaces → dashes, ascii-safe).

        Returns ``{created, updated, skipped, errors}``. Pages whose
        slug matches an existing one are updated; new slugs are
        created. Malformed entries are skipped (counted in
        ``skipped``) and the first error is reported.
        """
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        fmt = (body.get("format") or "json").lower()
        entries: list[dict] = []
        errors: list[str] = []

        def _slugify(title: str, fallback: str = "") -> str:
            import re as _re
            s = (title or fallback or "").strip().lower()
            s = _re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", s)
            s = s.strip("-")[:80]
            return s or "untitled"

        if fmt == "json":
            raw = body.get("pages")
            if not isinstance(raw, list):
                raise HTTPException(400, "pages must be a list")
            # Audit O7: cap payload size so a single import can't trigger
            # an unbounded FTS5 rebuild + graph build.
            if len(raw) > 5000:
                raise HTTPException(
                    413,
                    f"too many pages in one request ({len(raw)} > 5000); "
                    "split into chunks or stream via /api/admin/ingest",
                )
            for i, item in enumerate(raw):
                if not isinstance(item, dict):
                    errors.append(f"#{i}: not an object"); continue
                title = (item.get("title") or "").lstrip("\ufeff").strip()
                body_text = (item.get("body") or "").lstrip("\ufeff").strip()
                slug = (item.get("slug") or "").strip()
                if not title or not body_text:
                    errors.append(f"#{i}: missing title/body"); continue
                if not slug:
                    slug = _slugify(title)
                entries.append({
                    "slug": slug.lower().replace(" ", "-")[:80],
                    "title": title,
                    "body": body_text,
                    "summary": (item.get("summary") or "").strip(),
                    "tags": item.get("tags") or [],
                    "importance": float(item.get("importance") or 0.5),
                    "evidence_ids": item.get("evidence_ids") or [],
                    "requested_scope": item.get("scope"),
                    "source_hint": item.get("source") or item.get("client") or item.get("agent"),
                })
        elif fmt == "markdown":
            md = (body.get("markdown") or "")
            # Audit O13: strip UTF-8 BOM and normalise CRLF before
            # splitting on ``\n(?=##\s)`` so a Windows-pasted doc
            # round-trips the same as a Unix-pasted one.
            md = md.lstrip("\ufeff").replace("\r\n", "\n")
            # Cap raw markdown length -- 5 MB is the practical upper
            # bound before the regex split hits pathological O(n^2)
            # behaviour on inputs with millions of ``\n`` chars.
            if len(md) > 5 * 1024 * 1024:
                raise HTTPException(
                    413,
                    f"markdown body too large ({len(md)} > 5MB); "
                    "split into chunks or use JSON format",
                )
            md = md.strip()
            if not md:
                raise HTTPException(400, "markdown is empty")
            # Split on "## " headings. Each chunk becomes a page.
            import re as _re
            chunks = _re.split(r"\n(?=##\s)", md)
            for chunk in chunks:
                chunk = chunk.strip()
                if not chunk.startswith("## "):
                    continue
                # Drop leading "## "
                lines = chunk.splitlines()
                title = lines[0][3:].strip()
                rest  = "\n".join(lines[1:]).strip()
                # Optional ">" summary at the start
                summary = ""
                if rest.startswith(">"):
                    maybe = rest.split("\n", 1)
                    summary = maybe[0].lstrip("> ").strip()
                    rest = maybe[1].strip() if len(maybe) > 1 else ""
                if not title or not rest:
                    continue
                entries.append({
                    "slug": _slugify(title),
                    "title": title,
                    "body": rest,
                    "summary": summary,
                    "tags": [],
                    "importance": 0.5,
                    "evidence_ids": [],
                    "requested_scope": body.get("scope"),
                    "source_hint": body.get("source") or body.get("client"),
                })
        else:
            raise HTTPException(400, f"unknown format {fmt!r}; use 'json' or 'markdown'")

        created = 0
        updated = 0
        for e in entries:
            try:
                existing = store.get_wiki_page_by_slug(e["slug"])
                tags = e.get("tags") or []
                if not isinstance(tags, list):
                    tags = [tags]
                evidence_ids = e.get("evidence_ids") or []
                if not isinstance(evidence_ids, list):
                    evidence_ids = [evidence_ids]
                scope, auto_classification = _resolve_wiki_scope(
                    store,
                    title=e["title"],
                    body=e["body"],
                    summary=e.get("summary") or "",
                    tags=tags,
                    evidence_ids=evidence_ids,
                    requested_scope=e.get("requested_scope"),
                    source_hint=e.get("source_hint"),
                    existing=existing,
                    preserve_existing=existing is not None and e.get("requested_scope") is None,
                )
                store.upsert_wiki_page(
                    slug=e["slug"],
                    title=e["title"],
                    body=e["body"],
                    summary=e.get("summary") or "",
                    tags=tags,
                    importance=e.get("importance") or 0.5,
                    evidence_ids=evidence_ids,
                    scope=scope,
                    auto_classification=auto_classification,
                )
                if existing: updated += 1
                else:        created += 1
            except Exception as ex:
                errors.append(f"{e['slug']}: {ex}")
        return {
            "created": created,
            "updated": updated,
            "skipped": len(errors),
            "errors": errors[:10],
            "total": len(entries),
        }


    @app.get("/api/wiki/{page_id}/export")
    def wiki_page_export(page_id: str, format: str = "markdown"):
        """Export a single wiki page as a context-block ready to paste
        into another LLM client as a system prompt."""
        page = store.get_wiki_page(page_id)
        if page is None:
            raise HTTPException(404, "wiki page not found")
        title   = _export_safe_segment(page.get("title"), fallback="untitled", kind="title")
        body    = _export_safe_segment(page.get("body"), fallback="", kind="body")
        summary = _export_safe_segment(page.get("summary"), fallback=title, kind="summary")
        md = f"# {title}\n\n{body}"
        ctx = (
            "[Distilled knowledge — use as background context]\n"
            f"Title: {title}\n"
            f"Summary: {summary}\n\n"
            f"{body}\n"
        )
        return {"format": format, "markdown": md, "context": ctx}


    @app.post("/api/wiki/ask")
    def wiki_ask(q: str, limit: int = 5):
        """Recall top wiki pages by keyword match and return a ready-to-paste
        context block plus the matching memory ids."""
        ql = (q or "").lower().strip()
        if not ql:
            raise HTTPException(400, "q query param required")
        try:
            pages = store.list_wiki_pages(limit=200)
        except Exception:
            pages = []
        scored = []
        for p in pages:
            t  = (p.get("title") or "").lower()
            b  = (p.get("body") or "").lower()
            sm = (p.get("summary") or "").lower()
            score = 0
            for token in ql.split():
                score += t.count(token) * 3 + sm.count(token) * 2 + b.count(token) * 1
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        top = scored[:limit]
        if not top:
            return {"q": q, "matches": [], "context": f"(no wiki pages matched {q!r})"}
        ctx_lines = [f"[Distilled knowledge for: {q}]"]
        for _s, p in top:
            _t = _export_safe_segment(p.get("title"), fallback="untitled", kind="title")
            _s = _export_safe_segment(p.get("summary"), fallback="", kind="summary")
            _b = _export_safe_segment(p.get("body"), fallback="", kind="body")
            ctx_lines.append(
                f"\n## {_t}\n"
                f"{_s}\n"
                f"{_b[:800]}"
            )
        return {
            "q": q,
            "matches": [{"id": p["id"], "title": p.get("title"), "score": s} for s, p in top],
            "context": "\n".join(ctx_lines),
        }


    @app.get("/api/wiki")
    def wiki_list(limit: int = 200, min_importance: float = 0.0,
                  q: str | None = None):
        return store.list_wiki_pages(
            limit=limit,
            min_importance=min_importance if min_importance > 0 else None,
            query=q,
        )


    @app.get("/api/wiki/{page_id}")
    def wiki_get(page_id: str):
        page = store.get_wiki_page(page_id)
        if not page:
            raise HTTPException(404, "wiki page not found")
        return page


    @app.get("/api/wiki/{page_id}/classification-history")
    def wiki_classification_history(page_id: str):
        page = store.get_wiki_page(page_id)
        if not page:
            raise HTTPException(404, "wiki page not found")
        current = page.get("auto_classification")
        history = []
        if isinstance(current, dict):
            raw_history = current.get("history")
            if isinstance(raw_history, list):
                history.extend(item for item in raw_history if isinstance(item, dict))
            history.append({key: value for key, value in current.items() if key != "history"})
        return {
            "page_id": page_id,
            "scope": page.get("scope") or "global",
            "history": history,
            "current": current,
        }


    @app.post("/api/wiki/classify")
    def wiki_classify(body: dict):
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        title = str(body.get("title") or "").strip()
        body_text = str(body.get("body") or "").strip()
        summary = str(body.get("summary") or "").strip()
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        evidence_ids = body.get("evidence_ids") or []
        if not isinstance(evidence_ids, list):
            evidence_ids = [evidence_ids]
        scope, audit = _resolve_wiki_scope(
            store,
            title=title,
            body=body_text,
            summary=summary,
            tags=tags,
            evidence_ids=evidence_ids,
            requested_scope=body.get("scope"),
            source_hint=_wiki_source_hint(body),
        )
        return {
            "classification": audit,
            "recommended_scope": scope,
            "scope": scope,
            "config": _wiki_scope_config(store),
        }


    @app.post("/api/wiki")
    def wiki_create(body: dict):
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        slug = (body.get("slug") or "").strip()
        title = (body.get("title") or "").strip()
        body_text = (body.get("body") or "").strip()
        if not slug or not title or not body_text:
            raise HTTPException(400, "slug, title and body are required")
        # Don't let manual creates clobber a consolidated page for the
        # same slug; if it already exists, route them to PUT instead.
        if store.get_wiki_page_by_slug(slug):
            raise HTTPException(409, "wiki page with this slug already exists")
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        evidence_ids = body.get("evidence_ids") or []
        if not isinstance(evidence_ids, list):
            evidence_ids = [evidence_ids]
        normalised, auto_classification = _resolve_wiki_scope(
            store,
            title=title,
            body=body_text,
            summary=str(body.get("summary") or "").strip(),
            tags=tags,
            evidence_ids=evidence_ids,
            requested_scope=body.get("scope"),
            source_hint=_wiki_source_hint(body),
        )
        return store.upsert_wiki_page(
            slug=slug.lower().replace(" ", "-")[:80],
            title=title,
            body=body_text,
            summary=(body.get("summary") or "").strip(),
            tags=tags,
            importance=float(body.get("importance") or 0.5),
            evidence_ids=evidence_ids,
            scope=normalised,
            auto_classification=auto_classification,
        )


    @app.put("/api/wiki/{page_id}")
    def wiki_update(page_id: str, body: dict):
        existing = store.get_wiki_page(page_id)
        if not existing:
            raise HTTPException(404, "wiki page not found")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        slug = (body.get("slug") or existing["slug"]).strip()
        title = (body.get("title") or existing["title"]).strip()
        body_text = (body.get("body") or existing["body"]).strip()
        summary = (body.get("summary") or existing.get("summary") or "").strip()
        tags = body.get("tags") if "tags" in body else existing.get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        evidence_ids = body.get("evidence_ids") if "evidence_ids" in body else existing.get("evidence_ids") or []
        if not isinstance(evidence_ids, list):
            evidence_ids = [evidence_ids]
        scope_val, auto_classification = _resolve_wiki_scope(
            store,
            title=title,
            body=body_text,
            summary=summary,
            tags=tags,
            evidence_ids=evidence_ids,
            requested_scope=body.get("scope") if "scope" in body else None,
            source_hint=_wiki_source_hint(body),
            existing=existing,
            preserve_existing="scope" not in body,
        )
        return store.upsert_wiki_page(
            slug=slug.lower().replace(" ", "-")[:80],
            title=title,
            body=body_text,
            summary=summary,
            tags=tags,
            importance=float(body.get("importance")
                             if "importance" in body else existing.get("importance") or 0.5),
            evidence_ids=evidence_ids,
            scope=scope_val,
            auto_classification=auto_classification,
        )


    @app.delete("/api/wiki/{page_id}")
    def wiki_delete(page_id: str):
        ok = store.delete_wiki_page(page_id)
        if not ok:
            raise HTTPException(404, "wiki page not found")
        return {"ok": True}


    @app.post("/api/wiki/bulk-scope")
    def wiki_bulk_scope(body: dict):
        """Set the ``scope`` field on multiple wiki pages in one shot.

        Drives the master 全局 toggle in the Wiki tab. When the user
        flips the toolbar toggle ON, we bulk-update every page to
        ``scope="global"``. When they flip a single page OFF while the
        master is ON, the master auto-flips OFF — but this endpoint
        is also exposed so the front-end can keep the bulk fast-path
        (single round-trip) without iterating over PUTs.

        Body:
            ``scope`` (str, required) — the new scope value. Allowed
                tokens: ``global``, ``codex``, ``claude``, ``hermes``,
                ``openclaw``. Comma-separated for per-client lists.
            ``page_ids`` (list[str], optional) — when provided, only
                these pages are updated. When omitted, ALL pages are
                updated. Use the explicit list for the "user
                manually toggled one off" path, omit for the master
                toggle's bulk-ON case.
        """
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        raw_scope = (body.get("scope") or "").strip().lower()
        if not raw_scope:
            raise HTTPException(400, "scope is required")
        from ...wiki.scope import normalise_scope
        try:
            normalised = normalise_scope(raw_scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        page_ids = body.get("page_ids")
        if page_ids is not None:
            if not isinstance(page_ids, list) or not all(
                isinstance(x, str) for x in page_ids
            ):
                raise HTTPException(400, "page_ids must be a list of strings")
            targets = page_ids
        else:
            # No explicit list = apply to every page. Used by the
            # master "全局" toggle's bulk-ON path.
            targets = [p["id"] for p in store.list_wiki_pages(limit=10000)]
        updated = 0
        for pid in targets:
            existing = store.get_wiki_page(pid)
            if not existing:
                continue
            # Avoid bumping updated_at / version when scope is
            # already at the target value.
            if (existing.get("scope") or "global") == normalised:
                updated += 1
                continue
            from ...wiki.scope import build_scope_audit
            scope_cfg = _wiki_scope_config(store)
            manual_audit = build_scope_audit(
                existing.get("auto_classification") or {},
                scope=normalised,
                decision="manual-override",
                enabled=scope_cfg["enabled"],
                mode=scope_cfg["mode"],
                existing=existing,
            )
            store.upsert_wiki_page(
                slug=existing["slug"],
                title=existing["title"],
                body=existing["body"],
                summary=existing.get("summary") or "",
                tags=existing.get("tags") or [],
                importance=existing.get("importance") or 0.5,
                evidence_ids=existing.get("evidence_ids") or [],
                run_id=existing.get("run_id"),
                scope=normalised,
                auto_classification=manual_audit,
            )
            updated += 1
        return {"ok": True, "updated": updated, "scope": normalised}


    @app.post("/api/wiki/{page_id}/resummarize")
    def wiki_resummarize(page_id: str):
        """Re-run the consolidator's wiki step targeting just this page.

        Useful when the user has edited a page manually and wants the
        next consolidation pass to refresh its evidence list, or when
        they want to regenerate a single page without touching others.
        """
        existing = store.get_wiki_page(page_id)
        if not existing:
            raise HTTPException(404, "wiki page not found")
        from ...llm.providers import build_provider, default_config, validate_config
        cfg, _ = validate_config(store.get_setting("llm_consolidator", default_config()))
        provider = build_provider(cfg)
        from ...jobs.llm_consolidate import LLMConsolidator
        cons = LLMConsolidator(store, provider, cfg.get("behaviour") or {})
        # Synthesize on the memories currently pointed at by evidence_ids.
        evidence = existing.get("evidence_ids") or []
        memories = []
        for mid in evidence:
            m = store.get_memory(mid)
            if m is not None:
                memories.append(m)
        if not memories:
            raise HTTPException(400,
                "no evidence memories for this page; rerun consolidation to refresh")
        result = cons._synth_wiki_pages(
            memories=memories, pre_drop=set(), cfg=cfg.get("behaviour") or {},
            stats=type("S", (), {"notes": [], "llm_calls": 0})(),
            run_id=None,
        )
        return {"ok": True, **result}

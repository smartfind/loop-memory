"""Route group: memories.

Memory CRUD + recall + v1/memories + v1/recall + /api/memories/page.

All routes were extracted from ``serve/app.py`` as part of the O1
refactor to keep the central ``create_app`` small. Each block lives
inside ``register(app, store, scheduler=None)`` so closures over the
three captured variables work unchanged from the original layout.
"""
from __future__ import annotations

from typing import Any, Optional

import re
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ...storage.sqlite_store import MemoryStore
from ._shared import _memory_to_dict, _export_safe_segment


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
    @app.get("/api/memories/{mid}/score")
    def memory_score_components(mid: str):
        """Return the v2 score breakdown (importance / recency / usage /
        feedback) so the UI can render a radar / breakdown chart per
        memory."""
        m = store.get_memory(mid)
        if m is None:
            raise HTTPException(404, "memory not found")
        sig = store.get_signal(mid)
        comps = MemoryStore.score_components(
            importance=m.importance or 0.0,
            created_at=m.created_at,
            recall_count=sig["recall_count"],
            last_recalled_at=sig["last_recalled_at"],
            positive=sig["positive"],
            negative=sig["negative"],
            half_life_days=30.0,
        )
        return {"memory_id": mid, **comps}


    @app.get("/api/memories")
    def memories(
        source: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        min_score: float | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        limit: int = 200,
    ):
        if source:
            rows = store.list_memories(
                kind=kind,
                min_score=min_score,
                since=since,
                until=until,
                query=q,
                limit=limit * 4,
            )
            rows = [r for r in rows if (r.source == source)]
            rows = rows[:limit]
        else:
            rows = store.list_memories(
                session_id=session_id,
                kind=kind,
                min_score=min_score,
                since=since,
                until=until,
                query=q,
                limit=limit,
            )
        # If a session_id was passed, drop the source-coded full scan and
        # apply the session filter on top of either branch (catches the
        # ``source=`` branch where list_memories was called without
        # session_id so the filter didn't propagate).
        if session_id:
            rows = [r for r in rows if r.session_id == session_id]
        return [_memory_to_dict(m) for m in rows]


    @app.get("/api/memories/page")
    def memories_page(
        before_id: str | None = None,
        after_id: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        min_score: float | None = None,
        limit: int = 100,
    ):
        """Cursor-paginated memory list (audit O11).

        Returns ``{rows: [...], next_before_id, next_after_id}`` so
        the UI can request additional pages with a stable, opaque
        cursor (a memory id) instead of an offset that breaks on
        concurrent inserts.

        Pass ``before_id`` to fetch rows older than the boundary;
        pass ``after_id`` to fetch rows newer. Omit both to start
        from the most-recent page.
        """
        from fastapi import HTTPException as _HE
        try:
            rows, next_before_id, next_after_id = store.list_memories_cursor(
                limit=min(max(limit, 1), 500),
                before_id=before_id,
                after_id=after_id,
                session_id=session_id,
                source=source,
                kind=kind,
                min_score=min_score,
            )
        except ValueError as e:
            raise _HE(404, str(e))
        return {
            "rows": [_memory_to_dict(m) for m in rows],
            "next_before_id": next_before_id,
            "next_after_id": next_after_id,
            "count": len(rows),
        }


    @app.post("/api/memories/{mid}/feedback")
    def memory_feedback(mid: str, value: str = "up", reason: str | None = None):
        """Record 👍/👎 feedback on a memory.

        ``value`` is one of:
          - ``up``    — bump positive counter (user kept / liked this)
          - ``down``  — bump negative counter (user rejected this)
          - ``ignore``— same as down + soft-delete the memory
        """
        v = (value or "").strip().lower()
        if v not in ("up", "down", "ignore"):
            raise HTTPException(400, f"value must be up|down|ignore (got {value!r})")
        try:
            store.record_signal(mid, positive=(v == "up"))
        except Exception as e:
            raise HTTPException(500, f"signal failed: {e}")
        deleted = 0
        if v == "ignore":
            deleted = store.delete_memory(mid)
        return {"ok": True, "value": v, "deleted": deleted, "reason": reason}


    @app.post("/api/v1/memories")
    def v1_create_memory(body: dict):
        """Idempotent remember(). Body: {text, kind?, importance?,
        tags?, source?, session_id?, external_id?, agent_id?, user_id?,
        ttl?}. When ``external_id`` is set, the (agent_id, user_id,
        external_id) tuple must be unique; re-pushing updates the
        row in place.
        """
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        kind = (body.get("kind") or "fact").strip()
        importance = body.get("importance")
        try:
            importance_f = float(importance) if importance is not None else 0.5
        except (TypeError, ValueError):
            raise HTTPException(400, f"importance must be a number, got {importance!r}")
        importance_f = max(0.0, min(1.0, importance_f))
        ttl = body.get("ttl")
        try:
            ttl_f = float(ttl) if ttl is not None else None
        except (TypeError, ValueError):
            raise HTTPException(400, f"ttl must be a number, got {ttl!r}")
        ext = body.get("external_id")
        if ext is not None:
            ext = str(ext).strip() or None
        _ca = body.get("created_at")
        try:
            _ca_f = float(_ca) if _ca is not None else None
        except (TypeError, ValueError):
            raise HTTPException(400, "created_at must be a number, got " + repr(_ca))
        stored = store.upsert_memory(
            kind=kind,
            text=text,
            importance=importance_f,
            source=body.get("source"),
            session_id=body.get("session_id"),
            tags=list(body.get("tags") or []),
            agent_id=body.get("agent_id"),
            user_id=body.get("user_id"),
            external_id=ext,
            ttl=ttl_f,
        )
        return _memory_to_dict(stored)


    @app.post("/api/v1/memories:batch")
    def v1_create_memories_batch(body: dict):
        """Bulk remember(). Body: {items: [ {...same as single...}, ... ]}.

        Returns a per-item list with either the created memory or an
        error string. The HTTP status is always 200 so a single
        malformed item doesn't fail the whole batch — callers should
        inspect each ``items[].error`` field.
        """
        items = body.get("items") or []
        if not isinstance(items, list):
            raise HTTPException(400, "items must be a list")
        if len(items) > 500:
            raise HTTPException(400, "batch size capped at 500 per request")
        out = []
        for raw in items:
            try:
                if not isinstance(raw, dict):
                    raise ValueError("item must be an object")
                text = (raw.get("text") or "").strip()
                if not text:
                    raise ValueError("text is required")
                ext = raw.get("external_id")
                if ext is not None:
                    ext = str(ext).strip() or None
                importance = raw.get("importance")
                try:
                    importance_f = float(importance) if importance is not None else 0.5
                except (TypeError, ValueError):
                    raise ValueError(f"importance must be a number, got {importance!r}")
                importance_f = max(0.0, min(1.0, importance_f))
                _ca = raw.get("created_at")
                try:
                    _ca_f = float(_ca) if _ca is not None else None
                except (TypeError, ValueError):
                    raise ValueError("created_at must be a number, got " + repr(_ca))
                stored = store.upsert_memory(
                    kind=(raw.get("kind") or "fact").strip(),
                    text=text,
                    importance=importance_f,
                    source=raw.get("source"),
                    session_id=raw.get("session_id"),
                    tags=list(raw.get("tags") or []),
                    agent_id=raw.get("agent_id"),
                    user_id=raw.get("user_id"),
                    external_id=ext,
                    created_at=_ca_f,
                )
                out.append(_memory_to_dict(stored))
            except Exception as e:
                out.append({"error": str(e), "input": raw})
        return {"items": out, "count": len(out)}


    @app.get("/api/v1/memories")
    def v1_list_memories(
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        external_id: str | None = None,
        min_score: float | None = None,
        q: str | None = None,
        limit: int = 50,
    ):
        """List memories with simple filters. ``external_id`` is exact-
        match; combine with ``agent_id`` / ``user_id`` to address a
        specific row pushed by an external Agent."""
        rows = store.list_memories(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            kind=kind,
            external_id=external_id,
            min_score=min_score,
            query=q,
            limit=min(max(limit, 1), 500),
        )
        return {"memories": [_memory_to_dict(m) for m in rows], "count": len(rows)}


    @app.get("/api/v1/recall")
    def v1_recall(
        q: str,
        limit: int = 8,
        include: str = "memories,wiki,entities",
        source: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        mode: str = "hybrid",
    ):
        """Unified recall filtered to a single Agent namespace.

        ``agent_id`` and ``user_id`` are optional narrowing filters
        applied on top of the existing BM25+semantic+entity hybrid
        pipeline. Memories with no ``agent_id`` set are treated as
        "global" and visible to every caller (matches the SDK
        semantics).
        """
        wanted = tuple(s.strip() for s in include.split(",") if s.strip())
        if not wanted:
            wanted = ("memories", "wiki", "entities")
        if mode == "legacy" or not hasattr(store, "recall_hybrid"):
            r = store.recall(
                q,
                limit=limit,
                include=wanted,
                bump_signals=True,
                source=source,
            )
        else:
            r = store.recall_hybrid(
                q, limit=limit, include=wanted,
                bump_signals=True, source=source, level=1,
            )
        if agent_id is not None or user_id is not None:
            def _own(m: dict) -> bool:
                ag = m.get("agent_id")
                ur = m.get("user_id")
                # Global memories (no agent_id) are visible to all.
                if ag is None and ur is None:
                    return True
                if agent_id is not None and ag not in (None, agent_id):
                    return False
                if user_id is not None and ur not in (None, user_id):
                    return False
                return True
            r["memories"] = [m for m in r.get("memories", []) if _own(m)]
        return {
            "query": q,
            "tokens": r.get("tokens", []),
            "memories": r.get("memories", []),
            "wiki": r.get("wiki", []),
            "entities": r.get("entities", []),
            "mode": mode,
            "source": source,
            "temporal_intent": r.get("temporal_intent", "any"),
            "temporal_confidence": r.get("temporal_confidence", 0.0),
        }


    @app.post("/api/v1/memories/{mid}/feedback")
    def v1_feedback(mid: str, body: dict):
        """Record 👍/👎 on a memory by id. Body: {value, reason?}."""
        value = (body.get("value") or "up").strip().lower()
        if value not in ("up", "down", "ignore"):
            raise HTTPException(400, "value must be up|down|ignore")
        row = store.get_memory(mid)
        if row is None:
            raise HTTPException(404, "memory not found")
        try:
            store.record_signal(mid, positive=(value == "up"))
        except Exception as e:
            raise HTTPException(500, f"signal failed: {e}")
        deleted = 0
        if value == "ignore":
            deleted = store.delete_memory(mid)
        return {"ok": True, "value": value, "deleted": deleted}


    @app.post("/api/v1/memories/feedback")
    def v1_feedback_by_external(body: dict):
        """Record 👍/👎 on a memory addressed by ``(agent_id, user_id,
        external_id)``. Body: {external_id, agent_id?, user_id?,
        value, reason?}. Returns 404 when no memory matches.
        """
        ext = (body.get("external_id") or "").strip()
        if not ext:
            raise HTTPException(400, "external_id is required")
        agent_id = body.get("agent_id")
        user_id = body.get("user_id")
        row = store.find_memory_by_external_id(agent_id or "", ext, user_id=user_id)
        if row is None:
            raise HTTPException(404, "no memory matches that external_id")
        value = (body.get("value") or "up").strip().lower()
        if value not in ("up", "down", "ignore"):
            raise HTTPException(400, "value must be up|down|ignore")
        store.record_signal(row.id, positive=(value == "up"))
        deleted = 0
        if value == "ignore":
            deleted = store.delete_memory(row.id)
        return {"ok": True, "memory_id": row.id, "value": value, "deleted": deleted}


    @app.delete("/api/v1/memories")
    def v1_delete_memory_by_external(
        external_id: str,
        agent_id: str | None = None,
        user_id: str | None = None,
    ):
        """Delete a memory by its external triple. Returns 404 if no
        row matches so the Agent can retry with a corrected tuple."""
        if not external_id:
            raise HTTPException(400, "external_id is required")
        row = store.find_memory_by_external_id(agent_id or "", external_id, user_id=user_id)
        if row is None:
            raise HTTPException(404, "no memory matches that external_id")
        deleted = store.delete_memory(row.id)
        return {"deleted": deleted, "memory_id": row.id}

    # ------------------------------------------------------------------
    # /api/v1 — graph, cognitive sleep, export/import/fork (v7)
    # ------------------------------------------------------------------
    # Closes the gaps the article called out: 3D adaptive scoring,
    # semantic graph edges, cognitive audit, white-box MEMORY.md
    # bundles, and a fork primitive so any Agent can snapshot the
    # wiki and `git revert` later.


    @app.delete("/api/memories/{mid}")
    def delete_memory(mid: str):
        n = store.delete_memory(mid)
        if n == 0:
            raise HTTPException(404, "memory not found")
        return {"deleted": n}


    @app.get("/api/memories/{mid}", name="memory_drill_down")
    def memory_drill_down(mid: str):
        """L2 drill-down for a single memory row (full text)."""
        mem = store.get_memory(mid)
        if not mem:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="memory not found")
        return {
            "id": mem.id,
            "kind": "memory",
            "text": mem.text,
            "importance": mem.importance,
            "score": mem.score,
            "source": mem.source,
            "tags": mem.tags,
            "created_at": mem.created_at,
            "updated_at": mem.updated_at,
        }


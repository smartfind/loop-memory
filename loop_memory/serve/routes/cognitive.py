"""Route group: cognitive.

v1/cognitive sleep / audit / revert.

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
    @app.post("/api/v1/cognitive/sleep")
    def v1_cognitive_sleep(body: dict):
        from ...jobs.cognitive import cognitive_sleep
        rpt = cognitive_sleep(
            store,
            apply=bool(body.get("apply", False)),
            stale_days=int(body.get("stale_days", 90)),
            min_score=float(body.get("min_score", 0.2)),
            min_importance=float(body.get("min_importance", 0.3)),
            low_value=float(body.get("low_value", 0.3)),
            merge_threshold=float(body.get("merge_threshold", 0.92)),
            limit=int(body.get("limit", 1000)),
            record_audit=bool(body.get("record_audit", True)),
        )
        return rpt.to_dict()


    @app.get("/api/v1/cognitive/audit")
    def v1_cognitive_audit(kind: str | None = None, action: str | None = None,
                            limit: int = 200):
        rows = store.list_audit(kind=kind, action=action, limit=limit)
        return {"rows": rows, "count": len(rows)}


    @app.post("/api/v1/cognitive/audit/revert")
    def v1_cognitive_audit_revert(body: dict):
        aid = (body.get("id") or "").strip()
        if not aid:
            raise HTTPException(400, "id is required")
        store.record_audit(
            kind="revert", action="reverted",
            target_kind="memory", target_id=aid,
            reason="user marked audit row as reverted",
        )
        return {"ok": True}



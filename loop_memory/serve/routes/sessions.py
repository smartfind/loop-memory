"""Route group: sessions.

Session listing + per-source counts.

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
    @app.get("/api/sessions")
    def sessions(source: str | None = None, limit: int = 100):
        return [
            _session_to_dict(s)
            for s in store.list_sessions(source=source, limit=limit)
        ]


    @app.get("/api/sessions/counts")
    def session_counts():
        """Per-source session + memory counts, for the sidebar filter UI."""
        try:
            with store._conn() as c:
                rows = c.execute(
                    """SELECT source,
                              COUNT(*) AS sessions,
                              COALESCE(SUM(message_count), 0) AS turns
                       FROM sessions
                       GROUP BY source"""
                ).fetchall()
        except Exception:
            rows = []
        out = {"all": {"sessions": 0, "turns": 0}, "by_source": {}}
        for r in rows:
            src = r["source"] or "unknown"
            out["by_source"][src] = {
                "sessions": r["sessions"],
                "turns": r["turns"],
            }
            out["all"]["sessions"] += r["sessions"]
            out["all"]["turns"] += r["turns"]
        return out


    @app.get("/api/sessions/{session_id}/memories")
    def session_memories(session_id: str, limit: int = 1000):
        return [
            _memory_to_dict(m)
            for m in store.list_memories(session_id=session_id, limit=limit)
        ]


    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str):
        n = store.delete_session(sid)
        return {"deleted": n}



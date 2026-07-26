"""Route group: export.

v1 export + v1 import.

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


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
    @app.post("/api/v1/export")
    def v1_export(body: dict):
        from ...export import export_bundle
        out_dir = (body.get("out_dir") or "").strip()
        if not out_dir:
            raise HTTPException(400, "out_dir is required")
        try:
            r = export_bundle(
                store, out_dir,
                agent_id=body.get("agent_id") or None,
                user_id=body.get("user_id") or None,
                scope=body.get("scope") or "global",
                min_importance=float(body.get("min_importance") or 0.0),
            )
        except Exception as e:
            raise HTTPException(500, f"export failed: {e}")
        return r.to_dict()


    @app.post("/api/v1/import")
    def v1_import(body: dict):
        from ...export import import_bundle
        in_dir = (body.get("in_dir") or "").strip()
        if not in_dir:
            raise HTTPException(400, "in_dir is required")
        try:
            r = import_bundle(
                store, in_dir,
                agent_id=body.get("agent_id") or None,
                user_id=body.get("user_id") or None,
                dry_run=bool(body.get("dry_run", False)),
            )
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            raise HTTPException(500, f"import failed: {e}")
        return r.to_dict()



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


def register(app, store, scheduler=None):
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
    # Audit 2026-09-23: cache the live DB path so each route can hand
    # it to ``_safe_resolve_path`` (rule 1 refuses writes to the live
    # DB). Pre-computing here keeps the per-request path cheap.
    _live_db_path = str(store.path)
    from ._shared import _safe_resolve_path as _safe_path
    @app.post("/api/v1/export")
    def v1_export(body: dict):
        from ...export import export_bundle
        out_dir = (body.get("out_dir") or "").strip()
        if not out_dir:
            raise HTTPException(400, "out_dir is required")
        try:
            safe_dir = _safe_path(out_dir, kind="write", live_db_path=_live_db_path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            r = export_bundle(
                store, str(safe_dir),
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
            safe_dir = _safe_path(in_dir, kind="read", live_db_path=_live_db_path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            r = import_bundle(
                store, str(safe_dir),
                agent_id=body.get("agent_id") or None,
                user_id=body.get("user_id") or None,
                dry_run=bool(body.get("dry_run", False)),
            )
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            raise HTTPException(500, f"import failed: {e}")
        return r.to_dict()

    @app.post("/api/snapshot")
    def snapshot_create(body: dict):
        """Write a portable SQLite snapshot of the live store.

        Body: ``{"out_path": "/path/to/file.memory.sqlite"}``. Returns
        the summary dict from ``loop_memory.storage.snapshot.snapshot``.
        Audit 2026-09-23: ``out_path`` is rejected if it points at the
        live store or a system / sensitive directory.
        """
        from ...storage.snapshot import snapshot as _snapshot
        out = (body.get("out_path") or "").strip()
        if not out:
            raise HTTPException(400, "out_path is required")
        try:
            safe_out = _safe_path(out, kind="write", live_db_path=_live_db_path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            return _snapshot(store, str(safe_out))
        except Exception as e:
            raise HTTPException(500, f"snapshot failed: {e}")

    @app.post("/api/snapshot/restore")
    def snapshot_restore(body: dict):
        """Re-hydrate a portable SQLite snapshot into the live store.

        Body: ``{"in_path": "/path/to/file.memory.sqlite"}``. Returns
        the summary dict from ``loop_memory.storage.snapshot.restore``.
        Refuses wrong-magic files with a 400 so the dashboard surfaces
        the error clearly instead of corrupting the live store.
        Audit 2026-09-23: ``in_path`` is rejected if it points at the
        live store or a system / sensitive directory.
        """
        from ...storage.snapshot import restore as _restore
        in_path = (body.get("in_path") or "").strip()
        if not in_path:
            raise HTTPException(400, "in_path is required")
        try:
            safe_in = _safe_path(in_path, kind="read", live_db_path=_live_db_path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            return _restore(store, str(safe_in))
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"restore failed: {e}")

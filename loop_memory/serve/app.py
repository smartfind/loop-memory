"""FastAPI app — the local web UI for Loop Memory.

Endpoints:

  GET  /                       index HTML
  GET  /static/*               static assets
  GET  /api/stats              counters
  GET  /api/sessions           list sessions
  GET  /api/sessions/{id}/memories
  GET  /api/memories           list with filters: source, kind, since, until,
                               min_score, q, limit
  POST /api/memories/{id}/delete
  POST /api/sessions/{id}/delete
  POST /api/admin/rescore
  POST /api/admin/gc
  POST /api/admin/consolidate
  POST /api/admin/ingest       body: {source, path?}

The framework stays zero-dependency; FastAPI / uvicorn live behind the
``serve`` extra. Without them, you can drive the same store from the CLI.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from ..storage.sqlite_store import MemoryStore
from ..cli._common import DEFAULT_DB
from .handlers import (
    llm_test,
    memory_to_dict,
    pipeline_dashboard,
    pipeline_stage_items,
    session_to_dict,
)

def _hint_for_llm_error(provider: str, status: int, code: str, msg: str) -> str:
    """Module-level copy of handlers._hint_for_llm_error so the weekly
    report endpoint can produce structured hints without a circular import.
    See handlers.py for the full mapping logic.
    """
    from .handlers import _hint_for_llm_error as _impl
    return _impl(provider, status, code, msg)

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

def _export_safe_segment(text: str, *, fallback: str = "", kind: str = "line") -> str:
    """Sanitise a user-controlled wiki field for the markdown export.

    Tiles a malicious title like ``"My\n## Pwned"`` would create a
    second ``## Pwned`` heading on re-import, smuggling arbitrary
    markdown into the next consumer (open redirect if rendered as
    HTML, structural corruption if re-imported).

    The real fix is downstream rendering as Markdown (with a
    allow-list sanitizer). Until that exists, this helper:
      * collapses any whitespace run into single spaces for
        ``title`` / ``summary`` (they're not multi-line fields),
      * strips a leading ``## `` so it cannot reopen a section,
      * keeps ``body`` content untouched (body *is* markdown — it
        legitimately contains ``## `` sub-headings).

    ``kind`` is either ``"title"`` / ``"summary"`` / ``"body"``.
    """
    if not text:
        return fallback
    s = text
    if kind in ("title", "summary"):
        # Collapse any whitespace run (incl. newlines) into a single
        # space and trim. Removes structural-injection vectors.
        s = re.sub(r"\s+", " ", s).strip()
        # Strip a leading "## " (and any case variant) that would
        # reopen a section when re-imported.
        s = re.sub(r"^#+\s*", "", s).strip()
    return s or fallback

def _split_think(text: str) -> tuple[str, str]:
    """Pull ``<think>...</think>`` blocks out of an LLM reply.

    Some models (notably MiniMax-M2.7) emit a thinking trace before the
    user-visible answer. We expose it under a separate field so the UI
    can render the visible report as Markdown while still letting power
    users peek at the chain-of-thought in a collapsed <details>.
    """
    if not text:
        return "", ""
    thinking_parts = _THINK_RE.findall(text)
    cleaned = _THINK_RE.sub("", text).strip()
    thinking = "\n\n".join(p.strip() for p in thinking_parts if p.strip())
    return cleaned, thinking

def create_app(store: MemoryStore, static_dir: Path | None = None, scheduler=None):
    """Build a FastAPI app wired to the given ``MemoryStore``.

    Imports are local so the core stays importable without FastAPI.
    """
    try:
        from fastapi import FastAPI, HTTPException  # type: ignore
        from fastapi.responses import FileResponse, JSONResponse  # type: ignore
        from fastapi.staticfiles import StaticFiles  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "FastAPI is not installed; run `pip install loop-memory[serve]`"
        ) from e

    app = FastAPI(title="Loop Memory", version="0.2.0")
    app.state.scheduler = scheduler

    # Static-asset cache policy. The default Starlette StaticFiles uses
    # ``Cache-Control: max-age=3600``, which means Safari will happily
    # serve a stale i18n / JS / CSS file for an hour after we push a
    # fix — users click "reload", see no change, and report "the fix
    # didn't take". Override per-extension so:
    #   * i18n json files: always revalidate (no-cache)
    #   * js / css / html:  short max-age with revalidate (5 min)
    # The browser still avoids re-downloading when the file is
    # unchanged thanks to the ETag / If-Modified-Since 304 path.
    import re as _re
    _CACHE_LONG = {'svg', 'png', 'jpg', 'jpeg', 'webp', 'ico', 'woff', 'woff2'}
    _CACHE_REVALIDATE = {'json', 'html', 'js', 'css'}
    _EXT_RE = _re.compile(r"\.([a-z0-9]+)(\?|$)", _re.IGNORECASE)

    # The server is local-only (127.0.0.1). We mount an explicit
    # CORS policy that denies all cross-origin browser callers, even
    # though the default browser policy already does so. The intent is
    # to make the policy obvious in code review and to avoid accidental
    # enablement via a future PR. Token-bearing requests must be
    # same-origin; non-browser clients (curl, the watchdog) are not
    # affected because they don't enforce CORS.
    from fastapi.middleware.cors import CORSMiddleware as _CORS
    app.add_middleware(
        _CORS,
        allow_origins=[],
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=600,
    )

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        # Security headers on all responses
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # CSP: scripts are self-hosted; Vue's browser compiler requires unsafe-eval.
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "object-src 'none'; "
            "base-uri 'self';"
        )
        response.headers["Content-Security-Policy"] = csp
        return response

    # ---- Auth middleware: Bearer token + CSRF ----
    # Reads auth token from settings. Public endpoints (read-only, no auth needed):
    # GET /, /api/stats, /api/sessions, /api/sessions/counts, /api/recall,
    # /api/memories (list only), /api/wiki (list only), /api/graph,
    # /api/pipeline, /api/weekly-report, /api/llm-audit, /api/source-health,
    # /api/write-guard, /api/diag, /api/install-hooks (GET), static files.
    _PUBLIC_PATHS = frozenset((
        '/', '/api/stats', '/api/sessions', '/api/sessions/counts',
        '/api/recall', '/api/memories', '/api/wiki', '/api/graph',
        '/api/pipeline', '/api/weekly-report', '/api/llm-audit',
        '/api/source-health', '/api/write-guard', '/api/diag',
        '/api/install-hooks', '/api/insights',
    ))

    def _is_public_path(path: str) -> bool:
        if path in _PUBLIC_PATHS:
            return True
        # Static assets must always be reachable so the SPA shell can
        # boot before the user has had a chance to paste the Bearer
        # token. The previous implementation folded /static/ and
        # /api/memories/{id} into a single block that returned False
        # for both, which broke the entire UI whenever a token was
        # configured. Audit H2: keep the static dir public.
        if path.startswith('/static/'):
            return True
        if path.startswith('/api/memories/'):
            # GET /api/memories/{id} is not public (contains data).
            return False
        if path.startswith('/api/wiki/') and path not in (
            '/api/wiki', '/api/wiki/contradictions', '/api/wiki/contradictions/scan',
        ):
            # Individual wiki page GETs are not public.
            return False
        return False

    @app.middleware("http")
    async def _auth_middleware(request, call_next):
        # Hoist JSONResponse import out of the conditional: every
        # error branch needs it and Python otherwise treats _J as
        # implicit-local at the second reference.
        from fastapi.responses import JSONResponse as _J
        if _is_public_path(request.url.path):
            return await call_next(request)
        # Check Bearer token
        auth_header = request.headers.get("Authorization", "")
        expected = store.get_setting("loop_memory_auth_token") if hasattr(store, "get_setting") else None
        if expected:
            if not auth_header.startswith("Bearer "):
                return _J({"error": "Missing or invalid Authorization header"}, status_code=401)
            token = auth_header[7:]
            # Simple constant-time comparison
            import secrets as _secrets
            if not _secrets.compare_digest(token, expected):
                return _J({"error": "Invalid token"}, status_code=401)
        # CSRF check for state-changing requests. A real browser
        # always sends an ``Origin`` header on POST/PUT/DELETE/PATCH,
        # and ``Sec-Fetch-Site`` is set to ``same-origin`` (or
        # ``none`` for file:// or some same-origin fetches). A request
        # with **no** Origin is therefore either a non-browser client
        # (curl, the watchdog) or a malicious same-network caller.
        # We only enforce CSRF for browser-like requests; non-browser
        # callers can still use the local API, but they must opt in
        # by sending the bearer token.
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("Origin", "")
            host = request.headers.get("Host", "")
            if origin:
                if origin not in (f"http://{host}", f"https://{host}"):
                    from fastapi.responses import JSONResponse as _J
                    return _J({"error": "Cross-origin request not allowed"}, status_code=403)
            elif request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                # Browser claims a cross-site context without sending Origin — reject.
                from fastapi.responses import JSONResponse as _J
                return _J({"error": "Missing Origin header"}, status_code=403)
            response = await call_next(request)
            response.headers.setdefault("Vary", "Origin")
            return response
        return await call_next(request)

    @app.middleware("http")
    async def _static_cache_headers(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/"):
            m = _EXT_RE.search(path)
            ext = m.group(1).lower() if m else ""
            if ext in _CACHE_REVALIDATE:
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            elif ext in _CACHE_LONG:
                response.headers["Cache-Control"] = "max-age=300, must-revalidate"
            else:
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    static_dir = static_dir or Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# moved to routes/system.py (register())

# moved to routes/insights.py (register())
# moved to routes/insights.py (register())
# moved to routes/system.py (register())
# moved to routes/system.py (register())

# moved to routes/system.py (register())
# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/memories.py (register())

# moved to routes/system.py (register())

# moved to routes/system.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/sessions.py (register())

# moved to routes/sessions.py (register())

# moved to routes/sessions.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/wiki.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/memories.py (register())

# moved to routes/graph.py (register())

# moved to routes/graph.py (register())

# moved to routes/graph.py (register())

# moved to routes/cognitive.py (register())

# moved to routes/cognitive.py (register())

# moved to routes/cognitive.py (register())

# moved to routes/export.py (register())

# moved to routes/export.py (register())

# moved to routes/graph.py (register())

# moved to routes/wiki.py (register())

# moved to routes/memories.py (register())

# moved to routes/sessions.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/system.py (register())

# moved to routes/graph.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/admin.py (register())

# moved to routes/memories.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())
# moved to routes/wiki.py (register())

# moved to routes/wiki.py (register())

# moved to routes/graph.py (register())
    # ----- Mount route groups (audit O1: app.py was 3226 lines;
    # route bodies now live under serve/routes/). -----
    from .routes.system import register as _register_system
    from .routes.insights import register as _register_insights
    from .routes.sessions import register as _register_sessions
    from .routes.memories import register as _register_memories
    from .routes.wiki import register as _register_wiki
    from .routes.graph import register as _register_graph
    from .routes.admin import register as _register_admin
    from .routes.cognitive import register as _register_cognitive
    from .routes.export import register as _register_export

    _register_system(app, store, scheduler, static_dir=static_dir)
    _register_insights(app, store, scheduler)
    _register_sessions(app, store, scheduler)
    _register_memories(app, store, scheduler)
    _register_wiki(app, store, scheduler)
    _register_graph(app, store, scheduler)
    _register_admin(app, store, scheduler)
    _register_cognitive(app, store, scheduler)
    _register_export(app, store, scheduler)

    return app

# Backwards-compatible aliases. The implementations live in
# ``serve.handlers`` so they can be unit-tested without spinning up
# a FastAPI app. ``_memory_to_dict`` was the local alias used by the
# old monolith; route modules now import it from ``._shared``.
_memory_to_dict = memory_to_dict
_session_to_dict = session_to_dict

def serve(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 7767,
    static_dir: str | None = None,
) -> None:
    try:
        import uvicorn  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "uvicorn is not installed; run `pip install loop-memory[serve]`"
        ) from e

    import logging
    log = logging.getLogger("loop_memory.serve")

    store = MemoryStore(db_path)
    sd = Path(static_dir) if static_dir else None
    app = create_app(store, sd)

    # Start the LLM consolidator scheduler. It is a no-op until the
    # user enables the schedule in /api/admin/llm/config.
    try:
        from ..jobs.scheduler import ConsolidatorScheduler
        scheduler = ConsolidatorScheduler(store)
        scheduler.reload_config()
        scheduler.start()
        app.state.scheduler = scheduler
        log.info("LLM consolidator scheduler started (enabled=%s)",
                 (scheduler.status().get("schedule") or {}).get("enabled", False))
    except Exception as e:
        log.warning("could not start LLM consolidator scheduler: %s", e)
        app.state.scheduler = None

    uvicorn.run(app, host=host, port=port, log_level="info")

"""Route group: admin.

Admin operations: storage, ingest, watcher, LLM, redact, auth token, rescore, gc, consolidate, compact, evolution, graph rebuild, etc.

All routes were extracted from ``serve/app.py`` as part of the O1
refactor to keep the central ``create_app`` small. Each block lives
inside ``register(app, store, scheduler=None)`` so closures over the
three captured variables work unchanged from the original layout.
"""
from __future__ import annotations

from typing import Any, Optional
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from ...serve.handlers import llm_test

from ...storage.sqlite_store import MemoryStore
from ._shared import _memory_to_dict, _export_safe_segment


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
    @app.post("/api/admin/bump-recall")
    def bump_recall(ids: str):
        """Manually increment recall_count on a set of memory ids.
        Used by the dashboard's ↻ button when the user wants to mark
        a memory as 'just consulted' so it ranks higher next time.
        ``ids`` is a comma-separated list of memory ids (the dashboard
        encodes the body this way so we stay on a query-only path,
        which works cleanly under FastAPI 0.139's stricter validation)."""
        ids_list = [s for s in (ids or "").split(",") if s]
        if not ids_list:
            raise HTTPException(400, "ids query param required (comma-separated)")
        n = store.bump_recalls(ids_list)
        if n:
            try: store.rescore_all(half_life_days=30.0)
            except Exception: pass
        return {"bumped": n}


    @app.post("/api/admin/evolution/run")
    def run_evolution(batch_size: int = 300, dry_run: bool = False):
        """Run the 5-stage evolution consolidator once. Requires an LLM
        provider to be configured; falls back to rule-based if not."""
        from loop_memory.jobs.evolution import EvolutionConsolidator
        from loop_memory.llm.providers import build_provider, default_config
        # The consolidator config is stored under "llm_consolidator" (see
        # /api/admin/llm/config). Pull it from there so MiniMax / etc.
        # providers are actually wired up — not silently downgraded.
        cfg_dict = store.get_setting("llm_consolidator", default_config()) or {}
        provider = build_provider(cfg_dict)
        rid = store.start_consolidation_run("manual-evolution", model=getattr(provider, "model", None))
        ec = EvolutionConsolidator(store, provider, {"dry_run": dry_run, "batch_size": batch_size})
        ec.set_run_id(rid)
        try:
            stats = ec.run(limit=batch_size)
            store.finish_consolidation_run(rid, status="done", stats=stats.to_dict())
            return stats.to_dict()
        except Exception as e:
            store.finish_consolidation_run(rid, status="error", error=str(e))
            raise HTTPException(500, str(e))


    @app.post("/api/admin/rescore")
    def rescore(half_life_days: float = 30.0):
        return {"updated": store.rescore_all(half_life_days)}


    @app.post("/api/admin/gc")
    def gc():
        return {"deleted": store.gc()}


    @app.post("/api/admin/consolidate")
    def consolidate():
        from ...backends.embedding import HashingEmbedder
        from ...jobs.consolidate import Consolidator
        report = Consolidator(store, embedder=HashingEmbedder(dim=64)).run()
        return {
            "rescored": report.rescored,
            "gc_removed": report.gc_removed,
            "merged": report.merged,
            "elapsed_ms": round(report.elapsed_ms, 1),
        }


    @app.post("/api/admin/compact")
    def admin_compact(force: bool = False, mode: str = "heuristic"):
        """Run a single compaction pass over the memory store.

        ``force=True`` ignores the age filter — useful for a one-shot
        "tidy up after a heavy ingest burst" trigger from the UI.
        ``mode`` switches between ``heuristic`` (cheap, scheduled) and
        ``llm`` (slower, higher-quality fusion). The LLM mode is a
        no-op for now and reserved for the future fusion pass.
        """
        from ...jobs.compact import Compactor
        from ...jobs.scheduler import ConsolidatorScheduler
        # Make sure we run on a worker thread so the request returns
        # fast even when there are tens of thousands of rows.
        sched: ConsolidatorScheduler | None = getattr(app.state, "scheduler", None)

        def _do() -> dict:
            c = Compactor(store, mode=mode)
            report = c.run(force=force)
            return report.to_dict()

        if sched is not None:
            return sched.run_blocking("compact", _do)
        return _do()


    @app.get("/api/admin/storage")
    def admin_storage():
        """Storage usage + per-table breakdown + budget info.

        Polled by the dashboard so the user can see when the store is
        approaching its ceiling *before* it crosses it.
        """
        breakdown = store.storage_breakdown()
        cfg = store.get_setting("storage_budget", {}) or {}
        return {
            "db_size_bytes": breakdown.get("db_size_bytes", 0),
            "breakdown": breakdown,
            "budget": {
                "max_bytes": int(cfg.get("max_bytes") or 0),
                "max_memories": int(cfg.get("max_memories") or 0),
                "auto_compact": bool(cfg.get("auto_compact", False)),
                "compact_interval_hours": int(cfg.get("compact_interval_hours") or 24),
            },
            "last_compact": store.get_setting("last_compact", {}) or {},
        }


    @app.post("/api/admin/storage/budget")
    def admin_storage_budget(body: dict):
        """Persist the storage budget config.

        Body keys (all optional, merged into existing config):

          * ``max_bytes`` int       - hard ceiling; auto-prune when exceeded
          * ``max_memories`` int    - soft ceiling on row count
          * ``auto_compact`` bool   - run compact on the scheduler cadence
          * ``compact_interval_hours`` int - cadence in hours (default 24)
        """
        current = store.get_setting("storage_budget", {}) or {}
        allowed_keys = {"max_bytes", "max_memories", "auto_compact", "compact_interval_hours"}
        for k in allowed_keys:
            if k in body:
                current[k] = body[k]
        store.set_setting("storage_budget", current)
        # Wake the scheduler so it picks up the new cadence.
        sched = getattr(app.state, "scheduler", None)
        if sched is not None and hasattr(sched, "reload_config"):
            sched.reload_config()
        return {"ok": True, "budget": current}


    @app.post("/api/admin/consolidate-now")
    def consolidate_now():
        """Trigger the configured consolidator scheduler immediately.

        Unlike ``/api/admin/llm/run`` (which runs once with the
        current request's params) and ``/api/admin/evolution/run``
        (which runs the 5-stage Evolution pipeline), this hits the
        user's *configured* scheduler — using their chosen model,
        provider, batch size, and schedule-derived behaviour — and
        records progress in the dashboard.
        """
        sched = getattr(app.state, "scheduler", None)
        if sched is None:
            raise HTTPException(503, "scheduler not running")
        result = sched.run_now(trigger="manual", block=False)
        if result is None:
            return {"queued": True}
        return {"queued": False, "result": result}


    @app.get("/api/admin/ingest/config")
    def ingest_config_get():
        """Return the watcher's ingest cadence settings.

        These knobs drive the background file-system watcher that
        auto-ingests finished transcripts from Codex / Claude / Hermes
        / OpenClaw. Exposing them via the API lets the Settings
        drawer tune the cadence without regenerating the launchd
        plist — the watcher reads from the settings store every
        few iterations and picks up changes automatically.

        Defaults are 5 minutes idle (size-stable wait) and 5-second
        poll. Users can dial up for less aggressive scanning (good
        for SSDs and long-lived sessions) or down for snappier
        recall.
        """
        from ...serve.watcher import DEFAULT_IDLE_SECONDS, DEFAULT_POLL_SECONDS
        cfg = store.get_setting("ingest", {}) or {}
        return {
            "idle_seconds": float(cfg.get("idle_seconds", DEFAULT_IDLE_SECONDS)),
            "poll_seconds": float(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)),
            "defaults": {
                "idle_seconds": DEFAULT_IDLE_SECONDS,
                "poll_seconds": DEFAULT_POLL_SECONDS,
            },
            "notes": {
                "idle_seconds": "size-stable wait before a transcript is "
                                "considered finished and ingested (seconds)",
                "poll_seconds": "how often the watcher scans the directory "
                                "(seconds)",
                "min_idle_seconds": 30,
                "min_poll_seconds": 1,
                "max_idle_seconds": 3600,
                "max_poll_seconds": 60,
            },
        }


    @app.get("/api/admin/wiki/scope")
    @app.get("/api/admin/wiki-auto-scope")
    def wiki_scope_config_get():
        """Return automatic wiki scope-routing settings.

        Pattern evaluation is local and deterministic.  Disabling it keeps
        new pages source-scoped but prevents automatic security promotion;
        it never changes existing pages.
        """
        from ...wiki.scope import auto_scope_config
        cfg = auto_scope_config(store)
        return {
            "wiki_auto_scope": cfg,
            "enabled": cfg["enabled"],
            "mode": cfg["mode"],
            "defaults": {"enabled": True, "mode": "pattern"},
        }


    @app.put("/api/admin/wiki/scope")
    @app.post("/api/admin/wiki/scope")
    @app.put("/api/admin/wiki-auto-scope")
    @app.post("/api/admin/wiki-auto-scope")
    def wiki_scope_config_put(body: dict):
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        current = wiki_scope_config_get()["wiki_auto_scope"]
        if "enabled" in body:
            value = body["enabled"]
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on"}
            current["enabled"] = bool(value)
        if "mode" in body:
            mode = str(body["mode"] or "").strip().lower()
            if mode not in {"pattern", "llm", "off"}:
                raise HTTPException(400, "mode must be one of: pattern, llm, off")
            current["mode"] = mode
        store.set_setting("wiki_auto_scope", current)
        store.set_setting("wiki_auto_scope_enabled", current["enabled"])
        store.set_setting("wiki_auto_scope_mode", current["mode"])
        return {"ok": True, "wiki_auto_scope": current, "enabled": current["enabled"], "mode": current["mode"]}


    @app.get("/api/admin/redact")
    def redact_config_get():
        """Return the current redaction settings.

        ``enabled`` controls whether ``MemoryPipeline._write`` strips
        secrets before they hit the long-term store. The setting
        also disables the ``<private>...</private>`` span stripping
        (a downstream consumer would still see the markers, but the
        body inside isn't removed).

        The ``counts`` field is empty here — the per-pipeline-run
        counter lives on the live ingest pipeline. See the live
        ``/api/admin/redact/live`` endpoint.
        """
        cfg = store.get_setting("redact", {}) or {}
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "kinds": cfg.get("kinds") or [
                "private_key_block",
                "openai_key", "openai_project_key", "openai_service_key",
                "anthropic_key", "gemini_key", "github_pat", "slack_token",
                "aws_access_key", "jwt", "bearer_token", "generic_high_entropy",
            ],
            "private_spans": bool(cfg.get("private_spans", True)),
        }


    @app.post("/api/admin/redact")
    def redact_config_post(body: dict):
        """Persist redaction settings.

        Body fields (all optional):
          * ``enabled`` (bool)   — global on/off for secret redaction
          * ``kinds`` (list[str])— disable specific pattern kinds
          * ``private_spans`` (bool) — honour ``<private>...</private>``
        """
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        current = store.get_setting("redact", {}) or {}
        new_cfg = dict(current)
        if "enabled" in body:
            new_cfg["enabled"] = bool(body["enabled"])
        if "private_spans" in body:
            new_cfg["private_spans"] = bool(body["private_spans"])
        if "kinds" in body:
            kinds = body["kinds"]
            if not isinstance(kinds, list):
                raise HTTPException(400, "kinds must be a list of strings")
            new_cfg["kinds"] = [str(k) for k in kinds]
        store.set_setting("redact", new_cfg)
        return {"ok": True, "redact": new_cfg}


    @app.post("/api/admin/redact/preview")
    def redact_preview(body: dict):
        """Show what the live redaction pipeline WOULD do on an
        arbitrary text snippet.

        Body: ``{"text": "..."}``
        Returns: ``{"text": "...redacted...",
                     "counts": {"openai_key": 1, ...},
                     "total": 1,
                     "total_chars": 51}``

        The intent is to let the UI show a side-by-side preview so
        users can see (and tune) what their pipeline produces
        without having to ingest a real transcript.
        """
        from ...privacy import redact_text, RedactionSummary
        if not isinstance(body, dict) or "text" not in body:
            raise HTTPException(400, "body must be {text: ...}")
        text = str(body.get("text") or "")
        summary = RedactionSummary()
        out = redact_text(text, summary=summary)
        return {
            "text": out,
            "counts": summary.counts,
            "total": summary.total,
            "total_chars": summary.total_chars,
        }


    @app.post("/api/admin/ingest/config")
    def ingest_config_post(body: dict):
        """Persist new ingest cadence settings.

        Body fields (all optional):
          * ``idle_seconds`` (int|float) — size-stable wait before ingest
          * ``poll_seconds`` (int|float) — directory scan period

        Validation enforces sane bounds (>= 30s for idle to avoid
        fragmenting long sessions, <= 3600s so something eventually
        ingests; >= 1s for poll, <= 60s to bound stat() churn).
        Settings persist immediately and the running watcher picks
        them up on the next reload tick (≤ 30s later).
        """
        if not isinstance(body, dict):
            from fastapi import HTTPException
            raise HTTPException(400, "body must be an object")
        from ...serve.watcher import DEFAULT_IDLE_SECONDS, DEFAULT_POLL_SECONDS
        current = store.get_setting("ingest", {}) or {}
        idle = body.get("idle_seconds", current.get("idle_seconds", DEFAULT_IDLE_SECONDS))
        poll = body.get("poll_seconds", current.get("poll_seconds", DEFAULT_POLL_SECONDS))
        try:
            idle = float(idle)
            poll = float(poll)
        except (TypeError, ValueError):
            from fastapi import HTTPException
            raise HTTPException(400, "idle_seconds and poll_seconds must be numeric")
        # Bounds — see API docstring.
        if idle < 30 or idle > 3600:
            from fastapi import HTTPException
            raise HTTPException(
                400,
                f"idle_seconds must be in [30, 3600] (got {idle}); "
                "use longer intervals to reduce disk wear and avoid "
                "fragmenting long sessions",
            )
        if poll < 1 or poll > 60:
            from fastapi import HTTPException
            raise HTTPException(
                400,
                f"poll_seconds must be in [1, 60] (got {poll}); "
                "the directory scan itself is cheap (stat()) but it "
                "still runs once per tick",
            )
        new_cfg = dict(current)
        new_cfg["idle_seconds"] = idle
        new_cfg["poll_seconds"] = poll
        store.set_setting("ingest", new_cfg)
        return {"ok": True, "ingest": new_cfg}


    @app.post("/api/admin/ingest")
    def ingest(source: str, path: str | None = None):
        """One-shot ingest endpoint (manual trigger).

        Was previously shadowed by an orphan @app.post decorator
        sitting on top of ingest_config_get — see the audit
        report (H1).  Now correctly wired so a UI click actually
        performs ingest.
        """
        from ...backends.embedding import HashingEmbedder
        from ...ingest.loader import default_paths, get_loader
        from ...ingest.pipeline import MemoryPipeline

        loader = get_loader(source)
        root = Path(path).expanduser() if path else default_paths()[source]
        files = list(loader.discover(root))
        if not files:
            return {"files": 0, "note": f"no transcripts found under {root}"}
        pipeline = MemoryPipeline(store, embedder=HashingEmbedder(dim=64))
        ingested = 0
        for fp in files:
            session = loader.load_one(fp)
            if session is None:
                continue
            pipeline.run(session)
            ingested += 1
        return {"files": ingested, "root": str(root)}


    @app.post("/api/admin/watcher/force-ingest")
    def watcher_force_ingest(
        source: str | None = None,
        path: str | None = None,
        idle_seconds: float = 0.0,
        active_only: bool = False,
    ):
        """Trigger a one-shot ingest pass for a watcher directory.

        This is the manual escape hatch for cases where the persistent
        launchd watcher is still waiting for the idle window (long
        active sessions whose file mtime keeps refreshing).

        Parameters:
          source         - one of "codex" | "claude" | "hermes" |
                           "openclaw". If omitted, we auto-detect from
                           the path (path's leaf must contain the
                           source name) or default to "codex".
          path           - explicit watch directory. If omitted, uses
                           the default watch dir for the given source.
          idle_seconds   - require files to have been size-stable for
                           at least this long before ingesting.
                           Default 0 = ingest any file that has new
                           content vs the last successful ingest.
          active_only    - if True, restrict to the most-recently
                           modified .jsonl file under the watch dir
                           (the "currently active" session). Useful
                           for the UI button: "force the active Codex
                           session to ingest now".

        Returns a per-file breakdown so the UI can show what happened.
        """
        from ...backends.embedding import HashingEmbedder
        from ...ingest.loader import default_paths, get_loader
        from ...ingest.pipeline import MemoryPipeline
        from .watcher import run_once

        # Resolve source
        src = (source or "").strip().lower() or None
        if not src and path:
            # Try to infer from path: ~/.codex/sessions → codex
            p_lower = path.lower()
            for candidate in ("codex", "claude", "hermes", "openclaw"):
                if f"/{candidate}/" in p_lower or p_lower.endswith(f"/{candidate}"):
                    src = candidate
                    break
        if not src:
            src = "codex"  # default fallback for the common case

        # Resolve watch dir
        if path:
            root = Path(path).expanduser()
        else:
            defaults = default_paths()
            if src not in defaults:
                return {"error": f"unknown source {src!r}", "sources": list(defaults.keys())}
            # default_paths may return a list for openclaw (sessions + memory)
            default_val = defaults[src]
            if isinstance(default_val, list):
                # For multi-path sources, force-ingest against the
                # first path; callers wanting a specific sub-dir
                # should pass ``path`` explicitly.
                root = Path(default_val[0]).expanduser()
            else:
                root = Path(default_val).expanduser()

        loader = get_loader(src)
        pipeline = MemoryPipeline(store, embedder=HashingEmbedder(dim=64))

        # active_only: pick the most-recently-modified transcript file
        # under root (or fall back to the dir itself).
        if active_only:
            try:
                candidates = [
                    p for p in loader.discover(root)
                    if p.is_file() and p.name != ".loop_memory_seen.json"
                ]
            except FileNotFoundError:
                candidates = []
            if not candidates:
                return {
                    "source": src, "root": str(root),
                    "active_only": True,
                    "scanned": 0, "ingested": 0, "skipped": 0, "errors": 0,
                    "files": [], "note": "no transcripts under watch dir",
                }
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            target = candidates[0]
            # Build a tiny virtual dir containing only this file by
            # running run_once against a temp dir is overkill — instead
            # we manually load + ingest just this one file but still
            # update the ledger.
            from .watcher import _load_ledger, _ledger_path, _save_ledger
            ledger_path = _ledger_path(root)
            ledger = _load_ledger(ledger_path)
            key = str(target)
            prev = ledger.get(key)
            try:
                st = target.stat()
            except FileNotFoundError:
                return {"source": src, "path": key, "error": "file disappeared"}
            file_result = {
                "path": key, "size": st.st_size, "mtime": st.st_mtime,
                "previous_ingested_at": (prev or {}).get("ingested_at"),
            }
            try:
                session = loader.load_one(target)
            except Exception as e:
                file_result.update({"status": "error", "error": f"{type(e).__name__}: {e}"})
                return {"source": src, "root": str(root), "active_only": True,
                        "scanned": 1, "ingested": 0, "skipped": 0, "errors": 1,
                        "files": [file_result]}
            if session is None:
                file_result["status"] = "skipped"
                file_result["reason"] = "loader returned None"
                return {"source": src, "root": str(root), "active_only": True,
                        "scanned": 1, "ingested": 0, "skipped": 1, "errors": 0,
                        "files": [file_result]}
            try:
                pipe_result = pipeline.run(session)
            except Exception as e:
                file_result.update({"status": "error", "error": f"{type(e).__name__}: {e}"})
                return {"source": src, "root": str(root), "active_only": True,
                        "scanned": 1, "ingested": 0, "skipped": 0, "errors": 1,
                        "files": [file_result]}
            n_items = len(pipe_result.summary_items)
            now = time.time()
            ledger[key] = {
                "sig": [st.st_mtime, st.st_size],
                "first_seen": (prev or {}).get("first_seen", now),
                "last_mtime": st.st_mtime,
                "size": st.st_size,
                "last_size_change_at": now,
                "ingested_at": now,
            }
            _save_ledger(ledger_path, ledger)
            file_result.update({"status": "ingested", "summary_items": n_items})
            return {
                "source": src, "root": str(root), "active_only": True,
                "scanned": 1, "ingested": 1, "skipped": 0, "errors": 0,
                "files": [file_result],
            }

        result = run_once(loader, root, pipeline, idle_seconds=idle_seconds)
        result["source"] = src
        result["root"] = str(root)
        result["active_only"] = False
        return result


    @app.get("/api/admin/watcher/active-session")
    def watcher_active_session(source: str = "codex"):
        """Return the most-recently-modified transcript file under the
        source's default watch dir. Used by the UI button to show
        "currently active session: <name>" next to the Force-ingest
        action.
        """
        from ...ingest.loader import default_paths, get_loader
        defaults = default_paths()
        if source not in defaults:
            return {"error": f"unknown source {source!r}", "sources": list(defaults.keys())}
        root = defaults[source]
        if isinstance(root, list):
            root = root[0]
        root = Path(root).expanduser()
        loader = get_loader(source)
        try:
            candidates = [
                p for p in loader.discover(root)
                if p.is_file() and p.name != ".loop_memory_seen.json"
            ]
        except FileNotFoundError:
            candidates = []
        if not candidates:
            return {"source": source, "root": str(root), "active": None}
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        top = candidates[0]
        st = top.stat()
        return {
            "source": source,
            "root": str(root),
            "active": {
                "path": str(top),
                "name": top.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "age_seconds": time.time() - st.st_mtime,
            },
        }


    @app.post("/api/admin/graph/rebuild")
    def graph_rebuild(clear: bool = True, limit: int = 0, mode: str = "wiki"):
        """Rebuild the knowledge graph.

        ``mode``:

          * ``wiki``   (default) — build from distilled wiki pages.
                       Cleaner, denser, mirrors the user's curated knowledge.
          * ``memory``            — build from raw memories (legacy behaviour).
                       Useful for "I want to see everything" exploration.
        """
        from ...graph.build import KnowledgeGraph
        kg = KnowledgeGraph(store)
        if mode == "memory":
            report = kg.rebuild(clear=clear, limit=limit or None)
        else:
            report = kg.rebuild_from_wiki(clear=clear, limit=limit or None)
        return {
            "entities": report.entities,
            "relations": report.relations,
            "memories_scanned": report.memories_scanned,
            "elapsed_ms": round(report.elapsed_ms, 1),
            "mode": mode,
        }

    # =====================================================================
    # LLM-driven consolidator: settings, status, runs, manual + dry-run
    # =====================================================================


    @app.get("/api/admin/llm/providers")
    def llm_providers():
        from ...llm.providers import PROVIDERS
        return [
            {
                "id": spec.id,
                "label": spec.label,
                "default_model": spec.default_model,
                "needs_api_key": spec.needs_api_key,
                "needs_base_url": spec.needs_base_url,
                "default_base_url": spec.default_base_url,
                "description": spec.description,
            }
            for spec in PROVIDERS.values()
        ]


    @app.get("/api/admin/llm/config")
    def llm_config_get():
        import hashlib
        import time
        from ...llm.providers import default_config, validate_config
        from ...security import (
            account_for, backend_display_name, backend_name,
            get_secret, has_secret,
        )
        cfg, warnings = validate_config(
            store.get_setting("llm_consolidator", default_config())
        )
        provider = cfg.get("provider") or "echo"
        account = cfg.get("api_key_account") or account_for(provider)
        cfg["api_key_account"] = account
        has = has_secret(account)
        cfg["api_key_set"] = bool(has)
        # If the secret exists but no fingerprint / saved_at are recorded
        # in cfg (e.g. the user pasted the key directly into the JSON file
        # or the secret backend was filled by an external tool), derive them on
        # the fly so the UI can show a useful "ends with … · saved …"
        # chip instead of placeholders.
        if has and (not cfg.get("api_key_fingerprint") or not cfg.get("api_key_saved_at")):
            try:
                raw = get_secret(account) or ""
                if raw:
                    tail = raw[-4:] if len(raw) >= 4 else raw
                    h = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
                    cfg["api_key_fingerprint"] = f"{tail}·{h}"
                # Use the secret file mtime as a sane fallback so the chip
                # shows a real timestamp instead of "—".
                try:
                    import os as _os
                    from pathlib import Path as _Path
                    from ...security.secrets import _pick_backend
                    backend = _pick_backend()
                    p = getattr(backend, "path", None)
                    if p is None and hasattr(backend, "path"):
                        p = backend.path
                    if p:
                        cfg["api_key_saved_at"] = p.stat().st_mtime
                except Exception:
                    cfg["api_key_saved_at"] = cfg.get("api_key_saved_at") or _time.time()
            except Exception:
                pass
        return {
            "config": cfg,
            "warnings": warnings,
            "secret_backend": backend_name(),
            "secret_backend_display": backend_display_name(),
        }


    @app.put("/api/admin/llm/config")
    async def llm_config_put(body: dict):
        from ...llm.providers import default_config, validate_config
        from ...security import account_for, delete_secret, has_secret, set_secret
        body = body or {}
        provider = (body.get("provider") or "echo").lower()
        # The api_key field, if present, is *only* sent to the
        # secret backend - never to the settings store. We pop it before
        # validate_config so the cleaned config never carries the key.
        raw_key = body.pop("api_key", None)
        if raw_key is not None and raw_key != "":
            account = account_for(provider)
            if raw_key == "__clear__":
                delete_secret(account)
                body["api_key_set"] = False
                body["api_key_fingerprint"] = ""
                body["api_key_saved_at"] = 0
            else:
                set_secret(account, raw_key)
                body["api_key_set"] = True
                # Non-secret fingerprint: last 4 chars + short hash.
                # Never store the key itself.
                import hashlib
                tail = raw_key[-4:] if len(raw_key) >= 4 else raw_key
                h = hashlib.sha1(raw_key.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
                body["api_key_fingerprint"] = f"{tail}·{h}"
                body["api_key_saved_at"] = time.time()
            body["api_key_account"] = account
        elif "api_key" not in body:
            # Preserve current key status if the user didn't touch it
            existing = store.get_setting("llm_consolidator", default_config())
            ex_provider = (existing.get("provider") or "echo").lower()
            if ex_provider == provider:
                ex_account = existing.get("api_key_account") or account_for(provider)
                body["api_key_set"] = bool(existing.get("api_key_set")) and has_secret(ex_account)
                body["api_key_account"] = ex_account
        cfg, warnings = validate_config(body)
        # Always read the canonical status from the secret backend so the
        # response reflects reality.
        cfg["api_key_set"] = has_secret(cfg.get("api_key_account") or account_for(provider))
        cfg["api_key_account"] = cfg.get("api_key_account") or account_for(provider)
        # Compute the response-only fingerprint / saved_at on the fly
        # rather than persisting them. Audit M3: persisting a 6-char
        # sha1+tail fingerprint into the settings table leaks enough
        # info to brute-force candidate keys (36-bit entropy). The
        # secret file's mtime already records "when" anyway.
        try:
            from ...security.secrets import _pick_backend as _pb
            _be = _pb()
            _path = getattr(_be, "path", None)
            _saved_at = _path.stat().st_mtime if _path else time.time()
        except Exception:
            _saved_at = time.time()
        try:
            account_fp = cfg.get("api_key_account") or account_for(provider)
            raw = ""
            # Only compute a fingerprint if we just wrote a fresh key
            # (the prior branch set body["api_key_fingerprint"]); never
            # read the secret back to display — that would force a key
            # unlock prompt on every PUT for OS keychain users.
            if body.get("api_key_fingerprint"):
                fingerprint = body["api_key_fingerprint"]
            else:
                fingerprint = ""
        except Exception:
            fingerprint = ""
        # Strip response-only fields before persisting.
        cfg.pop("api_key_fingerprint", None)
        cfg.pop("api_key_saved_at", None)
        store.set_setting("llm_consolidator", cfg)
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.reload_config()
        from ...security import backend_display_name, backend_name
        return {
            "ok": True,
            "config": cfg,
            "warnings": warnings,
            "secret_backend": backend_name(),
            "secret_backend_display": backend_display_name(),
            "api_key_fingerprint": fingerprint,
            "api_key_saved_at": _saved_at,
        }


    @app.get("/api/admin/auth/token")
    def auth_token_get():
        """Check if auth token is configured."""
        token = store.get_setting("loop_memory_auth_token")
        return {"enabled": bool(token)}


    @app.post("/api/admin/auth/token")
    async def auth_token_post():
        """Generate or rotate the auth token.

        Audit M4: previously this endpoint was a TOFU bootstrap
        that left the server in ``no-token`` mode after a DELETE,
        immediately making every protected endpoint unauthenticated
        again. Now DELETE *also* rotates (returns a fresh token)
        so the server can never be in a fully-unauthenticated state
        unless the user has never visited the UI.

        Auth-wise: middleware already requires ``Authorization:
        Bearer`` on this route *if* a token is currently
        configured, so a rotation always needs the old token.
        """
        import secrets as _secrets
        new_token = _secrets.token_urlsafe(32)
        store.set_setting("loop_memory_auth_token", new_token)
        return {"token": new_token, "rotated": True}


    @app.delete("/api/admin/auth/token")
    def auth_token_rotate_via_delete():
        """Rotate the auth token (audit M4).

        Previous behaviour: fully removed the token, leaving the
        server unauthenticated for every subsequent call. The
        attacker-friendly use case is: a curious user creates a
        token, then clicks "Disable", at which point anyone with
        localhost access (e.g. a misconfigured proxy, a malicious
        dashboard widget, a future feature that exposes the
        endpoint without auth) gains admin.

        New behaviour: this endpoint is a *rotate*, not a *disable*.
        A fresh token is generated and returned; the old one is
        invalidated. The server stays authenticated, so we can
        never bottom out at ``auth-disabled`` after a first token
        has been set.

        If the user genuinely wants to disable auth (which we do
        not recommend for any non-loopback bind), they should
        delete the row directly via the ``settings`` store or
        uninstall the server.
        """
        import secrets as _secrets
        # Even if a token exists, this route is still gated by the
        # middleware (so a DELETE without a Bearer header on a
        # previously-configured server is 401). When no token
        # existed, we act as a bootstrap (acts the same as POST).
        new_token = _secrets.token_urlsafe(32)
        store.set_setting("loop_memory_auth_token", new_token)
        return {"token": new_token, "rotated": True, "ok": True}


    @app.post("/api/admin/llm/test")
    async def llm_test_route(body: dict | None = None):
        """Smoke-test the LLM provider without writing to the store.
        See handlers.llm_test for the body contract.

        The result is also recorded on the scheduler so the top-bar
        model chip can switch between green-pulsing (verified) and
        amber (last test failed) without the user re-opening the
        drawer.
        """
        sched = getattr(app.state, "scheduler", None)
        return llm_test(store, body or {}, scheduler=sched)


    @app.get("/api/admin/llm/status")
    def llm_status():
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is None:
            return {"running": False, "reason": "scheduler-not-started"}
        return scheduler.status()


    @app.delete("/api/admin/llm/key")
    def llm_clear_key():
        """Delete the API key for the currently-configured provider."""
        from ...llm.providers import default_config, validate_config
        from ...security import account_for, delete_secret, has_secret
        cfg, _ = validate_config(store.get_setting("llm_consolidator", default_config()))
        account = cfg.get("api_key_account") or account_for(cfg.get("provider") or "echo")
        removed = delete_secret(account) if has_secret(account) else False
        cfg["api_key_set"] = False
        cfg["api_key_fingerprint"] = ""
        cfg["api_key_saved_at"] = 0
        store.set_setting("llm_consolidator", cfg)
        return {"removed": removed, "account": account}


    @app.get("/api/admin/llm/runs")
    def llm_runs(limit: int = 20):
        return store.list_consolidation_runs(limit=limit)


    @app.post("/api/admin/llm/run")
    def llm_run_now(dry_run: bool = False, limit: int = 0):
        """Trigger a consolidation pass right now.

        ``dry_run=true`` returns the actions the LLM *would* take on
        the first ~50 memories without writing anything back.
        """
        from ...jobs.llm_consolidate import LLMConsolidator
        from ...llm.providers import build_provider, default_config, validate_config
        cfg, _ = validate_config(store.get_setting("llm_consolidator", default_config()))
        provider = build_provider(cfg)
        if dry_run:
            cons = LLMConsolidator(store, provider, cfg.get("behaviour") or {})
            preview = cons.preview(limit=limit or 20)
            return {"dry_run": True, "preview": preview, "config": cfg}
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.notify_ingest()
            scheduler.run_now(trigger="manual", block=False)
            return {"ok": True, "queued": True, "config": cfg}
        # no scheduler (shouldn't happen in normal serve mode): run synchronously
        cons = LLMConsolidator(store, provider, cfg.get("behaviour") or {})
        stats = cons.run()
        return {"ok": True, "queued": False, "stats": stats.to_dict(), "config": cfg}


    @app.post("/api/admin/llm/schedule")
    async def llm_schedule(body: dict):
        """Quick-toggle the scheduler on/off without rewriting the rest of the config."""
        from ...llm.providers import default_config, validate_config
        cfg, warnings = validate_config(store.get_setting("llm_consolidator", default_config()))
        sched = dict(cfg.get("schedule") or {})
        for k, v in (body or {}).items():
            sched[k] = v
        cfg["schedule"] = sched
        store.set_setting("llm_consolidator", cfg)
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.reload_config()
        return {"ok": True, "schedule": sched, "warnings": warnings}


    @app.post("/api/admin/wiki/reclassify-legacy")
    def wiki_reclassify_legacy_endpoint(body: dict | None = None):
        """Re-classify legacy wiki pages and downgrade non-universal
        security rows to per-source scope. Read-only with
        body.dry_run=true; otherwise persists scope changes
        in-place. Audit history is preserved on every touched row
        so the front-end classification history view still works."""
        body = body or {}
        dry = bool(body.get("dry_run", False))
        batch = int(body.get("batch", 500))
        if dry:
            pages = store.list_wiki_pages(limit=batch)
            legacy = [
                p for p in pages
                if (p.get("scope") or "global") == "global"
                and p.get("auto_classification") is None
            ]
            return {
                "dry_run": True,
                "scanned": len(pages),
                "legacy_global": len(legacy),
                "items": [{"slug": p["slug"], "title": p.get("title")}
                           for p in legacy],
            }
        from ...wiki import reclassify_legacy_pages
        return reclassify_legacy_pages(store, batch=batch)


    # ---- wiki pages -----------------------------------------------------

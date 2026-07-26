"""Route group: system.

Read-only system diagnostics + bootstrap endpoints (index, stats, diag, install-hooks, source-health, llm-audit, write-guard, signals, pipeline*, recall).

All routes were extracted from ``serve/app.py`` as part of the O1
refactor to keep the central ``create_app`` small. Each block lives
inside ``register(app, store, scheduler=None)`` so closures over the
three captured variables work unchanged from the original layout.
"""
from __future__ import annotations

from typing import Any, Optional
from pathlib import Path

import re
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from ...serve.handlers import pipeline_stage_items
from ...serve.handlers import pipeline_dashboard
from ...cli._common import DEFAULT_DB

from ...storage.sqlite_store import MemoryStore
from ._shared import _memory_to_dict, _export_safe_segment


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None,
               static_dir: Path | None = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    ``static_dir`` is the directory containing index.html + i18n JSONs.
    Falls back to ``../static`` next to ``routes/`` if not provided.
    """
    if static_dir is None:
        static_dir = Path(__file__).parent.parent / "static"
    @app.get("/")
    def index():
        """Serve the dashboard HTML with the i18n JSONs inlined.

        The dashboard renders its UI from string keys like
        ``tab.wiki`` looked up via ``t()`` in ``store.js``. Until the
        i18n JSON files finish loading, ``t()`` would return the raw
        key (e.g. ``tab.wiki``) — which the user perceived as
        English-looking code-style text on every hard refresh
        ("页面先变英文再转中文").

        The fix is to inline both dictionaries as ``<script
        type="application/json">`` tags in the HTML so ``store.js``
        can read them synchronously at module init time, before Vue
        even mounts. The total inline payload is ~60KB (the two
        JSON files), which is acceptable for a single full-page
        load — and crucially, it eliminates the hydration race.

        Reads the JSONs from disk on every request so an i18n edit
        is picked up on the next hard refresh without a server
        restart. Cheap (<1ms on local SSD).
        """
        index_path = static_dir / "index.html"
        if not index_path.exists():
            return JSONResponse({"error": "index.html missing"}, status_code=500)
        try:
            html = index_path.read_text(encoding="utf-8")
        except Exception:
            return FileResponse(str(index_path))
        try:
            en_json = (static_dir / "i18n" / "en.json").read_text(encoding="utf-8")
            zh_json = (static_dir / "i18n" / "zh.json").read_text(encoding="utf-8")
            # HTML-encode the JSON to prevent XSS when injected into <script> tags.
            # Escape &, <, >, and " (all HTML special chars). Also handle </script>
            # with a standard reverse-solidus escape so it can't close the tag.
            import html as _html
            en_safe = _html.escape(en_json, quote=True).replace("</script>", "<\\/script>")
            zh_safe = _html.escape(zh_json, quote=True).replace("</script>", "<\\/script>")
            # Inject the two payloads right before main.js so they
            # exist when store.js evaluates.
            inject = (
                '<script type="application/json" id="loop-i18n-en">'
                + en_safe + "</script>"
                '<script type="application/json" id="loop-i18n-zh">'
                + zh_safe + "</script>"
            )
            marker = '<script type="module" src="static/js/main.js"></script>'
            if marker in html:
                html = html.replace(marker, inject + marker, 1)
            else:
                # Fallback: inject before </body>
                html = html.replace("</body>", inject + "</body>", 1)
        except Exception:
            # If the i18n files are missing or unreadable, fall back
            # to the bare HTML — ``store.js`` will then load them
            # over HTTP (with the old flash behaviour, but at least
            # the page still renders).
            pass
        from fastapi.responses import HTMLResponse  # type: ignore
        return HTMLResponse(html)


    @app.get("/api/llm-audit")
    def llm_audit(limit: int = 50, kind: str | None = None, since: float | None = None):
        from ...storage.sqlite_store import LLMAuditStore
        audit = LLMAuditStore(store)
        recent = audit.recent(limit=limit, kind=kind)
        s = audit.stats(since_ts=since)
        return {"recent": recent, "stats": s}

    # ====================================================================
    # /api/write-guard — live drop counts + last-rejected timestamps
    # ====================================================================


    @app.get("/api/write-guard")
    def write_guard(window_hours: float = 24 * 7):
        from ...storage.sqlite_store import WriteGuardDropStore
        ds = WriteGuardDropStore(store)
        return ds.summary(window_hours=window_hours)


    @app.get("/api/source-health")
    def source_health():
        """Detect per-source ingest staleness + launchd hook status.

        Statuses:
          - fresh:    last ingest within 24h
          - stale:    24h - 7d
          - silent:   > 7d
          - never:    no memories yet
        """
        import subprocess as _sp
        now = time.time()
        with store._conn() as c:
            # Normalize per-thread codex sources to a single bucket so the
            # health view shows one row per real source.
            rows = c.execute(
                """SELECT COALESCE(source, 'unknown') AS s,
                          COUNT(*) AS c,
                          MAX(created_at) AS last_ts
                   FROM memories GROUP BY s"""
            ).fetchall()
            bucket = {}
            for r in rows:
                key = r['s'].split('/')[0] if '/' in r['s'] else r['s']
                if key not in bucket or r['last_ts'] > bucket[key]['last_ts']:
                    bucket[key] = {'c': r['c'], 'last_ts': r['last_ts']}
            rows = [
                {'s': k, 'c': v['c'], 'last_ts': v['last_ts']}
                for k, v in bucket.items()
            ]
            rows.sort(key=lambda x: -x['c'])
        sources = []
        healthy_count = 0
        for r in rows:
            last_ts = r["last_ts"] or 0
            age_h = (now - last_ts) / 3600 if last_ts else None
            if age_h is None:
                status = "never"
                hint = "no memories yet -- check the watcher is running"
            elif age_h <= 24:
                status = "fresh"; healthy_count += 1
                hint = "OK ingesting normally"
            elif age_h <= 24 * 7:
                status = "stale"
                hint = "WARN no ingest in " + str(int(age_h // 24)) + "d -- check the hook"
            else:
                status = "silent"
                hint = "DEAD silent for " + str(int(age_h // 24)) + "d -- hook likely dead"
            sources.append({
                "source": r["s"],
                "count": r["c"],
                "last_ts": last_ts,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "status": status,
                "hint": hint,
            })
        # Detected launchd hooks
        hooks = []
        try:
            out = _sp.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=3,
            )
            for line in out.stdout.splitlines():
                if "loopmemory" in line.lower():
                    hooks.append(line.strip())
        except Exception:
            pass
        # Overall
        statuses = [s["status"] for s in sources]
        if statuses and all(s in ("never", "silent") for s in statuses):
            overall = "silent"
        elif any(s in ("silent", "stale") for s in statuses):
            overall = "degraded"
        else:
            overall = "healthy"
        return {
            "sources": sources,
            "hooks": hooks,
            "overall": overall,
            "healthy_count": healthy_count,
            "checked_at": now,
        }

    # ------------------------------------------------------------------
    # Client integration: ``install-hooks`` endpoint.
    #
    # Until 2026-07 the only way to wire Codex/Claude/Hermes up to the
    # loop-memory MCP + SessionStart inject was the CLI command
    # ``loop-memory install-hooks`` — invisible from the web UI. Users
    # had to find the README, the doctor command, or the wiki page.
    # This endpoint + Diagnostic modal give the user a single button
    # that configures every detected CLI in one shot.
    # ------------------------------------------------------------------


    @app.get("/api/install-hooks")
    def install_hooks_status():
        """Inspect detected clients + their MCP/hook state (read-only).

        Returns a per-client status map + a hint of what would happen
        if the user pressed the 'configure all' button.
        """
        from pathlib import Path as _P
        try:
            from ...cli.commands.diag import _detect_clients as _dc
            clients = _dc()
        except Exception as e:
            return {"ok": False, "error": str(e), "clients": {}}
        return {
            "ok": True,
            "clients": clients,
            "configured_count": sum(1 for c in clients.values() if c.get("mcp_configured")),
            "installed_count":  sum(1 for c in clients.values() if c.get("installed")),
        }


    @app.post("/api/install-hooks")
    def install_hooks_run():
        """Run ``loop-memory install-hooks`` programmatically.

        Re-uses the same code path the CLI does so a UI click produces
        an identical result to ``loop-memory install-hooks`` typed at
        the shell. Returns the per-client actions so the UI can show
        confirmation rows.
        """
        try:
            from ...cli.commands.hooks import (
                _install_codex, _install_claude, _install_hermes, _openclaw_hint,
            )
            from pathlib import Path as _P
            home = _P.home()
            actions = []
            _install_codex(home, actions)
            _install_claude(home, actions)
            _install_hermes(home, actions)
            _openclaw_hint(home, actions)
            try:
                from ...cli.commands.diag import _detect_clients as _dc
                clients_after = _dc()
            except Exception:
                clients_after = {}
            return {
                "ok": True,
                "actions": actions,
                "clients": clients_after,
                "note": "Restart your CLI to pick up the new MCP server + hooks.",
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "actions": []}


    @app.get("/api/diag")
    def diag():
        """JSON version of ``loop-memory doctor``.

        Used by the web UI "Run doctor" modal so the user sees
        what's installed / wired / broken without opening a shell.
        """
        from pathlib import Path as _Path
        import json as _json
        import shutil as _shutil
        import urllib.request as _ur
        from ...cli.commands.diag import (
            _detect_clients, _detect_watcher,
        )
        from ...llm.providers import default_config
        from ...security import (
            account_for, backend_display_name, has_secret,
        )
        cli_path = _shutil.which("loop-memory")
        server_running = False
        try:
            with _ur.urlopen("http://127.0.0.1:7767/api/stats", timeout=1) as r:
                server_running = r.status == 200
        except Exception:
            server_running = False
        # Provider + key status
        try:
            cfg = store.get_setting("llm_consolidator", default_config())
            provider = cfg.get("provider") or "echo"
            if provider != "echo":
                account = cfg.get("api_key_account") or account_for(provider)
                api_key_set = bool(has_secret(account))
            else:
                api_key_set = None
        except Exception:
            cfg = {}
            provider = "echo"
            api_key_set = None
        try:
            n_mem = store.count_memories()
            n_sess = store.count_sessions()
            n_wiki = store.count_wiki_pages()
            n_ent = store.count_entities()
        except Exception:
            n_mem = n_sess = n_wiki = n_ent = 0
        clients = _detect_clients()
        return {
            "cli_on_path": cli_path is not None,
            "cli_path": cli_path,
            "db_path": str(_Path(DEFAULT_DB)),
            "db_exists": _Path(DEFAULT_DB).exists(),
            "server_running": server_running,
            "clients": clients,
            "watcher_running": _detect_watcher(),
            "provider": provider,
            "model": cfg.get("model") if cfg else None,
            "secret_backend": backend_display_name(),
            "api_key_set": api_key_set,
            "counts": {
                "memories": n_mem, "sessions": n_sess,
                "wiki": n_wiki, "entities": n_ent,
            },
        }


    @app.get("/api/stats")
    def stats():
        return store.stats()


    @app.get("/api/recall")
    def recall(query: str, limit: int = 10, include: str = "memories,wiki,entities",
              bump: int = 1, source: str | None = None,
              mode: str = "hybrid", level: int = 1):
        """Unified recall — wiki + memories + entities in one ranked stream.

        ``mode`` is 'hybrid' (default) for BM25+semantic+entity RRF,
        or 'legacy' for the old LIKE-based recall. Hybrid is what the
        dashboard Timeline + MCP + CLI use; legacy is kept for
        benchmarking and for skipping the FTS5 rebuild on huge DBs.

        ``source`` enables per-source knowledge scope: only wiki pages
        whose scope is 'global' OR contains this source are returned,
        and memories whose source matches this token are boosted. The
        dashboard passes the currently-active client (codex/claude/
        hermes/openclaw); the MCP/CLI tools pass the calling client.
        """
        wanted = tuple(s.strip() for s in include.split(",") if s.strip())
        if not wanted:
            wanted = ("memories", "wiki", "entities")
        if mode == "legacy" or not hasattr(store, "recall_hybrid"):
            r = store.recall(
                query,
                limit=limit,
                include=wanted,
                bump_signals=bool(bump),
                source=source,
            )
        else:
            r = store.recall_hybrid(
                query, limit=limit, include=wanted,
                bump_signals=bool(bump), source=source, level=level,
            )
        return {
            "query": query,
            "tokens": r.get("tokens", []),
            "memories": r.get("memories", []),
            "wiki": r.get("wiki", []),
            "entities": r.get("entities", []),
            "mode": mode,
            "source": source,
            "level": level,
        }


    @app.get("/api/pipeline")
    def pipeline_dashboard_route():
        """5-stage data flow dashboard. See handlers.pipeline_dashboard."""
        return pipeline_dashboard(store)


    @app.get("/api/pipeline/{stage}/items")
    def pipeline_stage_items_route(stage: str, limit: int = 50):
        """Drill-down: see handlers.pipeline_stage_items."""
        return pipeline_stage_items(store, stage, limit=limit)


    @app.get("/api/pipeline/score-distribution")
    def score_distribution(limit: int = 1000):
        """Bucket every memory's score into 10 bins (0.0–0.1, 0.1–0.2, ...)
        for the dashboard's score-distribution chart."""
        bins = [0] * 10
        now = time.time()
        rows = store.list_memories(limit=limit)
        for m in rows:
            sig = store.get_signal(m.id)
            comps = MemoryStore.score_components(
                importance=m.importance or 0.0,
                created_at=m.created_at,
                now=now,
                recall_count=sig["recall_count"],
                last_recalled_at=sig["last_recalled_at"],
                positive=sig["positive"],
                negative=sig["negative"],
                half_life_days=30.0,
            )
            idx = min(9, int(comps["score"] * 10))
            bins[idx] += 1
        return {
            "bins": [{"range": [i/10, (i+1)/10], "count": bins[i]} for i in range(10)],
            "total": sum(bins),
            "sampled": len(rows),
        }


    @app.get("/api/pipeline/decay-stats")
    def decay_stats():
        """How is the average score decaying over time? Bins memories by
        age bucket so the dashboard can show 'fresh memories still rank
        high, 60-day-old memories have decayed to X%'."""
        now = time.time()
        buckets = [
            ("<1d",  0,    86400),
            ("1-7d",  86400,    7*86400),
            ("7-30d", 7*86400,  30*86400),
            ("30-90d", 30*86400, 90*86400),
            (">90d",  90*86400, 10**9),
        ]
        out = []
        for label, lo, hi in buckets:
            mems = store.list_memories(since=now-hi, until=now-lo, limit=2000)
            if not mems:
                out.append({"label": label, "count": 0, "avg_score": 0,
                            "avg_recency": 0, "avg_usage": 0})
                continue
            rec_sum = use_sum = sc_sum = 0.0
            for m in mems:
                sig = store.get_signal(m.id)
                c = MemoryStore.score_components(
                    importance=m.importance or 0.0, created_at=m.created_at,
                    now=now, recall_count=sig["recall_count"],
                    last_recalled_at=sig["last_recalled_at"],
                    positive=sig["positive"], negative=sig["negative"],
                    half_life_days=30.0,
                )
                sc_sum += c["score"]; rec_sum += c["recency"]; use_sum += c["usage"]
            n = len(mems)
            out.append({"label": label, "count": n,
                        "avg_score": round(sc_sum/n, 3),
                        "avg_recency": round(rec_sum/n, 3),
                        "avg_usage": round(use_sum/n, 3)})
        return {"buckets": out}


    @app.get("/api/signals")
    def signals(kind: str = "recall_count", limit: int = 10):
        """Top-N memories by a usage signal column (recall / 👍 / 👎)."""
        rows = store.top_signals(kind=kind, limit=limit)
        return {
            "kind": kind,
            "items": [
                {
                    "id": r["id"],
                    "kind": r.get("kind"),
                    "text": r.get("text") or "",
                    "importance": r.get("importance") or 0.0,
                    "score": r.get("score") or 0.0,
                    "recall_count": r.get("recall_count") or 0,
                    "positive": r.get("positive") or 0,
                    "negative": r.get("negative") or 0,
                }
                for r in rows
            ],
        }


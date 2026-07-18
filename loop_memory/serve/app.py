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
    static_dir = static_dir or Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def index():
        index_path = static_dir / "index.html"
        if not index_path.exists():
            return JSONResponse({"error": "index.html missing"}, status_code=500)
        return FileResponse(str(index_path))

    @app.get("/api/insights")
    def insights():
        """All the data the Insights dashboard needs in one shot.

        Powers the Stats overview + 生命周期 + Self-improvement Pulse
        + 记忆压缩 + 记忆粒度 + 数据分布 widgets. Cheap enough to
        poll every 5-10s.
        """
        import time as _time
        now = _time.time()
        with store._conn() as c:
            # --- 1) Stats overview ---
            n_total = c.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            n_today = c.execute(
                "SELECT COUNT(*) c FROM memories "
                "WHERE created_at >= strftime('%s','now','start of day')"
            ).fetchone()["c"]
            n_active = c.execute(
                "SELECT COUNT(*) c FROM memories "
                "WHERE score >= 0.3 AND created_at >= ?",
                (now - 7 * 86400,),
            ).fetchone()["c"]
            n_links = c.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
            # Merge group count: tag-cluster clusters from evolution runs
            n_clusters = c.execute(
                "SELECT COUNT(*) c FROM (SELECT DISTINCT slug FROM wiki_pages)"
            ).fetchone()["c"]
            avg_score = c.execute(
                "SELECT AVG(score) FROM memories"
            ).fetchone()[0] or 0.0
            n_decayed = c.execute(
                "SELECT COUNT(*) c FROM memories WHERE score < 0.3"
            ).fetchone()["c"]
            # entities are our "向量数"
            n_entities = c.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
            # Occupation = % of memories that are High+Mid importance
            n_high = c.execute(
                "SELECT COUNT(*) c FROM memories WHERE importance >= 0.7"
            ).fetchone()["c"]
            n_mid = c.execute(
                "SELECT COUNT(*) c FROM memories WHERE importance >= 0.4 AND importance < 0.7"
            ).fetchone()["c"]
            occupation = ((n_high + n_mid) / max(1, n_total)) * 100.0
            # Citation rate = share of memories that were recalled >= 1 time
            n_recalled = c.execute(
                "SELECT COUNT(DISTINCT s.memory_id) c FROM memory_signals s "
                "WHERE s.recall_count > 0"
            ).fetchone()["c"]
            citation = (n_recalled / max(1, n_total)) * 100.0
            # Decay rate = share of memories with score < 0.3
            decay = (n_decayed / max(1, n_total)) * 100.0

            # --- 2) Lifecycle stage counts ---
            # Six stages: 已提取 / 活跃 / 已衰减 / 已合并 / 已归档 / 已遗忘
            stages = {
                "extracted": c.execute(
                    "SELECT COUNT(*) c FROM memories WHERE created_at >= ?",
                    (now - 86400,),
                ).fetchone()["c"],
                "active": n_active,
                "decayed": n_decayed,
                "merged": c.execute(
                    "SELECT COUNT(*) c FROM wiki_pages"
                ).fetchone()["c"],
                "archived": c.execute(
                    "SELECT COUNT(*) c FROM memories "
                    "WHERE score < 0.15 AND score > 0"
                ).fetchone()["c"],
                "forgotten": c.execute(
                    "SELECT COUNT(*) c FROM memories WHERE score = 0"
                ).fetchone()["c"],
            }

            # --- 3) Compression candidates ---
            # A memory is "compressible" if its text is long AND it has
            # been distilled into a wiki page.
            wiki_ids = {r[0] for r in c.execute(
                "SELECT id FROM memories WHERE id IN "
                "(SELECT DISTINCT json_each.value FROM wiki_pages, json_each(evidence_ids))"
            ).fetchall()}
            compressible = c.execute(
                "SELECT id, kind, text, length(text) AS L, importance, score "
                "FROM memories WHERE length(text) > 280 "
                "ORDER BY length(text) DESC LIMIT 30"
            ).fetchall()
            compression = {
                "compressible_count": len(compressible),
                "avg_length": int(c.execute(
                    "SELECT AVG(length(text)) FROM memories"
                ).fetchone()[0] or 0),
                "compression_progress": 0,
                "items": [
                    {
                        "id": r["id"],
                        "kind": r["kind"],
                        "preview": (r["text"] or "")[:80],
                        "length": r["L"],
                        "importance": r["importance"] or 0.0,
                        "score": r["score"] or 0.0,
                        "in_wiki": r["id"] in wiki_ids,
                    }
                    for r in compressible
                ],
            }
            # progress = wiki_count / compressible_count
            if compression["compressible_count"] > 0:
                compression["compression_progress"] = round(
                    min(100.0, (len(wiki_ids) / compression["compressible_count"]) * 100.0),
                    1,
                )

            # --- 4) Memory granularity buckets ---
            # Core: kind=fact + importance >= 0.7 + score >= 0.5
            # Working: anything else not in Core/Scratch
            # Scratch: low score AND low importance OR has been merged
            core = c.execute(
                "SELECT id, text, importance, score FROM memories "
                "WHERE kind='fact' AND importance >= 0.7 AND score >= 0.5 "
                "ORDER BY score DESC LIMIT 24"
            ).fetchall()
            scratch = c.execute(
                "SELECT id, text, importance, score FROM memories "
                "WHERE (importance < 0.4 AND score < 0.4) OR score < 0.15 "
                "ORDER BY score ASC LIMIT 24"
            ).fetchall()
            working = c.execute(
                "SELECT id, text, importance, score FROM memories "
                "WHERE id NOT IN (SELECT id FROM memories WHERE kind='fact' "
                "  AND importance >= 0.7 AND score >= 0.5) "
                "  AND id NOT IN (SELECT id FROM memories WHERE "
                "  (importance < 0.4 AND score < 0.4) OR score < 0.15) "
                "ORDER BY score DESC LIMIT 24"
            ).fetchall()

            def _row(r):
                return {
                    "id": r["id"],
                    "text": (r["text"] or "")[:60],
                    "importance": r["importance"] or 0.0,
                    "score": r["score"] or 0.0,
                }
            granularity = {
                "core_count": c.execute(
                    "SELECT COUNT(*) c FROM memories "
                    "WHERE kind='fact' AND importance >= 0.7 AND score >= 0.5"
                ).fetchone()["c"],
                "working_count": c.execute(
                    "SELECT COUNT(*) c FROM memories "
                    "WHERE NOT (kind='fact' AND importance >= 0.7 AND score >= 0.5) "
                    "AND NOT ((importance < 0.4 AND score < 0.4) OR score < 0.15)"
                ).fetchone()["c"],
                "scratch_count": c.execute(
                    "SELECT COUNT(*) c FROM memories "
                    "WHERE (importance < 0.4 AND score < 0.4) OR score < 0.15"
                ).fetchone()["c"],
                "core": [_row(r) for r in core],
                "working": [_row(r) for r in working],
                "scratch": [_row(r) for r in scratch],
            }

            # --- 5) Data distribution: type / status / 7-day trend ---
            type_rows = c.execute(
                "SELECT kind, COUNT(*) c FROM memories GROUP BY kind ORDER BY c DESC"
            ).fetchall()
            status_rows = c.execute(
                "SELECT CASE "
                "  WHEN score >= 0.5 THEN 'active' "
                "  WHEN score >= 0.15 THEN 'decayed' "
                "  WHEN score = 0 THEN 'forgotten' "
                "  ELSE 'archived' "
                "END AS s, COUNT(*) c FROM memories GROUP BY s"
            ).fetchall()
            trend_rows = c.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) c "
                "FROM memories WHERE created_at >= strftime('%s','now','-7 days') "
                "GROUP BY d ORDER BY d"
            ).fetchall()
            # Build a dense 7-day series (filling gaps with 0)
            import datetime as _dt
            today = _dt.date.today()
            trend_map = {r["d"]: r["c"] for r in trend_rows}
            trend_series = []
            for i in range(6, -1, -1):
                d = (today - _dt.timedelta(days=i)).isoformat()
                trend_series.append({"date": d, "count": trend_map.get(d, 0)})

            distribution = {
                "types": [{"kind": r["kind"], "count": r["c"]} for r in type_rows],
                "status": [{"status": r["s"], "count": r["c"]} for r in status_rows],
                "trend": trend_series,
            }

            # --- 6) Self-improvement Pulse: contradiction pairs ---
            # Cheap heuristic: 2 memories share >= 4 distinct tags and
            # have contradicting importance deltas (one very high, one
            # mid/low), suggesting they should be merged.
            # --- 5b) Sources breakdown (real, not synthetic) ---
            # Normalize per-thread codex sources to a single bucket.
            src_rows = c.execute(
                "SELECT source, COUNT(*) c FROM memories "
                "WHERE source IS NOT NULL AND source != '' "
                "GROUP BY source ORDER BY c DESC"
            ).fetchall()
            bucket = {}
            for r in src_rows:
                s = r["source"] or "unknown"
                # Collapse long thread ids into their parent prefix
                key = s.split("/")[0] if "/" in s else s
                bucket[key] = bucket.get(key, 0) + r["c"]
            sources = [
                {"source": k, "count": v}
                for k, v in sorted(bucket.items(), key=lambda x: -x[1])
            ]

            # --- 5c) Wiki health ---
            wiki_count = c.execute(
                "SELECT COUNT(*) c FROM wiki_pages"
            ).fetchone()["c"]
            wiki_avg_imp = c.execute(
                "SELECT AVG(importance) FROM wiki_pages"
            ).fetchone()[0] or 0.0
            wiki_total_chars = c.execute(
                "SELECT COALESCE(SUM(length(body)), 0) FROM wiki_pages"
            ).fetchone()[0] or 0
            # how many memories are referenced by at least one wiki page
            wiki_ref_count = c.execute(
                "SELECT COUNT(DISTINCT json_each.value) c "
                "FROM wiki_pages, json_each(wiki_pages.evidence_ids) "
                "WHERE evidence_ids IS NOT NULL"
            ).fetchone()["c"]

            # --- 5d) Ingest rate (24 hourly buckets) ---
            hourly_rows = c.execute(
                "SELECT strftime('%H', created_at, 'unixepoch', 'localtime') AS h, "
                "COUNT(*) c FROM memories "
                "WHERE created_at >= strftime('%s','now','-24 hours') "
                "GROUP BY h ORDER BY h"
            ).fetchall()
            hourly_map = {r["h"]: r["c"] for r in hourly_rows}
            hourly = [{"hour": f"{int(h):02d}:00", "count": hourly_map.get(f"{int(h):02d}", 0)}
                      for h in range(24)]

            # --- 5e) Recall throughput (last 24h) ---
            recall_rows = c.execute(
                "SELECT COALESCE(SUM(recall_count), 0) AS c, "
                "COUNT(DISTINCT memory_id) AS uniq "
                "FROM memory_signals "
                "WHERE last_recalled_at >= strftime('%s','now','-24 hours')"
            ).fetchone()
            recall_total_24h = recall_rows["c"] or 0
            recall_uniq_24h = recall_rows["uniq"] or 0

            pulse = {"contradictions": [], "score_distribution": []}
            c.execute(
                "SELECT tags FROM memories WHERE tags IS NOT NULL AND tags != '' "
                "AND length(tags) > 5"
            ).fetchall()
            # (Real contradiction detection needs an LLM; we surface
            # near-duplicates so the user can pick which to merge.)
            sample = c.execute(
                "SELECT id, text, importance, score, tags FROM memories "
                "WHERE length(text) > 120 ORDER BY created_at DESC LIMIT 80"
            ).fetchall()
            from collections import defaultdict
            tag_buckets = defaultdict(list)
            for r in sample:
                try:
                    import json as _json
                    tags = _json.loads(r["tags"]) if r["tags"] else []
                except Exception:
                    tags = []
                for t in tags[:3]:
                    tag_buckets[t].append(r)
            # Filter out pairs the user already resolved.
            ignored = store.list_ignored_pairs()
            for tag, rows in list(tag_buckets.items())[:6]:
                if len(rows) >= 2:
                    a, b = rows[0], rows[1]
                    if store.pair_key(a["id"], b["id"]) in ignored:
                        continue
                    pulse["contradictions"].append({
                        "tag": tag,
                        "a": {"id": a["id"], "text": (a["text"] or "")[:120],
                               "importance": a["importance"] or 0,
                               "score": a["score"] or 0},
                        "b": {"id": b["id"], "text": (b["text"] or "")[:120],
                               "importance": b["importance"] or 0,
                               "score": b["score"] or 0},
                        "similarity": 0.78,  # placeholder
                    })

            # Score distribution histogram (0.0 .. 1.0 in 0.1 bins)
            for i in range(10):
                lo, hi = i * 0.1, (i + 1) * 0.1
                cnt = c.execute(
                    "SELECT COUNT(*) c FROM memories WHERE score >= ? AND score < ?",
                    (lo, hi if hi < 1 else 2.0),
                ).fetchone()["c"]
                pulse["score_distribution"].append({
                    "range": [round(lo, 1), round(hi, 1)],
                    "count": cnt,
                })

            # --- 7) Pipeline latency / skill pipeline ---
            # Average ms per stage from pipeline_runs.
            stages_pipeline = []
            for stage in ("score", "cluster", "distill", "wiki", "graph"):
                row = c.execute(
                    "SELECT AVG((finished_at - started_at) * 1000) AS ms, COUNT(*) c "
                    "FROM pipeline_runs WHERE stage=? AND finished_at IS NOT NULL",
                    (stage,),
                ).fetchone()
                stages_pipeline.append({
                    "stage": stage,
                    "avg_ms": int(row["ms"] or 0),
                    "count": row["c"] or 0,
                })

        return {
            "now": now,
            "overview": {
                "total": n_total,
                "today": n_today,
                "active": n_active,
                "links": n_links,
                "clusters": n_clusters,
                "avg_score": round(avg_score, 3),
                "decay_pct": round(decay, 1),
                "entities": n_entities,
                "occupation": round(occupation, 1),
                "citation": round(citation, 1),
                "decay": round(decay, 1),
            },
            "stages": stages,
            "compression": compression,
            "granularity": granularity,
            "distribution": distribution,
            "sources": sources,
            "wiki_health": {
                "pages": wiki_count,
                "avg_importance": round(wiki_avg_imp, 3),
                "total_chars": int(wiki_total_chars),
                "referenced_memories": wiki_ref_count,
            },
            "ingest_rate": hourly,
            "recall_24h": {
                "total": recall_total_24h,
                "unique_memories": recall_uniq_24h,
            },
            "pulse": pulse,
            "pipeline": stages_pipeline,
        }

    # ====================================================================
    # /api/weekly-report — natural-language weekly digest of what was learned
    # ====================================================================
    @app.get("/api/weekly-report")
    def weekly_report(
        days: int = 7,
        max_facts: int = 60,
        use_llm: bool = True,
        lang: str = "zh",
    ):
        """Build a Markdown weekly report covering the last ``days`` days."""
        import datetime as _dt
        from ..llm.providers import build_provider, default_config
        from ..llm.base import ChatHistory, Message
        from ..storage.sqlite_store import LLMAuditStore

        lang = "en" if str(lang).lower().startswith("en") else "zh"
        now = time.time()
        since = now - days * 86400
        with store._conn() as c:
            rows = c.execute(
                """SELECT id, kind, text, importance, source, tags, score,
                          datetime(created_at, 'unixepoch') AS ts
                   FROM memories
                   WHERE created_at >= ?
                   ORDER BY importance DESC, score DESC, created_at DESC
                   LIMIT ?""",
                (since, max_facts),
            ).fetchall()
            source_rows = c.execute(
                """SELECT COALESCE(source, 'unknown') AS s, COUNT(*) c
                   FROM memories WHERE created_at >= ? GROUP BY s ORDER BY c DESC""",
                (since,),
            ).fetchall()
            total_window = c.execute(
                "SELECT COUNT(*) c FROM memories WHERE created_at >= ?", (since,)
            ).fetchone()["c"]

        # Normalize sources: collapse per-thread codex IDs into the parent
        # "codex" bucket so the UI doesn't show one noisy row per thread.
        norm: dict[str, int] = {}
        for r in source_rows:
            key = (r["s"] or "unknown").split("/")[0]
            norm[key] = norm.get(key, 0) + int(r["c"])
        normalized_sources = [
            {"source": k, "count": v} for k, v in sorted(norm.items(), key=lambda x: -x[1])
        ]

        items = [dict(r) for r in rows]
        sources = [{"source": r["s"], "count": r["c"]} for r in source_rows]
        highlights = items[: max(3, max_facts // 4)]
        lowlights = sorted(items, key=lambda x: (x.get("importance") or 0, x.get("score") or 0))[: max(3, max_facts // 6)]

        stats = {
            "window_days": days,
            "since": since,
            "now": now,
            "total_in_window": total_window,
            "sources": sources,
            "sources_normalized": normalized_sources,
            "highlight_count": len(highlights),
            "lowlight_count": len(lowlights),
        }

        llm_used = False
        summary_md = ""
        prov_name = "none"
        provider = None
        llm_error = None
        llm_error_kind = None  # "no_provider" | "no_key" | "auth_failed" | "network" | "other"
        llm_hint = None        # user-facing hint for the banner
        try:
            cfg = store.get_setting("llm_consolidator", default_config()) or {}
            provider = build_provider(cfg)
            prov_name = type(provider).__name__
        except Exception as e:
            llm_error = f"{type(e).__name__}: {e}"
            llm_error_kind = "no_provider"
            llm_hint = (
                "LLM provider not configured. Open Settings → Models to pick one."
                if lang == "en"
                else "尚未配置大模型提供方，请前往“设置 → 模型”选择一个提供方。"
            )
            provider = None

        # Detect placeholder / empty API keys (the most common cause of "auth
        # failed" on a fresh install). The shipped secret store contains
        # a recognizable placeholder by default — anyone who never opened
        # the settings page is hitting that.
        raw_key = getattr(provider, "api_key", None) or ""
        # Only flag *clearly fake* placeholders, not real keys that happen to
        # contain \'x\'. Common patterns: "sk-xxxx...", "your-api-key",
        # "REPLACE_ME", "<API_KEY>", or fewer than 8 non-whitespace chars.
        if (not raw_key
                or len(raw_key.strip()) < 8
                or re.search(r"x{4,}", raw_key)
                or re.search(r"(your[-_ ]?(api[-_ ]?)?key|replace[-_ ]?me|<api[-_ ]?key>|placeholder)", raw_key, re.I)):
            llm_error_kind = "no_key"
            llm_error = llm_error or "API key not configured"
            llm_hint = (
                f"No real API key for {prov_name}. Open Settings → Models, paste your key, "
                "and Save. Stored locally in ~/.loop_memory/secrets.json — never sent anywhere else."
                if lang == "en"
                else f"{prov_name} 尚未配置有效 API Key。请前往“设置 → 模型”粘贴并保存。"
                "密钥仅保存在本机 ~/.loop_memory/secrets.json，不会发送到其他位置。"
            )
            provider = None  # skip the LLM call entirely

        if use_llm and provider is not None:
            t0 = time.time()
            try:
                if lang == "en":
                    sys_prompt = (
                        "You are a memory-system reporter. Given a JSON snapshot of a user's "
                        "recent memories, write a concise weekly report in English Markdown. "
                        "Output exactly 3 sections: ## Highlights (3-5 bullets), "
                        "## Lowlights (2-3 bullets, things to review / prune / forget), "
                        "## Next focus (1-2 bullets suggesting what to remember better next week). "
                        "Keep the total under 250 words."
                    )
                else:
                    sys_prompt = (
                        "你是记忆系统周报助手。根据用户近期记忆的 JSON 快照，使用中文 Markdown "
                        "生成简洁周报。必须只输出 3 个章节：## 本周亮点（3-5 条）、"
                        "## 待整理内容（2-3 条需要复查、裁剪或遗忘的内容）、"
                        "## 下周重点（1-2 条下周应重点积累的知识）。总字数不超过 250 字。"
                    )
                user_payload = {
                    "window": f"{days} days",
                    "total_new_memories": total_window,
                    "sources": normalized_sources,
                    "top_memories": [
                        {
                            "kind": i["kind"], "imp": round(i["importance"] or 0, 2),
                            "score": round(i["score"] or 0, 2),
                            "source": i["source"],
                            "text": (i["text"] or "")[:160],
                        }
                        for i in items[:20]
                    ],
                }
                user_prompt = json.dumps(user_payload, ensure_ascii=False)
                history = ChatHistory(
                    system=sys_prompt,
                    messages=[Message(role="user", content=user_prompt)],
                )
                try:
                    reply = provider.complete(history, temperature=0.4, max_tokens=600) or ""
                except Exception as e:
                    reply = ""
                    raw = f"{type(e).__name__}: {e}"
                    llm_error = raw
                    # Pull structured fields from LLMHttpError when available.
                    status = getattr(e, "status", None)
                    pcode  = getattr(e, "provider_code", None) or ""
                    pmsg   = getattr(e, "provider_message", None) or ""
                    sl = str(e).lower()
                    if status is None:
                        m = re.search(r"HTTP\s+(\d+)", str(e))
                        status = int(m.group(1)) if m else 0
                    if (status in (401, 403)
                            or "2049" in pcode or "1004" in pcode
                            or "authorized" in sl or "invalid api key" in sl):
                        llm_error_kind = "auth_failed"
                    elif status == 429 or "rate" in sl:
                        llm_error_kind = "rate_limited"
                    elif status >= 500 or status == 0 or "timeout" in sl or "connection" in sl:
                        llm_error_kind = "network"
                    else:
                        llm_error_kind = "other"
                    llm_hint = _hint_for_llm_error(prov_name, status or 0, pcode, pmsg)
                    # Keep the bare kind; provider code goes in its own field.
                    llm_provider_code_resp = pcode
                    llm_provider_message_resp = pmsg
                    log.warning("weekly-report LLM call failed [kind=%s, status=%s, code=%s]: %s",
                                llm_error_kind, status, pcode, raw)
                llm_used = bool(reply.strip())
                summary_md = reply.strip()
                # Audit
                try:
                    LLMAuditStore(store).record(
                        provider=type(provider).__name__,
                        model=getattr(provider, "model", "?") or "?",
                        kind="weekly_report",
                        prompt=sys_prompt + "\n" + user_prompt,
                        response=reply,
                        prompt_tokens=max(1, len(sys_prompt) // 4) + max(1, len(user_prompt) // 4),
                        completion_tokens=max(1, len(reply) // 4) if reply else 0,
                        cost_usd=0.0,
                        latency_ms=int((time.time() - t0) * 1000),
                        ok=llm_used,
                    )
                except Exception:
                    pass
            except Exception as e:
                llm_error = f"{type(e).__name__}: {e}"

        if not summary_md:
            # Templated fallback
            if lang == "en":
                lines = [f"# Memory weekly · {_dt.date.today().isoformat()}", ""]
                lines.append(f"- Window: {days} days")
                lines.append(f"- New memories: {total_window}")
            else:
                lines = [f"# 记忆周报 · {_dt.date.today().isoformat()}", ""]
                lines.append(f"- 时间窗口：{days} 天")
                lines.append(f"- 新增记忆：{total_window} 条")
            if sources:
                src_str = ", ".join(f"{s['source']}={s['count']}" for s in sources[:5])
                separator = ": " if lang == "en" else "："
                lines.append(f"- {'Source distribution' if lang == 'en' else '来源分布'}{separator}{src_str}")
            lines.append("")
            lines.append("## Highlights" if lang == "en" else "## 本周亮点")
            for h in highlights[:5]:
                lines.append(f"- **[{h['source']}]** {h['text'][:100]}")
            lines.append("")
            lines.append("## Lowlights" if lang == "en" else "## 待整理内容")
            for lowlight in lowlights[:3]:
                lines.append(f"- *[{lowlight['source']}]* {lowlight['text'][:100]}")
            lines.append("")
            lines.append("## Next focus" if lang == "en" else "## 下周重点")
            lines.append(
                "- Keep capturing, distill regularly, and retain high-quality memories"
                if lang == "en"
                else "- 持续记录、定期蒸馏，并保留高质量记忆"
            )
            summary_md = "\n".join(lines)

        # Surface enough provider / key info that the UI can render a
        # specific "what went wrong" hint (key prefix, provider code, etc.)
        # without making the user re-test from the settings page.
        prov_code = locals().get("llm_provider_code_resp") or None
        prov_msg = locals().get("llm_provider_message_resp") or None
        if provider is not None:
            rk = getattr(provider, "api_key", None) or ""
            kp = (rk[:10] + "...") if len(rk) > 10 else rk
            klen = len(rk)
        else:
            kp = ""
            klen = 0
        return {
            "markdown": summary_md,
            "stats": stats,
            "highlights": highlights[:5],
            "lowlights": lowlights[:3],
            "llm_used": llm_used,
            "llm_provider": prov_name,
            "llm_error": llm_error,
            "llm_error_kind": llm_error_kind,
            "llm_provider_code": prov_code,
            "llm_provider_message": prov_msg,
            "llm_hint": llm_hint,
            "llm_key_prefix": kp,
            "llm_key_len": klen,
            "generated_at": now,
        }

    # ====================================================================
    # /api/llm-audit — surface the LLM audit log
    # ====================================================================
    @app.get("/api/llm-audit")
    def llm_audit(limit: int = 50, kind: str | None = None, since: float | None = None):
        from ..storage.sqlite_store import LLMAuditStore
        audit = LLMAuditStore(store)
        recent = audit.recent(limit=limit, kind=kind)
        s = audit.stats(since_ts=since)
        return {"recent": recent, "stats": s}

    # ====================================================================
    # /api/write-guard — live drop counts + last-rejected timestamps
    # ====================================================================
    @app.get("/api/write-guard")
    def write_guard(window_hours: float = 24 * 7):
        from ..storage.sqlite_store import WriteGuardDropStore
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
        from ..cli.commands.diag import (
            _detect_clients, _detect_watcher,
        )
        from ..llm.providers import default_config
        from ..security import (
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
              bump: int = 1):
        """Unified recall — wiki + memories + entities in one ranked stream.

        Powers the CLI `recall`/`ask`/`inject` and the web UI Timeline.
        ``include`` is a comma-separated subset of {memories, wiki, entities}.
        ``bump=0`` skips the recall_count bump (useful for preview-only
        queries). Returns ``{query, tokens, memories, wiki, entities}``.
        """
        wanted = tuple(s.strip() for s in include.split(",") if s.strip())
        if not wanted:
            wanted = ("memories", "wiki", "entities")
        r = store.recall(query, limit=limit, include=wanted, bump_signals=bool(bump))
        return {
            "query": query,
            "tokens": r.get("tokens", []),
            "memories": r.get("memories", []),
            "wiki": r.get("wiki", []),
            "entities": r.get("entities", []),
        }

    @app.get("/api/pipeline")
    def pipeline_dashboard_route():
        """5-stage data flow dashboard. See handlers.pipeline_dashboard."""
        return pipeline_dashboard(store)

    @app.get("/api/pipeline/{stage}/items")
    def pipeline_stage_items_route(stage: str, limit: int = 50):
        """Drill-down: see handlers.pipeline_stage_items."""
        return pipeline_stage_items(store, stage, limit=limit)


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

    @app.get("/api/memories")
    def memories(
        source: str | None = None,
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
                kind=kind,
                min_score=min_score,
                since=since,
                until=until,
                query=q,
                limit=limit,
            )
        return [_memory_to_dict(m) for m in rows]

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

    @app.delete("/api/memories/{mid}")
    def delete_memory(mid: str):
        n = store.delete_memory(mid)
        if n == 0:
            raise HTTPException(404, "memory not found")
        return {"deleted": n}

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str):
        n = store.delete_session(sid)
        return {"deleted": n}

    @app.post("/api/admin/rescore")
    def rescore(half_life_days: float = 30.0):
        return {"updated": store.rescore_all(half_life_days)}

    @app.post("/api/admin/gc")
    def gc():
        return {"deleted": store.gc()}

    @app.post("/api/admin/consolidate")
    def consolidate():
        from ..backends.embedding import HashingEmbedder
        from ..jobs.consolidate import Consolidator
        report = Consolidator(store, embedder=HashingEmbedder(dim=64)).run()
        return {
            "rescored": report.rescored,
            "gc_removed": report.gc_removed,
            "merged": report.merged,
            "elapsed_ms": round(report.elapsed_ms, 1),
        }

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

    @app.post("/api/admin/ingest")
    def ingest(source: str, path: str | None = None):
        from ..backends.embedding import HashingEmbedder
        from ..ingest.loader import default_paths, get_loader
        from ..ingest.pipeline import MemoryPipeline

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

    @app.get("/api/graph")
    def graph(limit_entities: int = 200, limit_relations: int = 1000):
        ents = store.list_entities(limit=limit_entities)
        rels = store.list_relations(limit=limit_relations)
        return {
            "entities": [
                {
                    "id": e.id, "name": e.name, "kind": e.kind,
                    "mention_count": e.mention_count, "weight": round(e.weight, 3),
                }
                for e in ents
            ],
            "node_kinds": sorted({e.kind for e in ents}),
            "relations": [
                {
                    "id": r.id, "src": r.src, "dst": r.dst,
                    "kind": r.kind, "weight": round(r.weight, 3),
                    "evidence": r.evidence_ids[:5],
                }
                for r in rels
            ],
            "stats": store.graph_stats(),
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
        from ..graph.build import KnowledgeGraph
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
        from ..llm.providers import PROVIDERS
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
        import time as _time
        from ..llm.providers import default_config, validate_config
        from ..security import (
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
                    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
                    cfg["api_key_fingerprint"] = f"{tail}·{h}"
                # Use the secret file mtime as a sane fallback so the chip
                # shows a real timestamp instead of "—".
                try:
                    import os as _os
                    from pathlib import Path as _Path
                    from ..security.secrets import _pick_backend
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
        from ..llm.providers import default_config, validate_config
        from ..security import account_for, delete_secret, has_secret, set_secret
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
                h = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:6]
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
        # If the user just cleared the key, the fingerprint is also
        # cleared on the in-memory cfg already; otherwise default to
        # what's in the body (the fresh fingerprint we just set).
        cfg["api_key_fingerprint"] = body.get("api_key_fingerprint", cfg.get("api_key_fingerprint", ""))
        cfg["api_key_saved_at"] = body.get("api_key_saved_at", cfg.get("api_key_saved_at", 0))
        store.set_setting("llm_consolidator", cfg)
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.reload_config()
        from ..security import backend_display_name, backend_name
        return {
            "ok": True,
            "config": cfg,
            "warnings": warnings,
            "secret_backend": backend_name(),
            "secret_backend_display": backend_display_name(),
            "api_key_fingerprint": cfg.get("api_key_fingerprint", ""),
            "api_key_saved_at": cfg.get("api_key_saved_at", 0),
        }

    @app.post("/api/admin/llm/test")
    async def llm_test_route(body: dict | None = None):
        """Smoke-test the LLM provider without writing to the store.
        See handlers.llm_test for the body contract."""
        return llm_test(store, body or {})

    @app.get("/api/admin/llm/status")
    def llm_status():
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is None:
            return {"running": False, "reason": "scheduler-not-started"}
        return scheduler.status()

    @app.delete("/api/admin/llm/key")
    def llm_clear_key():
        """Delete the API key for the currently-configured provider."""
        from ..llm.providers import default_config, validate_config
        from ..security import account_for, delete_secret, has_secret
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
        from ..jobs.llm_consolidate import LLMConsolidator
        from ..llm.providers import build_provider, default_config, validate_config
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
        from ..llm.providers import default_config, validate_config
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


    # ---- wiki pages -----------------------------------------------------

    @app.get("/api/wiki/export")
    def wiki_export(format: str = "markdown", limit: int = 500,
                    q: str | None = None):
        """Dump all (or matched) wiki pages as a single markdown string.

        ``format`` is currently only ``markdown``. ``q`` (optional)
        filters by substring match against the page title or body.
        """
        try:
            pages = store.list_wiki_pages(limit=limit, query=q)
        except Exception:
            pages = []
        out_lines = ["# Loop Memory — Distilled Knowledge", ""]
        out_lines.append(f"_Exported {len(pages)} wiki pages._")
        out_lines.append("")
        for p in pages:
            title   = (p.get("title") or "untitled").strip()
            body    = (p.get("body") or "").strip()
            summary = (p.get("summary") or "").strip()
            out_lines.append(f"## {title}")
            out_lines.append("")
            if summary and summary != title:
                out_lines.append(f"> {summary}")
                out_lines.append("")
            out_lines.append(body)
            out_lines.append("")
            evidence = p.get("evidence_ids") or []
            if evidence:
                ev = ", ".join(str(x) for x in evidence[:6])
                out_lines.append(f"<sub>evidence: {ev}… ({len(evidence)} sources)</sub>")
                out_lines.append("")
        return {"format": format, "count": len(pages), "markdown": "\n".join(out_lines)}

    @app.get("/api/wiki/{page_id}/export")
    def wiki_page_export(page_id: str, format: str = "markdown"):
        """Export a single wiki page as a context-block ready to paste
        into another LLM client as a system prompt."""
        page = store.get_wiki_page(page_id)
        if page is None:
            raise HTTPException(404, "wiki page not found")
        title   = (page.get("title") or "untitled").strip()
        body    = (page.get("body") or "").strip()
        summary = (page.get("summary") or title).strip()
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
            ctx_lines.append(
                f"\n## {p.get('title')}\n"
                f"{(p.get('summary') or '').strip()}\n"
                f"{(p.get('body') or '').strip()[:800]}"
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
        return store.upsert_wiki_page(
            slug=slug.lower().replace(" ", "-")[:80],
            title=title,
            body=body_text,
            summary=(body.get("summary") or "").strip(),
            tags=body.get("tags") or [],
            importance=float(body.get("importance") or 0.5),
            evidence_ids=body.get("evidence_ids") or [],
        )

    @app.put("/api/wiki/{page_id}")
    def wiki_update(page_id: str, body: dict):
        existing = store.get_wiki_page(page_id)
        if not existing:
            raise HTTPException(404, "wiki page not found")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        slug = (body.get("slug") or existing["slug"]).strip()
        return store.upsert_wiki_page(
            slug=slug.lower().replace(" ", "-")[:80],
            title=(body.get("title") or existing["title"]).strip(),
            body=(body.get("body") or existing["body"]).strip(),
            summary=(body.get("summary") or existing.get("summary") or "").strip(),
            tags=body.get("tags") if "tags" in body else existing.get("tags") or [],
            importance=float(body.get("importance")
                             if "importance" in body else existing.get("importance") or 0.5),
            evidence_ids=body.get("evidence_ids")
                         if "evidence_ids" in body else existing.get("evidence_ids") or [],
        )

    @app.delete("/api/wiki/{page_id}")
    def wiki_delete(page_id: str):
        ok = store.delete_wiki_page(page_id)
        if not ok:
            raise HTTPException(404, "wiki page not found")
        return {"ok": True}

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
        from ..llm.providers import build_provider, default_config, validate_config
        cfg, _ = validate_config(store.get_setting("llm_consolidator", default_config()))
        provider = build_provider(cfg)
        from ..jobs.llm_consolidate import LLMConsolidator
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

    @app.get("/api/graph/entity/{name}/memories")
    def graph_entity_memories(name: str, limit: int = 30):
        # match by LIKE on the entity name appearing in memory text
        rows = store.list_memories(query=name, limit=limit)
        return [_memory_to_dict(m) for m in rows]
    return app


# Backwards-compatible aliases. The implementations live in
# ``serve.handlers`` so they can be unit-tested without spinning up
# a FastAPI app.
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

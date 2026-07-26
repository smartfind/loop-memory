"""Route group: insights.

Heavy computed views (insights + weekly report).

All routes were extracted from ``serve/app.py`` as part of the O1
refactor to keep the central ``create_app`` small. Each block lives
inside ``register(app, store, scheduler=None)`` so closures over the
three captured variables work unchanged from the original layout.
"""
from __future__ import annotations

from typing import Any, Optional

import re
from datetime import datetime
from pathlib import Path
import time
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ...storage.sqlite_store import MemoryStore
from ._shared import _memory_to_dict, _export_safe_segment


def register(app: FastAPI, store: MemoryStore, scheduler: Optional[Any] = None) -> None:
    """Mount every route in this bucket onto ``app``.

    ``store`` and ``scheduler`` are captured in the route closures so
    the function bodies stay byte-identical to the pre-split layout.
    """
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
        force: bool = False,
    ):
        """Build a Markdown weekly report covering the last ``days`` days.

        The full LLM-backed report is heavy (several seconds + 600+ tokens
        of output) and the user only needs a new digest when the week
        rolls over. We therefore cache the rendered response to disk
        under ``~/.loop_memory/cache/weekly/{lang}-{days}-{week_start}.json``
        and serve it as-is on subsequent calls within the same ISO week.

        Pass ``?force=true`` to bypass the cache and regenerate now
        (this is what the manual refresh button wires up).
        """
        import datetime as _dt
        from ...llm.providers import build_provider, default_config
        from ...llm.base import ChatHistory, Message
        from ...storage.sqlite_store import LLMAuditStore

        lang = "en" if str(lang).lower().startswith("en") else "zh"

        # ---- weekly report cache ----------------------------------------
        # The cache key is intentionally simple: same language + same
        # window + same ISO week (Monday 00:00 in the user's local
        # timezone) = same report. When Monday rolls around the key
        # changes and a fresh report is generated.
        cache_dir = Path.home() / ".loop_memory" / "cache" / "weekly"
        _now_local = _dt.datetime.now().astimezone()
        _iso_week = _now_local.isocalendar()  # (year, week, weekday)
        _week_start = (_now_local - _dt.timedelta(days=_iso_week.weekday - 1)
                       ).replace(hour=0, minute=0, second=0, microsecond=0)
        _cache_key = f"{lang}-{days}-{_iso_week.year}-W{_iso_week.week:02d}"
        _cache_path = cache_dir / f"{_cache_key}.json"
        if not force and use_llm:
            try:
                if _cache_path.exists():
                    cached = json.loads(_cache_path.read_text(encoding="utf-8"))
                    cached["from_cache"] = True
                    cached["cache_key"] = _cache_key
                    return cached
            except Exception:
                # Corrupt cache file: fall through and regenerate.
                pass

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
        thinking_md = ""
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
                    reply = provider.complete(history, temperature=0.3, max_tokens=1200) or ""
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
                summary_md, thinking_md = _split_think(reply.strip())
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
        result = {
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
            "thinking": thinking_md,
            "generated_at": now,
            "cache_key": _cache_key,
            "week_start": _week_start.isoformat(),
            "from_cache": False,
        }
        # Persist for next call within the same ISO week. Only cache
        # the LLM-backed reports — the templated fallback is cheap to
        # recompute and changes on every memory write, so caching it
        # would actually make the report go stale.
        if use_llm and llm_used:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                _cache_path.write_text(
                    json.dumps(result, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return result

    # ====================================================================
    # /api/llm-audit — surface the LLM audit log
    # ====================================================================



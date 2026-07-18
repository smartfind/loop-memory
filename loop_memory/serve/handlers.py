"""Module-level handler functions used by ``serve.app.create_app``.

These used to be inline closures inside ``create_app`` (so the file
ballooned to 960 lines and was hard to unit-test). Pulling them out
keeps the FastAPI app definition small and lets us test the logic
in isolation.

Each function is a *pure* transform: it takes a ``MemoryStore`` and
the parsed request arguments, and returns a JSON-serialisable dict
(or raises ``HTTPException``). The endpoint closure in ``app.py``
just wires the HTTP signature to the handler.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

from fastapi import HTTPException

PIPELINE_STAGES = ("ingest", "score", "cluster", "distill", "wiki", "memo")


def pipeline_dashboard(store) -> dict:
    """5-stage data flow: ingest -> score -> cluster -> distill -> wiki -> memo."""
    runs = store.latest_pipeline_runs(limit=120)
    by_stage: dict = {}
    for r in runs:
        by_stage.setdefault(r["stage"], []).append(r)
    stages_out = []
    for stage in PIPELINE_STAGES:
        entries = by_stage.get(stage, [])
        entries.sort(key=lambda x: -x["started_at"])
        stages_out.append({
            "stage": stage,
            "runs": entries[:5],
            "last_in": entries[0]["in_count"] if entries else 0,
            "last_out": entries[0]["out_count"] if entries else 0,
            "last_at": entries[0]["started_at"] if entries else None,
            "last_note": entries[0].get("note", "") if entries else "",
        })
    try:
        stats = store.stats()
    except Exception:
        stats = {}
    return {
        "stages": stages_out,
        "totals": {
            "memories": stats.get("memories", 0),
            "wiki_pages": stats.get("wiki_pages", 0),
            "wiki_avg_importance": stats.get("wiki_avg_importance", 0),
            "avg_score": stats.get("avg_score", 0),
        },
    }


def pipeline_stage_items(store, stage: str, limit: int = 50) -> dict:
    """Drill-down for a single stage: latest run + touched memories."""
    runs = store.latest_pipeline_runs(limit=200)
    runs = [r for r in runs if r["stage"] == stage]
    if not runs:
        return {"stage": stage, "run": None, "items": []}
    run = runs[0]
    try:
        stats = json.loads(run.get("stats_json") or "{}")
    except Exception:
        stats = {}
    evidence = stats.get("evidence_ids") or []
    items = []
    if isinstance(evidence, list) and evidence:
        for mid in evidence[:limit]:
            m = store.get_memory(mid)
            if m is not None:
                items.append({
                    "id": m.id, "kind": m.kind, "text": (m.text or "")[:300],
                    "importance": m.importance, "score": m.score,
                    "tags": list(m.tags or []),
                })
    else:
        for m in store.list_memories(limit=limit):
            items.append({
                "id": m.id, "kind": m.kind, "text": (m.text or "")[:300],
                "importance": m.importance, "score": m.score,
                "tags": list(m.tags or []),
            })
    return {
        "stage": stage,
        "run": {
            "id": run["id"],
            "started_at": run["started_at"],
            "finished_at": run.get("finished_at"),
            "in_count": run["in_count"],
            "out_count": run["out_count"],
            "note": run.get("note", ""),
            "stats": stats,
        },
        "items": items,
    }


def llm_test(store, body: dict) -> dict:
    """Smoke-test the LLM provider without writing to the store.

    Accepts ``api_key`` in the body for one-off testing. The key is
    stored in a *temporary* secret-backend account and deleted as soon as
    the test finishes. The structured response lets the UI show *why*
    a key failed (status, provider_code, hint) instead of a wall of
    raw JSON.
    """
    from ..llm.providers import (
        build_provider, default_config, validate_config, LLMHttpError,
    )
    from ..security import delete_secret, set_secret
    # Merge the body over the saved config so callers can override the
    # model/base_url without re-saving, and pass an ephemeral api_key in
    # the body to test before committing. When the body is empty, the
    # test runs against whatever the user has currently saved.
    saved = store.get_setting("llm_consolidator", default_config()) or {}
    body = dict(body or {})
    body.setdefault("provider", saved.get("provider"))
    body.setdefault("model", saved.get("model"))
    body.setdefault("base_url", saved.get("base_url"))
    body.setdefault("api_key_account", saved.get("api_key_account"))
    body.setdefault("api_key_set", saved.get("api_key_set"))
    provider = (body.get("provider") or "echo").lower()
    raw_key = body.pop("api_key", None)
    ephemeral_account = None
    if raw_key:
        ephemeral_account = f"llm-test/{provider}/{uuid.uuid4().hex[:8]}"
        set_secret(ephemeral_account, raw_key)
        body["api_key_account"] = ephemeral_account
        body["api_key_set"] = True
    cfg, warnings = validate_config(body or default_config())
    provider_obj = build_provider(cfg)
    real_key = getattr(provider_obj, "api_key", None) or ""
    base_info = {
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "base_url": getattr(provider_obj, "base_url", None),
        "key_prefix": (real_key[:10] + "...") if len(real_key) > 10 else real_key,
        "key_len": len(real_key),
        "warnings": warnings,
    }
    # Placeholder detection before we even hit the network.
    # Only flag *clearly fake* placeholders, not real keys that happen to
    # contain 'x'. Common patterns: "sk-xxxx...", "your-api-key",
    # "REPLACE_ME", "<API_KEY>", or fewer than 8 non-whitespace chars.
    if (not real_key
            or len(real_key.strip()) < 8
            or re.search(r"x{4,}", real_key)
            or re.search(r"(your[-_ ]?(api[-_ ]?)?key|replace[-_ ]?me|<api[-_ ]?key>|placeholder)", real_key, re.I)):
        return {
            **base_info,
            "ok": False,
            "elapsed_ms": 0,
            "error": {
                "status": 0,
                "provider_code": None,
                "provider_message": "API key not set or looks like a placeholder",
                "hint": "Paste a real API key in the field above and try again.",
            },
        }
    from ..llm.base import ChatHistory, Message
    t0 = time.time()
    try:
        reply = provider_obj.complete(
            ChatHistory(
                system="You are a connectivity probe. Reply with one word: ok",
                messages=[Message(role="user", content="Reply with the single word: ok")],
            ),
            temperature=0.0,
            max_tokens=10,
        )
        ok = bool((reply or "").strip())
        return {
            **base_info,
            "ok": ok,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "reply": (reply or "")[:200],
        }
    except LLMHttpError as e:
        hint = _hint_for_llm_error(
            base_info["provider"] or "?",
            e.status,
            e.provider_code or "",
            e.provider_message or "",
        )
        return {
            **base_info,
            "ok": False,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "error": {
                "status": e.status,
                "provider_code": e.provider_code,
                "provider_message": e.provider_message,
                "hint": hint,
            },
        }
    except Exception as e:
        return {
            **base_info,
            "ok": False,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "error": {
                "status": 0,
                "provider_code": None,
                "provider_message": f"{type(e).__name__}: {e}"[:200],
                "hint": "Could not reach the provider. Check network connectivity.",
            },
        }
    finally:
        if ephemeral_account:
            try:
                delete_secret(ephemeral_account)
            except Exception:
                pass


def _hint_for_llm_error(provider: str, status: int, code: str, msg: str) -> str:
    """Map an LLM HTTP error to a user-facing hint.

    MiniMax codes: 1004 missing auth header, 2049 invalid api key,
    1002 rate limit, 1008 insufficient balance, 1026 model not found.
    OpenAI / Anthropic surface standard HTTP statuses. We map both so
    the dashboard banner reads the same regardless of provider.
    """
    code = (code or "").strip()
    m = (msg or "").lower()
    if status in (401, 403) or "2049" in code or "1004" in code \
            or "unauthorized" in m or "authorized_error" in m \
            or "invalid api key" in m:
        return (
            f"{provider} rejected the API key. "
            "Verify it in your provider console (MiniMax: console.minimaxi.chat), "
            "then re-paste it here. The key prefix in the test result should match what you copied."
        )
    if status == 404 or ("model" in m and "not found" in m) or "1026" in code:
        return (
            f"The model name is wrong or not available on your {provider} plan. "
            "Pick another model from the dropdown."
        )
    if status == 429 or "rate" in m or "1002" in code:
        return (
            f"{provider} is rate-limiting. Wait a minute and retry, or upgrade your plan."
        )
    if status == 402 or "balance" in m or "1008" in code:
        return f"{provider} says your account is out of credit. Top up and retry."
    if status >= 500:
        return f"{provider} server error ({status}). Retry in a few seconds."
    if status == 0:
        return "Network error reaching the provider. Check connectivity."
    return f"{provider} call failed (HTTP {status}" + (f", code {code}" if code else "") + ")."


def memory_to_dict(m) -> dict:
    return {
        "id": m.id,
        "kind": m.kind,
        "text": m.text,
        "importance": round(m.importance, 3),
        "score": round(m.score, 4),
        "source": m.source,
        "session_id": m.session_id,
        "created_at": m.created_at,
        "updated_at": getattr(m, "updated_at", None) or m.created_at,
        "tags": m.tags,
    }


def session_to_dict(s) -> dict:
    return {
        "id": s.id,
        "source": s.source,
        "external_id": s.external_id,
        "title": s.title,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "message_count": s.message_count,
    }


def require_scheduler(app) -> Any:
    """Return the running scheduler or raise 503 if it isn't attached.

    Used by endpoints that *only* make sense when the serve process
    has booted with a scheduler (most admin actions).
    """
    sched = getattr(app.state, "scheduler", None)
    if sched is None:
        raise HTTPException(503, "scheduler not running")
    return sched

"""Minimal MCP server over stdio (JSON-RPC 2.0).

Exposes loop-memory's distilled wiki and search to any MCP-compatible
client (Codex CLI, Claude Code, Hermes, …). Zero third-party deps —
the protocol is small enough to implement directly.

Wire format: one JSON message per line (newline-delimited JSON).
We deliberately use line-delimited instead of the standard LSP-style
Content-Length headers because every MCP client we care about
(Codex CLI, Claude Code, Hermes) accepts newline-delimited JSON.

Tools exposed:

  * ``recall(query, limit=8)``     - full-text + entity search over memories
  * ``list_wiki(limit=20)``        - list distilled wiki pages
  * ``get_wiki(slug)``             - full body of one wiki page
  * ``recent_memories(limit=20)``  - newest memories (for warm-start)
  * ``wiki_summary()``             - one-paragraph "what we know about you"

All tools return a single text block. The clients surface that text
directly to the model.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

from ..storage.sqlite_store import MemoryStore

log = logging.getLogger("loop_memory.mcp")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _store() -> MemoryStore:
    """Lazy import to avoid file-touching on every request."""
    import os
    from pathlib import Path
    db = os.environ.get("LOOP_MEMORY_DB") or str(
        Path.home() / ".loop_memory" / "loop_memory.db"
    )
    return MemoryStore(db)


def _clip(text: str, n: int) -> str:
    t = (text or "").strip()
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def tool_recall(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    query = (arguments.get("query") or "").strip()
    limit = int(arguments.get("limit") or 8)
    limit = max(1, min(limit, 50))
    if not query:
        return [_err("missing 'query' argument")]
    store = _store()
    r = store.recall(query, limit=limit)
    n_mem = len(r["memories"])
    n_wiki = len(r["wiki"])
    n_ent = len(r["entities"])
    if not (n_mem or n_wiki or n_ent):
        return [_text(
            f"No memories or wiki pages match {query!r} yet. "
            "If you just finished a conversation, wait ~60s for the "
            "watcher to ingest it, then try again."
        )]
    lines = [
        f"# Recall: {query!r}",
        f"_({n_mem} memories · {n_wiki} wiki pages · {n_ent} entities)_",
        "",
    ]
    if r["wiki"]:
        lines.append("## Distilled knowledge (wiki)")
        for w in r["wiki"][:max(2, limit // 2)]:
            tag_s = " [" + ", ".join(w.get("tags") or []) + "]" if w.get("tags") else ""
            lines.append(f"- **{w.get('title')}** (`{w.get('slug')}`){tag_s} — imp {w.get('importance', 0):.2f}")
            if w.get("summary"):
                lines.append(f"  > {_clip(w['summary'], 200)}")
            elif w.get("body"):
                lines.append(f"  {_clip(w['body'], 200)}")
        lines.append("")
    if r["memories"]:
        lines.append("## Raw memories")
        for m in r["memories"][:limit]:
            meta = []
            if m.get("kind"):
                meta.append(m["kind"])
            if m.get("tags"):
                meta.append("tags=" + ",".join(m["tags"][:3]))
            if m.get("importance"):
                meta.append(f"imp={m['importance']:.2f}")
            when = ""
            try:
                from datetime import datetime
                when = " · " + datetime.fromtimestamp(float(m["created_at"])).strftime("%Y-%m-%d")
            except Exception:
                pass
            lines.append(f"- [{' · '.join(meta)}{when}]")
            lines.append(f"  {_clip(m['text'], 240)}")
        lines.append("")
    if r["entities"]:
        lines.append("## Entities")
        for e in r["entities"][:limit]:
            lines.append(
                f"- {e['name']} _({e['entity_kind']}, w={e['weight']:.2f}, "
                f"mentions={e['mention_count']})_"
            )
    return [_text("\n".join(lines))]


def tool_list_wiki(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    limit = int(arguments.get("limit") or 20)
    limit = max(1, min(limit, 100))
    store = _store()
    pages = store.list_wiki_pages(limit=limit)
    if not pages:
        return [_text("No distilled wiki pages yet. Run an AI consolidation pass first.")]
    lines = [f"# Distilled wiki ({len(pages)} page{'s' if len(pages)!=1 else ''})", ""]
    for p in pages:
        tags = ", ".join(p.get("tags") or [])
        meta = f"v{p.get('version',1)} · imp={p.get('importance',0):.2f}"
        if tags:
            meta += f" · [{tags}]"
        lines.append(f"- **{p.get('title', p.get('slug','?'))}** (`{p.get('slug')}`) — {meta}")
        if p.get("summary"):
            lines.append(f"  > {_clip(p['summary'], 200)}")
    return [_text("\n".join(lines))]


def tool_get_wiki(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    slug = (arguments.get("slug") or "").strip()
    if not slug:
        return [_err("missing 'slug' argument")]
    store = _store()
    page = store.get_wiki_page_by_slug(slug) or store.get_wiki_page(slug)
    if not page:
        return [_text(f"No wiki page found for slug {slug!r}.")]
    tags = ", ".join(page.get("tags") or [])
    header = (
        f"# {page.get('title', page.get('slug'))}\n"
        f"slug: {page.get('slug')} · version {page.get('version',1)} · "
        f"importance {page.get('importance',0):.2f}"
    )
    if tags:
        header += f"\ntags: {tags}"
    body = (page.get("body") or "").strip() or "(empty)"
    summary = page.get("summary") or ""
    parts = [header]
    if summary:
        parts.append("")
        parts.append(f"## Summary\n{summary}")
    parts.append("")
    parts.append(f"## Body\n{body}")
    return [_text("\n".join(parts))]


def tool_recent_memories(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    limit = int(arguments.get("limit") or 20)
    limit = max(1, min(limit, 100))
    store = _store()
    rows = store.list_memories(limit=limit)
    if not rows:
        return [_text("No memories stored yet.")]
    lines = [f"# Recent memories ({len(rows)})", ""]
    for r in rows:
        try:
            from datetime import datetime
            when = datetime.fromtimestamp(float(r.created_at)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = "?"
        lines.append(
            f"- **{r.kind or 'memory'}** ({when}, imp={r.importance or 0:.2f}): "
            f"{_clip(r.text, 220)}"
        )
    return [_text("\n".join(lines))]


def tool_wiki_summary(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    store = _store()
    pages = store.list_wiki_pages(limit=200, min_importance=0.4)
    if not pages:
        return [_text(
            "No high-importance wiki pages yet. The distillation pipeline "
            "needs to run at least once (open the web UI → AI Consolidate)."
        )]
    # Group by leading tag if possible
    lines = [
        f"# What we know about you (top {len(pages)} distilled pages)",
        "",
        "This is a digest of the user's long-term memory: their preferences, "
        "decisions, ongoing projects, and the facts they have validated. "
        "Treat this as ground truth unless the user contradicts it.",
        "",
    ]
    for p in pages[:12]:
        tags = p.get("tags") or []
        tag_s = f" [{', '.join(tags[:4])}]" if tags else ""
        lines.append(f"## {p.get('title', p.get('slug'))}{tag_s}")
        if p.get("summary"):
            lines.append(_clip(p["summary"], 320))
        else:
            lines.append(_clip(p.get("body", "") or "", 320))
        lines.append("")
    return [_text("\n".join(lines))]


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _text(s: str) -> dict[str, Any]:
    return {"type": "text", "text": s}


def _err(s: str) -> dict[str, Any]:
    return {"type": "text", "text": f"⚠ {s}"}


TOOLS = [
    {
        "name": "recall",
        "description": (
            "Unified search across the user's loop-memory store: returns "
            "ranked wiki pages (curated knowledge), raw memories (with "
            "importance + tags + a short preview), and matching entities "
            "(people, projects, concepts) for any free-text query. Use this "
            "when the user references something specific and you need to "
            "recall context, prior decisions, or earlier conversations. "
            "Handles English + Chinese tokenisation automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query"},
                "limit": {"type": "integer", "description": "Max results (default 8, max 50)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_wiki",
        "description": (
            "List all distilled wiki pages with their slugs, summaries and "
            "importance. Use this to discover what topics the user has "
            "already validated through consolidation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max pages (default 20, max 100)"},
            },
        },
    },
    {
        "name": "get_wiki",
        "description": (
            "Fetch the full body of one distilled wiki page by its slug. "
            "Use this after list_wiki to drill into the topic you need."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Wiki page slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "recent_memories",
        "description": (
            "Return the most recent N memories verbatim. Useful when you "
            "need raw context for a continuation of an earlier session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max memories (default 20, max 100)"},
            },
        },
    },
    {
        "name": "wiki_summary",
        "description": (
            "Return a structured digest of the highest-importance wiki pages. "
            "Use this as a warm-start background block when the user opens "
            "a new conversation, so you immediately know their preferences, "
            "ongoing projects, and validated decisions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


TOOL_DISPATCH = {
    "recall": tool_recall,
    "list_wiki": tool_list_wiki,
    "get_wiki": tool_get_wiki,
    "recent_memories": tool_recent_memories,
    "wiki_summary": tool_wiki_summary,
}


SERVER_INFO = {
    "name": "loop-memory",
    "version": "0.3.0",
}


CAPABILITIES = {
    "tools": {"listChanged": False},
}


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err_resp(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _handle(req: dict[str, Any]) -> dict[str, Any] | None:
    """Return a JSON-RPC response or None for notifications."""
    method = req.get("method")
    params = req.get("params") or {}
    rid = req.get("id")
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
        })
    if method == "ping":
        return _ok(rid, {})
    if method == "notifications/initialized":
        return None  # no response for notifications
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_DISPATCH.get(name)
        if not fn:
            return _err_resp(rid, -32601, f"unknown tool {name!r}")
        try:
            content = fn(args)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            content = [_err(f"tool {name} failed: {type(e).__name__}: {e}")]
        return _ok(rid, {"content": content, "isError": False})
    if method == "resources/list":
        return _ok(rid, {"resources": []})
    if method == "prompts/list":
        return _ok(rid, {"prompts": []})
    # Unknown — be lenient and ack with empty result.
    if rid is None:
        return None
    return _ok(rid, {})


def serve_stdio() -> None:
    """Read newline-delimited JSON-RPC from stdin, write to stdout."""
    log.info("loop-memory MCP server starting")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001
            sys.stdout.write(json.dumps(_err_resp(
                None, -32700, f"parse error: {e}"
            )) + "\n")
            sys.stdout.flush()
            continue
        resp = _handle(req)
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


__all__ = ["serve_stdio", "TOOLS", "TOOL_DISPATCH", "SERVER_INFO"]

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


def _agent_context() -> tuple[str | None, str | None]:
    """Return the (agent_id, user_id) tuple for this MCP session.

    The defaults come from environment variables so a process started
    per-client (``loop-memory mcp`` launched by Codex, Claude Code,
    Hermes, …) can stamp every write with its own identity without
    the LLM having to remember to set it on every call.
    """
    import os
    return (
        os.environ.get("LOOP_MEMORY_AGENT_ID") or None,
        os.environ.get("LOOP_MEMORY_USER_ID") or None,
    )


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





# ---------------------------------------------------------------------------
# Write surface — the universal Agent Memory contract
# ---------------------------------------------------------------------------
# Until now the MCP server was strictly read-only. Any Agent that
# wanted to push a fact into long-term memory had to shell out to
# ``loop-memory write`` or hit ``/api/v1/memories`` over HTTP. These
# three tools close that gap and make the MCP server self-sufficient
# for write→read round-trips from any MCP-aware client.


def tool_remember(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Push a memory into long-term storage.

    Args:
        text: required memory body.
        kind: ``fact`` (default) / ``preference`` / ``decision`` /
              ``reflection`` / ``plan`` / ``episode``.
        importance: 0..1; default 0.5.
        tags: list of strings.
        source: free-form source pointer (e.g. tool name, URL).
        session_id: optional session id this memory belongs to.
        external_id: optional stable id; re-calling with the same
                     ``(agent_id, user_id, external_id)`` updates
                     the row in place. ``agent_id`` / ``user_id``
                     come from ``LOOP_MEMORY_AGENT_ID`` /
                     ``LOOP_MEMORY_USER_ID`` env when not given.
    """
    text = (arguments.get("text") or "").strip()
    if not text:
        return [_err("missing 'text' argument")]
    kind = (arguments.get("kind") or "fact").strip()
    importance = arguments.get("importance", 0.5)
    try:
        importance_f = float(importance)
    except (TypeError, ValueError):
        return [_err(f"importance must be a number, got {importance!r}")]
    importance_f = max(0.0, min(1.0, importance_f))
    tags = arguments.get("tags") or []
    if not isinstance(tags, list):
        return [_err("tags must be a list of strings")]
    ext = arguments.get("external_id")
    if ext is not None:
        ext = str(ext).strip() or None
    agent_id, user_id = _agent_context()
    agent_id = arguments.get("agent_id") or agent_id
    user_id = arguments.get("user_id") or user_id
    session_id = arguments.get("session_id")
    source = arguments.get("source")
    try:
        row = _store().upsert_memory(
            kind=kind,
            text=text,
            importance=importance_f,
            tags=[str(t) for t in tags],
            source=source,
            session_id=session_id,
            agent_id=agent_id,
            user_id=user_id,
            external_id=ext,
        )
    except Exception as e:
        return [_err(f"remember() failed: {e}")]
    out = (
        f"✅ remembered ({row.id})\n"
        f"  text: {_clip(text, 160)}\n"
        f"  kind={row.kind} importance={row.importance:.2f} "
        f"agent_id={agent_id or '-'} external_id={row.external_id or '-'}"
    )
    return [_text(out)]


def tool_forget(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Delete a memory by id or by ``(agent_id, user_id, external_id)``.

    Returns the number of rows removed (0 or 1).
    """
    mid = (arguments.get("id") or "").strip() or None
    ext = arguments.get("external_id")
    if ext is not None:
        ext = str(ext).strip() or None
    if not mid and not ext:
        return [_err("forget() needs 'id' or 'external_id'")]
    agent_id, user_id = _agent_context()
    agent_id = arguments.get("agent_id") or agent_id
    user_id = arguments.get("user_id") or user_id
    store = _store()
    if not mid and ext:
        row = store.find_memory_by_external_id(
            agent_id or "", ext, user_id=user_id,
        )
        if row is None:
            return [_text(f"No memory matches external_id={ext!r} for this agent.")]
        mid = row.id
    n = store.delete_memory(mid)
    return [_text(f"forget() → deleted={n} (id={mid})")]


def tool_feedback(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Record 👍/👎 on a memory by id or by external tuple.

    ``value`` is 'up' / 'down' / 'ignore'. Returns whether the
    signal was recorded.
    """
    value = (arguments.get("value") or "up").strip().lower()
    if value not in ("up", "down", "ignore"):
        return [_err("value must be up|down|ignore")]
    mid = (arguments.get("id") or "").strip() or None
    ext = arguments.get("external_id")
    if ext is not None:
        ext = str(ext).strip() or None
    if not mid and not ext:
        return [_err("feedback() needs 'id' or 'external_id'")]
    agent_id, user_id = _agent_context()
    agent_id = arguments.get("agent_id") or agent_id
    user_id = arguments.get("user_id") or user_id
    store = _store()
    if not mid and ext:
        row = store.find_memory_by_external_id(
            agent_id or "", ext, user_id=user_id,
        )
        if row is None:
            return [_text(f"No memory matches external_id={ext!r} for this agent.")]
        mid = row.id
    try:
        store.record_signal(mid, positive=(value == "up"))
    except Exception as e:
        return [_err(f"feedback() failed: {e}")]
    deleted = 0
    if value == "ignore":
        deleted = store.delete_memory(mid)
    return [_text(f"feedback({value}) → memory_id={mid} deleted={deleted}")]





# ---------------------------------------------------------------------------
# Universal Agent Memory v7 — graph + cognitive tools
# ---------------------------------------------------------------------------


def tool_remember_edge(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Push a high-signal semantic relation between two entities.

    Mirrors ``MemoryClient.remember_edge``. ``src`` and ``dst``
    are entity names; ``kind`` defaults to ``relates_to`` and
    ``weight`` to 0.5. The ``LOOP_MEMORY_AGENT_ID`` env is *not*
    auto-applied to graph edges because they're a public schema,
    not private memory.
    """
    src = (arguments.get("src") or "").strip()
    dst = (arguments.get("dst") or "").strip()
    if not src or not dst or src == dst:
        return [_err("'src' and 'dst' must be distinct non-empty names")]
    kind = (arguments.get("kind") or "relates_to").strip() or "relates_to"
    try:
        weight = float(arguments.get("weight", 0.5))
    except (TypeError, ValueError):
        return [_err(f"weight must be a number, got {arguments.get('weight')!r}")]
    try:
        from ..jobs.graph import upsert_semantic_edge
        upsert_semantic_edge(
            _store(), src, dst, kind=kind,
            weight=max(0.0, min(1.5, weight)),
            evidence_id=arguments.get("evidence_id"),
        )
    except Exception as e:
        return [_err(f"remember_edge failed: {e}")]
    return [_text(
        f"edge({src} -[{kind}, w={weight:.2f}]-> {dst}) ✓"
    )]


def tool_subgraph(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a small subgraph relevant to a free-text query."""
    query = (arguments.get("query") or "").strip()
    if not query:
        return [_err("'query' is required")]
    try:
        from ..jobs.graph import subgraph_for
        sg = subgraph_for(_store(), query)
    except Exception as e:
        return [_err(f"subgraph failed: {e}")]
    nodes = ", ".join(n.get("name", "") for n in sg.nodes)
    edges = ", ".join(f"{e['src']}→{e['dst']}" for e in sg.edges[:10])
    return [_text(
        f"# Subgraph for {query!r}\n"
        f"nodes ({len(sg.nodes)}): {nodes or '(none)'}\n"
        f"edges ({len(sg.edges)}): {edges or '(none)'}\n"
        f"backing memories: {len(sg.memory_ids)}"
    )]


def tool_cognitive_sleep(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the cognitive sleep sweep. ``apply=true`` actually
    deletes the suggested memories; default is dry-run.

    Returns a count summary plus a short list of the top actions
    so the LLM can decide whether to call apply=true.
    """
    apply = bool(arguments.get("apply", False))
    try:
        from ..jobs.cognitive import cognitive_sleep
        rpt = cognitive_sleep(
            _store(), apply=apply,
            stale_days=int(arguments.get("stale_days", 90)),
            min_score=float(arguments.get("min_score", 0.2)),
            min_importance=float(arguments.get("min_importance", 0.3)),
            low_value=float(arguments.get("low_value", 0.3)),
        )
    except Exception as e:
        return [_err(f"cognitive_sleep failed: {e}")]
    top = rpt.actions[:5]
    out = [
        f"# Cognitive sleep ({'applied' if apply else 'dry-run'})",
        f"counts: {rpt.counts}",
        f"total: {len(rpt.actions)} actions in {rpt.elapsed_ms:.1f}ms",
        "",
    ]
    for a in top:
        snippet = (a.target_text or "").replace("\n", " ")[:80]
        out.append(f"- [{a.kind}] {snippet}  ({a.reason})")
    return [_text("\n".join(out))]


def tool_audit(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the cognitive audit trail. Filters: ``kind``, ``action``,
    ``limit`` (default 50)."""
    try:
        rows = _store().list_audit(
            kind=arguments.get("kind") or None,
            action=arguments.get("action") or None,
            limit=int(arguments.get("limit", 50)),
        )
    except Exception as e:
        return [_err(f"audit failed: {e}")]
    if not rows:
        return [_text("No audit rows yet — run `cognitive_sleep` first.")]
    lines = [f"# Audit ({len(rows)} rows)"]
    for r in rows[:20]:
        snippet = (r.get("target_text") or "").replace("\n", " ")[:80]
        lines.append(
            f"- [{r.get('action','?')}/{r.get('kind','?')}] {snippet}"
        )
    return [_text("\n".join(lines))]


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
    {
        "name": "remember",
        "description": (
            "Push a single memory into the user's long-term store. Use this "
            "when the user shares a stable preference, decision, fact, or "
            "reflection that should survive across sessions. ``external_id`` "
            "makes the call idempotent: re-pushing the same external_id "
            "updates the row in place. ``importance`` is 0..1 (default 0.5). "
            "If ``LOOP_MEMORY_AGENT_ID`` is set in the environment, every "
            "call is auto-stamped with that agent id so different Agents "
            "don't overwrite each other."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Memory body (required)"},
                "kind": {"type": "string", "description": "fact|preference|decision|reflection|plan|episode (default fact)"},
                "importance": {"type": "number", "description": "0..1 (default 0.5)"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
                "source": {"type": "string", "description": "Free-form source pointer"},
                "session_id": {"type": "string", "description": "Session this memory belongs to"},
                "external_id": {"type": "string", "description": "Stable id for idempotent re-pushes"},
                "agent_id": {"type": "string", "description": "Override LOOP_MEMORY_AGENT_ID for this call"},
                "user_id": {"type": "string", "description": "Override LOOP_MEMORY_USER_ID for this call"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Delete a memory by id or by its ``(agent_id, user_id, "
            "external_id)`` triple. Returns the number of rows removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Internal memory id"},
                "external_id": {"type": "string", "description": "Stable external id"},
                "agent_id": {"type": "string", "description": "Agent namespace (overrides env)"},
                "user_id": {"type": "string", "description": "User namespace (overrides env)"},
            },
        },
    },
    {
        "name": "feedback",
        "description": (
            "Record 👍/👎 on a memory. ``value`` is 'up' (boost), "
            "'down' (demote), or 'ignore' (demote + soft-delete). "
            "Address by id or by ``(agent_id, user_id, external_id)``."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "up|down|ignore (default up)"},
                "id": {"type": "string", "description": "Internal memory id"},
                "external_id": {"type": "string", "description": "Stable external id"},
                "agent_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["value"],
        },
    },
    {
        "name": "remember_edge",
        "description": (
            "Push a high-signal semantic relation between two entities "
            "(Mem0's graph-memory differentiator). E.g. "
            "``{src: User, dst: Hangzhou, kind: lives_in, weight: 0.9}``."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source entity name (required)"},
                "dst": {"type": "string", "description": "Destination entity name (required)"},
                "kind": {"type": "string", "description": "lives_in|works_on|uses|prefers|decided|...|relates_to (default)"},
                "weight": {"type": "number", "description": "0..1.5 (default 0.5)"},
                "evidence_id": {"type": "string", "description": "Optional memory id backing this edge"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "subgraph",
        "description": (
            "Return a small subgraph (entities + edges + backing "
            "memory ids) relevant to a free-text query. Use to ground "
            "a prompt on the user\'s knowledge graph before answering."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query (required)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cognitive_sleep",
        "description": (
            "Run the cognitive sweep: identify stale, low-value, "
            "near-duplicate, and contradicted memories. ``apply=true`` "
            "actually deletes the suggestions; the default dry-run "
            "just lists them so the model can decide. Every action "
            "is recorded in the audit trail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {"type": "boolean", "description": "Actually delete (default false / dry-run)"},
                "stale_days": {"type": "integer", "description": "Stale cutoff in days (default 90)"},
                "min_score": {"type": "number", "description": "Score below which a memory is stale (default 0.2)"},
                "min_importance": {"type": "number", "description": "Importance below which a memory is stale (default 0.3)"},
                "low_value": {"type": "number", "description": "Score+0.5*importance below which a memory is low-value (default 0.3)"},
            },
        },
    },
    {
        "name": "audit",
        "description": (
            "Read the cognitive audit trail. ``kind`` and ``action`` "
            "filter; ``limit`` caps the rows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "stale|low_value|merge|contradict|forget|revert"},
                "action": {"type": "string", "description": "suggest|applied|reverted"},
                "limit": {"type": "integer", "description": "Max rows (default 50)"},
            },
        },
    },
]


TOOL_DISPATCH = {
    "recall": tool_recall,
    "list_wiki": tool_list_wiki,
    "get_wiki": tool_get_wiki,
    "recent_memories": tool_recent_memories,
    "wiki_summary": tool_wiki_summary,
    "remember": tool_remember,
    "forget": tool_forget,
    "feedback": tool_feedback,
    "remember_edge": tool_remember_edge,
    "subgraph": tool_subgraph,
    "cognitive_sleep": tool_cognitive_sleep,
    "audit": tool_audit,
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

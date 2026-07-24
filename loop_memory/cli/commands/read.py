"""Read-only commands: chat, stats, recall, ask, export, inject."""

from __future__ import annotations

import datetime as _dt
import io
import json
import sys
from pathlib import Path

from .._common import DEFAULT_DB, default_db_path, die, make_engine
from ...privacy import redact_text, strip_private_spans


def run_chat(_args) -> int:
    engine = make_engine()
    print(f"[loop-memory] ready. {engine}")
    print("Type your message, ':stats' for diagnostics, ':recall <q>' to search, ':quit' to exit.\n")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {":quit", ":q", ":exit"}:
            return 0
        if line == ":stats":
            print(json.dumps(
                {k: len(getattr(engine.short, "_items", [])) if k == "short" else 0 for k in ["short"]},
                indent=2,
            ))
            continue
        if line.startswith(":recall "):
            q = line[len(":recall "):].strip()
            for i, m in enumerate(engine.recall(q), 1):
                print(f"  {i}. ({m.kind}) {m.text}")
            continue
        result = engine.turn(line)
        print(f"bot> {result.reply}")
        if result.diagnostics.get("stored"):
            print(f"     [stored {result.diagnostics['stored']} new memories]")
    return 0


def run_stats(_args) -> int:
    from ...storage.sqlite_store import MemoryStore
    print(json.dumps(MemoryStore(DEFAULT_DB).stats(), indent=2))
    return 0


def run_recall(args) -> int:
    from ...storage.sqlite_store import MemoryStore
    if not args:
        return die("usage: loop-memory recall <query>")
    store = MemoryStore(default_db_path())
    query = " ".join(args)
    r = store.recall(query, limit=10)
    has = False
    if r["wiki"]:
        has = True
        print(f"## Distilled knowledge ({len(r['wiki'])} match{'es' if len(r['wiki'])!=1 else ''})")
        for w in r["wiki"]:
            tag_s = "  [" + ", ".join(w.get("tags") or []) + "]" if w.get("tags") else ""
            print(f"- **{w['title']}** (`{w['slug']}`) — imp {w['importance']:.2f}{tag_s}")
            if w.get("summary"):
                print(f"  > {w['summary'][:240]}")
        print()
    if r["memories"]:
        has = True
        print(f"## Raw memories ({len(r['memories'])} match{'es' if len(r['memories'])!=1 else ''})")
        for m in r["memories"]:
            tag_s = "  [" + ", ".join(m.get("tags") or []) + "]" if m.get("tags") else ""
            print(f"- [{m['kind']}] (imp={m['importance']:.2f}){tag_s}")
            print(f"  {m['text'][:240]}")
        print()
    if r["entities"]:
        has = True
        print(f"## Entities ({len(r['entities'])})")
        for e in r["entities"]:
            print(f"- {e['name']} _({e['entity_kind']}, w={e['weight']:.2f})_")
        print()
    if not has:
        print(f"_(nothing matched {query!r})_")
    return 0


def run_export(args) -> int:
    """Export distilled wiki pages as one markdown file.

    Usage:  loop-memory export [--out PATH] [--q QUERY]
    """
    from ...storage.sqlite_store import MemoryStore
    out_path = None
    query = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out" and i + 1 < len(args):
            out_path = args[i + 1]; i += 2
        elif a == "--q" and i + 1 < len(args):
            query = args[i + 1]; i += 2
        else:
            i += 1
    if out_path is None:
        stamp = _dt.date.today().isoformat()
        out_path = str(Path.home() / f"loop-memory-export-{stamp}.md")
    out_path = str(Path(out_path).expanduser())
    store = MemoryStore(default_db_path())
    pages = store.list_wiki_pages(limit=500, query=query)
    lines = ["# Loop Memory — Distilled Knowledge", ""]
    lines.append(f"_Exported {len(pages)} wiki pages._")
    lines.append("")
    for p in pages:
        title = (p.get("title") or "untitled").strip()
        # Defensive redaction: bodies should already be redacted at
        # write time, but a page distilled before the redaction hook
        # shipped could still contain a leaked secret. Run the page
        # through the same pipeline one more time so the exported
        # markdown is safe to paste into any public doc.
        body = redact_text(strip_private_spans((p.get("body") or "").strip()))
        summary = redact_text(strip_private_spans((p.get("summary") or "").strip()))
        lines.append(f"## {title}")
        lines.append("")
        if summary and summary != title:
            lines.append(f"> {summary}")
            lines.append("")
        lines.append(body)
        lines.append("")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ wrote {len(pages)} pages → {out_path}")
    print("   paste this file into any LLM client as context to apply your distilled knowledge.")
    return 0


def run_digest(args) -> int:
    """Build a tight, byte-budgeted markdown digest of the long-term
    memory store. Designed to be injected as ``AGENTS.md`` at session
    start so the assistant carries a compact summary of the user's
    distilled knowledge — without paying the cost of every historical
    turn being replayed into context.

    The output is plain markdown, ordered by importance, capped at
    ``--max-chars`` (default 12000 ≈ 3000 tokens). This is small
    enough to live permanently in the assistant's system prompt,
    dramatically reducing how much session history needs to be
    carried per turn.

    Usage:  loop-memory digest [--out PATH] [--max-chars 12000] [--q QUERY]
    """
    from ...storage.sqlite_store import MemoryStore
    out_path = None
    max_chars = 12000
    query = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out" and i + 1 < len(args):
            out_path = args[i + 1]; i += 2
        elif a == "--max-chars" and i + 1 < len(args):
            max_chars = max(500, int(args[i + 1])); i += 2
        elif a == "--q" and i + 1 < len(args):
            query = args[i + 1]; i += 2
        else:
            i += 1
    if out_path is None:
        out_path = str(Path.home() / ".loop_memory" / "AGENTS.md")
    out_path = str(Path(out_path).expanduser())
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(default_db_path())
    pages = store.list_wiki_pages(limit=200, query=query)
    # Order by importance desc, then updated_at desc — surface the
    # most useful and freshest knowledge first.
    pages.sort(key=lambda p: (float(p.get("importance") or 0), float(p.get("updated_at") or 0)), reverse=True)

    # Score the page's density so we keep self-contained pages and
    # drop thin ones when the budget runs out.
    def density(p):
        body = (p.get("body") or "").strip()
        facts = p.get("key_facts") or []
        return len(body) + 80 * len(facts)

    kept: list = []
    total = 0
    overhead = 280  # header + footer
    budget = max_chars - overhead
    for p in pages:
        body = redact_text(strip_private_spans((p.get("body") or "").strip()))
        summary = redact_text(strip_private_spans((p.get("summary") or "").strip()))
        title = (p.get("title") or "untitled").strip()
        facts = p.get("key_facts") or []
        # Build the candidate block
        block_lines = [f"## {title}", ""]
        if summary and summary != title:
            block_lines += [f"> {summary}", ""]
        if facts:
            block_lines.append("**Key facts:**")
            for f in facts[:8]:
                block_lines.append(f"- {f}")
            block_lines.append("")
        if body:
            # Cap each page body at 800 chars to keep digest compact.
            if len(body) > 800:
                body = body[:800].rstrip() + "…"
            block_lines += [body, ""]
        block = "\n".join(block_lines)
        if total + len(block) > budget:
            break
        kept.append((p, block))
        total += len(block)

    lines = ["# Distilled knowledge — auto-injected memory digest", ""]
    lines.append(f"_Compiled from {len(kept)} of {len(pages)} wiki pages, capped at {max_chars:,} chars (~{max_chars//4:,} tokens)._")
    lines.append(f"_Generated { _dt.datetime.now().isoformat(timespec='seconds') }._")
    lines.append("")
    lines.append("Read me at the start of every task. Update by running `loop-memory digest` again, "
                 "or via the web UI's Settings → 'Recompile digest'.")
    lines.append("")
    for _, block in kept:
        lines.append(block)
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ digest → {out_path}  ({total:,} chars, {len(kept)} pages)")
    return 0


def run_ask(args) -> int:
    """Print a copy-pasteable context block for a query — no server needed."""
    from ...storage.sqlite_store import MemoryStore
    if not args:
        return die("usage: loop-memory ask <query>")
    q = " ".join(args).strip()
    store = MemoryStore(default_db_path())
    r = store.recall(q, limit=8)
    n_wiki = len(r["wiki"])
    n_mem = len(r["memories"])
    print(f"# Distilled knowledge — {q}\n")
    print(f"_matched {n_wiki} wiki pages + {n_mem} memories (unified recall)_\n")
    if not (n_wiki or n_mem):
        print(f"_(no memories or wiki pages matched {q!r})_")
        print()
        print("If this is a brand-new question, the distillation pipeline "
              "needs to run at least once: open the web UI → AI Consolidate, "
              "or run `loop-memory consolidate-now`.")
        return 0
    for w in r["wiki"][:5]:
        print(f"## {w.get('title')}")
        print()
        if w.get("summary"):
            print(f"> {w['summary']}")
            print()
        body = (w.get("body") or "").strip()
        if len(body) > 800:
            body = body[:800] + "…"
        print(body)
        print()
    for m in r["memories"][:4]:
        print(f"## Memory ({m['kind']})")
        print()
        text = (m.get("text") or "").strip()
        if len(text) > 600:
            text = text[:600] + "…"
        print(text)
        print()
    return 0


def run_inject(args) -> int:
    """Print a context block of distilled wiki + relevant memories.

    Designed for SessionStart hooks so every new conversation starts
    with the user's curated knowledge already in context.

    With no arguments, surfaces the user's highest-importance wiki
    pages + preference facts. With a query argument (passed by the
    hook from the user's first message), it returns the most relevant
    memories for that query.
    """
    from ...storage.sqlite_store import MemoryStore
    store = MemoryStore(default_db_path())
    query = " ".join(args).strip()
    out = io.StringIO()
    out.write("# Long-term memory context\n")
    if query:
        r = store.recall(query, limit=10)
        wiki = r["wiki"][:6]
        mem = r["memories"][:6]
        out.write(
            f"_(generated by loop-memory for query {query!r}: "
            f"{len(wiki)} wiki pages + {len(mem)} memories)_\n\n"
        )
        if wiki:
            out.write("## Distilled knowledge (wiki, ranked for this query)\n\n")
            for w in wiki:
                title = w.get("title", w.get("slug", "?"))
                slug = w.get("slug", "")
                tags = w.get("tags") or []
                tag_s = f" — [{', '.join(tags[:4])}]" if tags else ""
                text = (w.get("summary") or w.get("body") or "").strip()
                if len(text) > 380:
                    text = text[:379] + "…"
                out.write(f"- **{title}** (`{slug}`){tag_s}\n")
                if text:
                    out.write(f"  {text}\n")
            out.write("\n")
        if mem:
            out.write("## Raw memories (ranked)\n\n")
            for m in mem:
                t = (m.get("text") or "").strip()
                if len(t) > 280:
                    t = t[:279] + "…"
                tag_s = ""
                if m.get("tags"):
                    tag_s = f" [{', '.join(m['tags'][:3])}]"
                try:
                    from datetime import datetime
                    when = datetime.fromtimestamp(float(m["created_at"])).strftime("%Y-%m-%d")
                except Exception:
                    when = "?"
                out.write(
                    f"- [{m['kind']} · {when} · imp={m['importance']:.2f}]{tag_s} {t}\n"
                )
    else:
        # No query: surface the user's top preferences / highest-importance
        # wiki pages so a brand-new conversation starts with the user
        # already in context.
        with store._conn() as c:
            pref = c.execute(
                "SELECT text, importance FROM memories "
                "WHERE kind='fact' AND importance >= 0.6 "
                "ORDER BY importance DESC, created_at DESC LIMIT 3"
            ).fetchall()
            pages = store.list_wiki_pages(limit=10, min_importance=0.4)
        out.write(
            f"_(generated by loop-memory: {len(pages)} wiki pages, "
            f"{len(pref)} preference facts)_\n\n"
        )
        if pref:
            out.write("## User preferences (use these to guide style)\n\n")
            for r in pref:
                t = (r["text"] or "").strip()
                if len(t) > 360:
                    t = t[:359] + "…"
                out.write(f"- {t}\n")
            out.write("\n")
        if pages:
            out.write("## Distilled knowledge (wiki)\n\n")
            for pg in pages[:8]:
                title = pg.get("title", pg.get("slug", "?"))
                slug = pg.get("slug", "")
                tags = pg.get("tags") or []
                tag_s = f" — [{', '.join(tags[:4])}]" if tags else ""
                summary = (pg.get("summary") or "").strip()
                body = (pg.get("body") or "").strip()
                text = summary or body
                if len(text) > 320:
                    text = text[:319] + "…"
                out.write(f"- **{title}** (`{slug}`){tag_s}\n")
                if text:
                    out.write(f"  {text}\n")
    sys.stdout.write(out.getvalue())
    return 0

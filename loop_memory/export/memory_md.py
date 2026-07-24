"""The MEMORY.md exporter/importer.

The format is intentionally small and human-readable:

* ``MEMORY.md`` — a top-level summary. The first line is a YAML
  front-matter block with metadata (schema version, export time,
  agent_id, user_id). The body groups wiki pages by tag
  (``# 偏好``, ``# 决策``, ``# 项目背景``, etc.) so the user can
  scan it like any other Markdown file.

* ``pages/<slug>.md`` — one file per wiki page. The front-matter
  carries ``title``, ``importance``, ``tags``, ``scope``; the body
  is the page's body. Key facts become a bullet list at the end.

* ``memories.jsonl`` — every memory as one JSON object per line.
  Stable key order, sorted by created_at for deterministic diffs.

* ``graph.json`` — ``{"entities": [...], "relations": [...]}``.

* ``sessions.json`` — ``{"sessions": [...]}``.

* ``meta.json`` — bundle metadata for the importer.

``import_bundle`` is the inverse: it walks the directory, upserts
each wiki page (by slug), each memory (by external triple), each
entity and relation. Wiki pages are versioned via
``snapshot_wiki_version`` so a `git revert` is a single SQL UPDATE.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..storage.sqlite_store import MemoryStore

log = logging.getLogger(__name__)


SCHEMA_VERSION = 1
BUNDLE_NAME = "loop-memory-bundle"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExportReport:
    out_dir: str
    memory_md_path: str
    pages: list[str] = field(default_factory=list)
    memories: int = 0
    graph_entities: int = 0
    graph_relations: int = 0
    sessions: int = 0
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "out_dir": self.out_dir,
            "memory_md_path": self.memory_md_path,
            "pages": self.pages,
            "memories": self.memories,
            "graph_entities": self.graph_entities,
            "graph_relations": self.graph_relations,
            "sessions": self.sessions,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_bundle(
    store: MemoryStore,
    out_dir: str | Path,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    scope: str = "global",
    min_importance: float = 0.0,
) -> ExportReport:
    """Write a full white-box bundle to ``out_dir``.

    Parameters mirror the common filters so an Agent can export
    only its own namespace:

    * ``agent_id`` / ``user_id``: optional, only export memories
      with these tags (NULL means "any").
    * ``scope``: only export wiki pages with this scope.
    * ``min_importance``: drop pages + memories below this.
    """
    t0 = time.time()
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    pages_dir = out / "pages"
    pages_dir.mkdir(exist_ok=True)

    pages = list(store.list_wiki_pages(limit=2000, scope=scope))
    if min_importance > 0:
        pages = [p for p in pages if float(p.get("importance") or 0) >= min_importance]

    # Write per-page files + the master MEMORY.md
    page_paths: list[str] = []
    for p in pages:
        path = _write_page_file(pages_dir, p)
        page_paths.append(path)
    memory_md = out / "MEMORY.md"
    write_memory_md(pages, memory_md, agent_id=agent_id, user_id=user_id)

    # memories.jsonl — one per row
    mem_path = out / "memories.jsonl"
    n_mem = 0
    with mem_path.open("w", encoding="utf-8") as f:
        # Walk the rows in created_at DESC so the diff is readable
        rows = store.list_memories(
            limit=100_000, agent_id=agent_id, user_id=user_id,
            min_score=None,
        )
        rows = [r for r in rows if float(r.importance or 0) >= min_importance]
        for r in rows:
            d = _memory_to_dict(r)
            f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")
            n_mem += 1

    # graph.json
    ents_rows = list(_iter_entities(store))
    rels_rows = list(_iter_relations(store))
    (out / "graph.json").write_text(
        json.dumps(
            {"entities": ents_rows, "relations": rels_rows},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # sessions.json
    sessions = store.list_sessions(limit=10000)
    (out / "sessions.json").write_text(
        json.dumps(
            {"sessions": [s.__dict__ if hasattr(s, "__dict__") else dict(s) for s in sessions]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # meta.json
    meta = {
        "bundle": BUNDLE_NAME,
        "schema_version": SCHEMA_VERSION,
        "exported_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "agent_id": agent_id,
        "user_id": user_id,
        "scope": scope,
        "min_importance": min_importance,
        "page_count": len(pages),
        "memory_count": n_mem,
        "graph_entities": len(ents_rows),
        "graph_relations": len(rels_rows),
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # INDEX.md
    index_lines = [
        f"# {BUNDLE_NAME} — file index",
        "",
        "_Auto-generated. Re-run `loop-memory export` to refresh._",
        "",
        f"- [MEMORY.md](./MEMORY.md) — top-level summary ({len(pages)} pages)",
        f"- [memories.jsonl](./memories.jsonl) — {n_mem} raw memories",
        f"- [graph.json](./graph.json) — {len(ents_rows)} entities, {len(rels_rows)} relations",
        f"- [sessions.json](./sessions.json) — {len(sessions)} sessions",
        "- [meta.json](./meta.json) — bundle metadata",
        "- [pages/](./pages/) — one Markdown file per wiki page",
    ]
    (out / "INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    elapsed = (time.time() - t0) * 1000
    return ExportReport(
        out_dir=str(out),
        memory_md_path=str(memory_md),
        pages=page_paths,
        memories=n_mem,
        graph_entities=len(ents_rows),
        graph_relations=len(rels_rows),
        sessions=len(sessions),
        elapsed_ms=round(elapsed, 1),
    )


def write_memory_md(
    pages: Iterable[dict[str, Any]],
    out_path: str | Path,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Write the top-level MEMORY.md (also used by the simpler
    ``loop-memory export`` command).

    Groups pages by their first tag, falling back to "其它". The
    file starts with a YAML front-matter block so downstream
    tooling can parse it without re-implementing our heuristics.
    """
    out = Path(out_path)
    pages = list(pages)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    fm = [
        "---",
        f"schema_version: {SCHEMA_VERSION}",
        f"generated_at: {now}",
        f"agent_id: {agent_id or ''}",
        f"user_id: {user_id or ''}",
        f"page_count: {len(pages)}",
        "---",
        "",
        "# 长期记忆",
        "",
        "_Auto-generated by `loop-memory export`. Edit `pages/*.md` "
        "and re-import to keep this file in sync. Use `git` to track "
        "history — every change shows up as a readable diff._",
        "",
    ]
    # Group by tag (first tag wins; "其它" fallback)
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for p in pages:
        tags = p.get("tags") or []
        cat = (tags[0] if tags else "其它")
        by_tag.setdefault(str(cat), []).append(p)
    for cat in sorted(by_tag.keys()):
        fm.append(f"## {cat}")
        fm.append("")
        for p in by_tag[cat]:
            title = p.get("title") or p.get("slug") or "?"
            slug = p.get("slug") or ""
            importance = float(p.get("importance") or 0)
            fm.append(f"### {title} (`{slug}`, importance={importance:.2f})")
            summary = p.get("summary") or ""
            if summary:
                fm.append("")
                fm.append(f"> {summary}")
            kf = p.get("key_facts") or []
            if kf:
                fm.append("")
                fm.append("**Key facts:**")
                for fact in kf:
                    fm.append(f"- {fact}")
            fm.append("")
    out.write_text("\n".join(fm) + "\n", encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    pages_upserted: int = 0
    memories_upserted: int = 0
    entities_upserted: int = 0
    relations_upserted: int = 0
    sessions_upserted: int = 0
    elapsed_ms: float = 0.0
    bundle_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def import_bundle(
    store: MemoryStore,
    in_dir: str | Path,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Re-hydrate a bundle into the live store.

    * Wiki pages are upserted by slug; the new row is versioned
      via ``snapshot_wiki_version`` so a `git revert` is one
      ``UPDATE`` away.
    * Memories are upserted by ``(agent_id, user_id, external_id)``.
      If the bundle row has no ``external_id``, we mint a stable
      one from a SHA-1 of the text so re-imports stay idempotent.
    * Entities and relations are upserted; relations re-point to
      the freshly-created entity ids.

    ``dry_run=True`` walks the bundle and returns counts without
    touching the store.
    """
    t0 = time.time()
    in_path = Path(in_dir).expanduser().resolve()
    if not in_path.is_dir():
        raise FileNotFoundError(f"bundle not found: {in_path}")
    # meta.json is optional; if present, sanity-check the version.
    meta_path = in_path / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if int(meta.get("schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
                log.warning("bundle schema_version > current; some fields may be ignored")
        except Exception as e:
            log.warning("could not parse meta.json: %s", e)
    report = ImportReport(bundle_path=str(in_path))

    pages = _read_pages(in_path)
    for p in pages:
        if dry_run:
            report.pages_upserted += 1
            continue
        slug = p["slug"]
        store.upsert_wiki_page(
            slug=slug,
            title=p.get("title") or slug,
            body=p.get("body") or "",
            summary=p.get("summary") or "",
            tags=p.get("tags") or [],
            importance=float(p.get("importance") or 0.5),
            scope=p.get("scope") or "global",
            key_facts=p.get("key_facts") or [],
        )
        report.pages_upserted += 1

    mem_path = in_path / "memories.jsonl"
    if mem_path.exists():
        for line in mem_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if dry_run:
                report.memories_upserted += 1
                continue
            ext = d.get("external_id")
            if not ext:
                ext = "sha1:" + hashlib.sha1(d.get("text", "").encode("utf-8")).hexdigest()[:16]
            store.upsert_memory(
                id=d.get("id"),
                kind=d.get("kind") or "fact",
                text=d.get("text") or "",
                importance=float(d.get("importance") or 0.5),
                source=d.get("source"),
                session_id=d.get("session_id"),
                tags=d.get("tags") or [],
                agent_id=d.get("agent_id") or agent_id,
                user_id=d.get("user_id") or user_id,
                external_id=ext,
                created_at=d.get("created_at"),
            )
            report.memories_upserted += 1

    graph_path = in_path / "graph.json"
    if graph_path.exists():
        g = json.loads(graph_path.read_text(encoding="utf-8"))
        for e in g.get("entities", []):
            if dry_run:
                report.entities_upserted += 1
                continue
            store.upsert_entity(
                e.get("name") or "", e.get("kind") or "concept",
                bump_weight=float(e.get("weight") or 0),
            )
            report.entities_upserted += 1
        for r in g.get("relations", []):
            if dry_run:
                report.relations_upserted += 1
                continue
            store.upsert_relation(
                r.get("src") or "", r.get("dst") or "",
                kind=r.get("kind") or "co_occurs_with",
                weight=float(r.get("weight") or 0.5),
                evidence_id=r.get("evidence_id"),
            )
            report.relations_upserted += 1

    sessions_path = in_path / "sessions.json"
    if sessions_path.exists():
        s_data = json.loads(sessions_path.read_text(encoding="utf-8"))
        for s in s_data.get("sessions", []):
            if dry_run:
                report.sessions_upserted += 1
                continue
            store.upsert_session(
                source=s.get("source") or "imported",
                external_id=s.get("external_id"),
                title=s.get("title"),
                started_at=s.get("started_at"),
                ended_at=s.get("ended_at"),
                message_count=int(s.get("message_count") or 0),
                metadata=s.get("metadata") or {},
            )
            report.sessions_upserted += 1

    report.elapsed_ms = round((time.time() - t0) * 1000, 1)
    return report


# ---------------------------------------------------------------------------
# Fork
# ---------------------------------------------------------------------------


def fork_snapshot(
    store: MemoryStore,
    *,
    branch_tag: str | None = None,
) -> dict[str, Any]:
    """Snapshot every wiki page into ``wiki_versions`` with the
    given ``branch_tag`` so the user can `git`-tag + restore later.

    Returns ``{"tag": ..., "snapshotted": N, "elapsed_ms": ...}``.
    """
    if not branch_tag:
        branch_tag = "fork-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    t0 = time.time()
    pages = store.list_wiki_pages(limit=10000)
    n = 0
    for p in pages:
        if store.snapshot_wiki_version(p["id"], branch_tag=branch_tag):
            n += 1
    return {
        "tag": branch_tag,
        "snapshotted": n,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_page_file(pages_dir: Path, p: dict[str, Any]) -> str:
    """Write a single wiki page as ``pages/<slug>.md``.

    Front-matter mirrors MEMORY.md so any Markdown renderer that
    understands YAML gets structured data for free.
    """
    slug = (p.get("slug") or uuid.uuid4().hex).strip()
    fm = [
        "---",
        f"slug: {slug}",
        f"title: {p.get('title') or ''}",
        f"importance: {float(p.get('importance') or 0):.3f}",
        f"scope: {p.get('scope') or 'global'}",
        "tags:",
    ]
    for t in p.get("tags") or []:
        fm.append(f"  - {t}")
    fm.append("---")
    fm.append("")
    fm.append(f"# {p.get('title') or slug}")
    fm.append("")
    if p.get("summary"):
        fm.append(f"> {p['summary']}")
        fm.append("")
    kf = p.get("key_facts") or []
    if kf:
        fm.append("## Key facts")
        fm.append("")
        for fact in kf:
            fm.append(f"- {fact}")
        fm.append("")
    fm.append("## Body")
    fm.append("")
    fm.append((p.get("body") or "").rstrip())
    fm.append("")
    path = pages_dir / f"{slug}.md"
    path.write_text("\n".join(fm), encoding="utf-8")
    return str(path)


def _read_pages(in_path: Path) -> list[dict[str, Any]]:
    """Read every ``pages/<slug>.md`` and parse the front-matter.

    Skips pages whose front-matter is corrupt and logs a warning
    so a typo doesn't kill the whole import.
    """
    pages_dir = in_path / "pages"
    if not pages_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(pages_dir.glob("*.md")):
        try:
            out.append(_parse_page_file(f))
        except Exception as e:
            log.warning("could not parse %s: %s", f, e)
    return out


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


def _parse_page_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    m = _FM_RE.match(raw)
    if not m:
        raise ValueError(f"missing front-matter: {path}")
    fm_text, body = m.group(1), m.group(2)
    fm: dict[str, Any] = {}
    list_key: str | None = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - "):
            if list_key:
                fm.setdefault(list_key, []).append(line[4:].strip())
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if not v:
                list_key = k
                fm.setdefault(k, [])
            else:
                list_key = None
                try:
                    fm[k] = float(v) if "." in v else v
                except ValueError:
                    fm[k] = v
    # Extract Key facts block
    kf: list[str] = []
    body_lines = body.splitlines()
    i = 0
    while i < len(body_lines):
        if body_lines[i].strip() == "## Key facts":
            j = i + 1
            while j < len(body_lines) and body_lines[j].startswith("- "):
                kf.append(body_lines[j][2:].strip())
                j += 1
            break
        i += 1
    # Extract body
    body_idx = 0
    for idx, line in enumerate(body_lines):
        if line.strip() == "## Body":
            body_idx = idx + 1
            break
    body_text = "\n".join(body_lines[body_idx:]).strip()
    return {
        "slug": fm.get("slug") or path.stem,
        "title": fm.get("title") or path.stem,
        "importance": float(fm.get("importance") or 0.5),
        "scope": fm.get("scope") or "global",
        "tags": list(fm.get("tags") or []),
        "key_facts": kf,
        "body": body_text,
        "summary": "",  # not currently in the per-page file
    }


def _memory_to_dict(r) -> dict[str, Any]:
    """Convert a StoredMemory dataclass to a JSON-safe dict."""
    return {
        "id": r.id,
        "kind": r.kind,
        "text": r.text,
        "importance": round(float(r.importance or 0), 3),
        "source": r.source,
        "session_id": r.session_id,
        "tags": list(r.tags or []),
        "created_at": float(r.created_at or 0),
        "agent_id": getattr(r, "agent_id", None),
        "user_id": getattr(r, "user_id", None),
        "external_id": getattr(r, "external_id", None),
    }


def _iter_entities(store: MemoryStore) -> Iterable[dict[str, Any]]:
    with store._conn() as c:  # type: ignore[attr-defined]
        rows = c.execute("SELECT * FROM entities ORDER BY name").fetchall()
    for r in rows:
        yield {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "weight": float(r["weight"] or 0),
            "mention_count": int(r["mention_count"] or 0),
        }


def _iter_relations(store: MemoryStore) -> Iterable[dict[str, Any]]:
    with store._conn() as c:  # type: ignore[attr-defined]
        rows = c.execute("SELECT * FROM relations ORDER BY id").fetchall()
    for r in rows:
        try:
            evid = json.loads(r["evidence_ids"]) if r["evidence_ids"] else []
        except Exception:
            evid = []
        yield {
            "id": r["id"],
            "src": r["src"],
            "dst": r["dst"],
            "kind": r["kind"],
            "weight": float(r["weight"] or 0),
            "evidence_ids": evid,
        }


__all__ = [
    "ExportReport",
    "ImportReport",
    "SCHEMA_VERSION",
    "export_bundle",
    "import_bundle",
    "fork_snapshot",
    "write_memory_md",
]

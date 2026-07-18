"""Build the knowledge graph from the memories table.

``KnowledgeGraph.rebuild()`` walks every memory row, runs the
zero-dep entity extractor on its text, and:

1. Upserts each entity row in ``entities`` (name, kind, weight, count).
2. Computes pairwise co-occurrence within a sliding window per memory
   and stores them as ``co_occurs_with`` relations in ``relations``.
3. Records which memory id each relation is evidenced by so the UI
   can highlight the underlying chunks.

Plug in an LLM extractor for higher quality by replacing the ``extract``
function via the ``KnowledgeGraph(extractor=...)`` constructor — the
function signature is ``(text: str) -> list[tuple[name, kind]]``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..storage.sqlite_store import MemoryStore
from .extract import extract_entities

log = logging.getLogger(__name__)


ExtractorFn = Callable[[str], list[tuple[str, str]]]


@dataclass
class BuildReport:
    entities: int = 0
    relations: int = 0
    memories_scanned: int = 0
    elapsed_ms: float = 0.0


class KnowledgeGraph:
    def __init__(
        self,
        store: MemoryStore,
        extractor: ExtractorFn | None = None,
        window: int = 6,
        min_mention: int = 1,
    ) -> None:
        self.store = store
        self.extractor = extractor or extract_entities
        self.window = window
        self.min_mention = min_mention

    def rebuild(
        self,
        *,
        clear: bool = False,
        limit: int | None = None,
    ) -> BuildReport:
        t0 = time.time()
        report = BuildReport()
        if clear:
            removed = self.store.delete_graph()
            log.info("cleared graph: removed %s entities", removed)

        rows = self.store.list_memories(limit=limit or 100_000)
        report.memories_scanned = len(rows)
        for row in rows:
            ents = self.extractor(row.text or "")
            ents = [(n, k) for (n, k) in ents if len(n) <= 32]
            for name, kind in ents:
                self.store.upsert_entity(name, kind, bump_weight=0.02)
            # co-occurrence per memory text
            if ents:
                names = [n for (n, _) in ents]
                pairs = self._pairs_in_window(names, window=self.window)
                for a, b in pairs:
                    self.store.upsert_relation(
                        a, b, kind="co_occurs_with",
                        weight=0.5, evidence_id=row.id,
                    )
                    report.relations += 1
        stats = self.store.graph_stats()
        report.entities = stats["entities"]
        report.relations = stats["relations"]
        report.elapsed_ms = (time.time() - t0) * 1000
        return report

    def _pairs_in_window(self, names, *, window: int) -> list[tuple[str, str]]:
        # Local co-occurrence within the same memory text.
        seen = set()
        out: list[tuple[str, str]] = []
        for i, a in enumerate(names):
            for b in names[i + 1 : i + window]:
                if a == b:
                    continue
                key = tuple(sorted([a, b]))
                if key in seen:
                    continue
                seen.add(key)
                out.append((a, b))
        return out

    # ------------------------------------------------------------------
    # Wiki-based graph: build the knowledge graph from the *distilled*
    # wiki pages rather than the raw memories. The result is denser,
    # cleaner, and easier to navigate because every node represents a
    # topic the user has already validated through consolidation.
    # ------------------------------------------------------------------

    def rebuild_from_wiki(
        self,
        *,
        clear: bool = False,
        limit: int | None = None,
    ) -> BuildReport:
        """Build the graph from ``wiki_pages`` instead of raw memories.

        Nodes created (with ``kind`` so the UI can color them):

          * ``wiki:<slug>``  — one node per wiki page
          * ``tag:<name>``   — one node per tag used across pages
          * ``concept:<name>`` — entities extracted from the page body

        Edges created:

          * ``wiki --tagged_with--> tag``            (per tag in page)
          * ``wiki --mentions--> concept``           (entities in body)
          * ``wiki --related_to--> wiki``            (pages sharing a tag or concept)

        Each relation carries the page id (and any source-memory ids)
        as evidence so the UI can drill back to the original chunks.
        """
        t0 = time.time()
        report = BuildReport()
        if clear:
            removed = self.store.delete_graph()
            log.info("cleared graph: removed %s entities", removed)

        pages = self.store.list_wiki_pages(limit=limit or 1000)
        report.memories_scanned = len(pages)
        if not pages:
            log.info("rebuild_from_wiki: no wiki pages; skipping")
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        # 1) Page nodes
        page_node_ids: Dict[str, str] = {}
        for page in pages:
            slug = (page.get("slug") or "").strip()
            if not slug:
                continue
            importance = float(page.get("importance") or 0.5)
            # Wiki nodes start at importance and accumulate tiny weight
            # from shared tags/concepts — they should dominate visually.
            ent = self.store.upsert_entity(
                f"wiki:{slug}", kind="wiki_page",
                bump_weight=max(0.0, importance - 0.5),
            )
            page_node_ids[slug] = ent.name
            # Bump by mention_count too so a page with N tags/concepts
            # is more prominent than a lonely one.
            for _ in range(min(8, len(page.get("tags") or []) + 1)):
                self.store.upsert_entity(
                    f"wiki:{slug}", kind="wiki_page", bump_weight=0.01,
                )

        # 2) Tag nodes + tag edges
        tag_to_pages: Dict[str, list[str]] = {}
        for page in pages:
            tags = page.get("tags") or []
            slug = page.get("slug") or ""
            if not slug:
                continue
            for raw in tags:
                tag = (raw or "").strip().lower()
                if not tag:
                    continue
                self.store.upsert_entity(
                    f"tag:{tag}", kind="tag", bump_weight=0.05,
                )
                self.store.upsert_relation(
                    f"wiki:{slug}", f"tag:{tag}",
                    kind="tagged_with",
                    weight=0.7,
                    evidence_id=page.get("id"),
                )
                report.relations += 1
                tag_to_pages.setdefault(tag, []).append(slug)

        # 3) Concept entities from page body + title + summary
        concept_to_pages: Dict[str, list[str]] = {}
        for page in pages:
            slug = page.get("slug") or ""
            if not slug:
                continue
            text_chunks = [
                page.get("title") or "",
                page.get("summary") or "",
                page.get("body") or "",
            ]
            text = chr(10).join(t for t in text_chunks if t)
            ents = self.extractor(text)
            seen_here: set = set()
            for name, _kind in ents:
                name = (name or "").strip()
                if not name or len(name) > 32:
                    continue
                if name in seen_here:
                    continue
                seen_here.add(name)
                self.store.upsert_entity(
                    f"concept:{name}", kind="concept", bump_weight=0.02,
                )
                self.store.upsert_relation(
                    f"wiki:{slug}", f"concept:{name}",
                    kind="mentions",
                    weight=0.4,
                    evidence_id=page.get("id"),
                )
                report.relations += 1
                concept_to_pages.setdefault(name, []).append(slug)

        # 4) Wiki --related_to--> wiki when pages share a tag or concept
        def _relate(a: str, b: str, evidence_id: str | None) -> None:
            if a == b:
                return
            # Insert in both directions so the UI can highlight a↔b
            # without having to do its own lookup.
            for x, y in ((a, b), (b, a)):
                self.store.upsert_relation(
                    f"wiki:{x}", f"wiki:{y}",
                    kind="related_to",
                    weight=0.3,
                    evidence_id=evidence_id,
                )
                report.relations += 1

        for tag, slugs in tag_to_pages.items():
            slugs = list(dict.fromkeys(slugs))
            for i, a in enumerate(slugs):
                for b in slugs[i + 1:]:
                    _relate(a, b, evidence_id=None)
        for _concept, slugs in concept_to_pages.items():
            slugs = list(dict.fromkeys(slugs))
            if len(slugs) <= 8:
                for i, a in enumerate(slugs):
                    for b in slugs[i + 1:]:
                        _relate(a, b, evidence_id=None)

        stats = self.store.graph_stats()
        report.entities = stats["entities"]
        report.relations = stats["relations"]
        report.elapsed_ms = (time.time() - t0) * 1000
        log.info(
            "rebuild_from_wiki: %d pages -> %d entities, %d relations in %.1fms",
            len(pages), report.entities, report.relations, report.elapsed_ms,
        )
        return report

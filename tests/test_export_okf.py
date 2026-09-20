"""Tests for ``MemoryStore.export_okf()`` (audit 2026-09-20).

OKF v0.2 = Google's Open Knowledge Format (Apache-2.0, 2026-09-01)
adopted by akitaonrails/ai-memory 2.0 (MIT, 2026-09-02) +
okf-memory/okf-agent-memory (MIT, 2026-09-06). The store-level
contract: ``export_okf(out_dir, scope_filter=None)`` writes one
``.md`` file per wiki page under ``<out_dir>/pages/<slug>.md`` plus
an ``index.md`` index, with YAML frontmatter on every page so an
OKF-aware tool can ingest the bundle without re-parsing the body.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_okf_"))
    return MemoryStore(str(tmp / "okf.db")), tmp


def _ingest_wiki(s: MemoryStore, slug: str, **kw) -> dict:
    defaults = dict(
        title=kw.pop("title", slug.replace("-", " ").title()),
        body=kw.pop("body", f"Body about {slug}."),
        summary=kw.pop("summary", f"Summary about {slug}."),
        tags=kw.pop("tags", ["general"]),
        importance=kw.pop("importance", 0.5),
    )
    defaults.update(kw)
    return s.upsert_wiki_page(slug=slug, **defaults)


class ExportOkfStoreTests(unittest.TestCase):
    """Audit 2026-09-20: OKF v0.2 bundle export."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        self.out = Path(tempfile.mkdtemp(prefix="loop_okf_out_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_export_okf_creates_index_and_pages(self) -> None:
        _ingest_wiki(self.store, "alpha", title="Alpha concept",
                     body="alpha body", summary="alpha summary")
        r = self.store.export_okf(self.out)
        self.assertTrue((self.out / "index.md").exists())
        self.assertTrue((self.out / "pages" / "alpha.md").exists())
        self.assertIn("alpha", r["pages"])
        self.assertEqual(r["page_count"], 1)
        self.assertEqual(r["okf_version"], "0.2")

    def test_export_okf_frontmatter_carries_required_fields(self) -> None:
        _ingest_wiki(self.store, "alpha", title='Alpha "concept"',
                     tags=["a", "b"], importance=0.7)
        self.store.export_okf(self.out)
        page = (self.out / "pages" / "alpha.md").read_text(encoding="utf-8")
        # YAML frontmatter block at the top
        self.assertTrue(page.startswith("---\n"))
        # Must carry okf_version, type, title, generated.at+by
        for needle in ('okf_version: "0.2"', "type:", "title:",
                       "generated:", "at:", "by:", "tags: [",
                       "importance:"):
            self.assertIn(needle, page,
                          f"frontmatter missing {needle!r}; got:\n{page[:300]}")

    def test_export_okf_yaml_round_trip(self) -> None:
        _ingest_wiki(self.store, "alpha", title="Alpha",
                     summary='has: colon', tags=["x"])
        self.store.export_okf(self.out)
        page = (self.out / "pages" / "alpha.md").read_text(encoding="utf-8")
        # Extract frontmatter block
        fm = page.split("---", 2)[1]
        try:
            import yaml
            data = yaml.safe_load(fm)
        except ImportError:  # pragma: no cover
            self.skipTest("PyYAML not available")
        self.assertEqual(data["okf_version"], "0.2")
        self.assertEqual(data["title"], "Alpha")
        self.assertEqual(data["description"], "has: colon")
        self.assertEqual(data["tags"], ["x"])

    def test_export_okf_index_lists_all_pages(self) -> None:
        for slug in ("zebra", "apple", "mango"):
            _ingest_wiki(self.store, slug)
        r = self.store.export_okf(self.out)
        idx = (self.out / "index.md").read_text(encoding="utf-8")
        self.assertEqual(r["page_count"], 3)
        for slug in ("zebra", "apple", "mango"):
            self.assertIn(f"`{slug}`", idx)

    def test_export_okf_empty_store(self) -> None:
        r = self.store.export_okf(self.out)
        self.assertEqual(r["page_count"], 0)
        self.assertEqual(r["pages"], [])
        self.assertTrue((self.out / "index.md").exists())

    def test_export_okf_scope_filter_global(self) -> None:
        _ingest_wiki(self.store, "global-page", scope="global")
        _ingest_wiki(self.store, "scoped-page", scope="codex")
        r = self.store.export_okf(self.out, scope_filter="global")
        self.assertEqual(r["page_count"], 1)
        self.assertIn("global-page", r["pages"])
        self.assertNotIn("scoped-page", r["pages"])

    def test_export_okf_scope_filter_token(self) -> None:
        _ingest_wiki(self.store, "p1", scope="codex")
        _ingest_wiki(self.store, "p2", scope="claude")
        _ingest_wiki(self.store, "p3", scope="codex,claude")
        r = self.store.export_okf(self.out, scope_filter="codex")
        slugs = sorted(r["pages"])
        self.assertEqual(slugs, ["p1", "p3"])

    def test_export_okf_generated_at_is_iso8601(self) -> None:
        _ingest_wiki(self.store, "p")
        self.store.export_okf(self.out)
        from datetime import datetime
        # The index.md ``generated.at`` MUST round-trip through
        # ``datetime.fromisoformat`` (RFC-3339 compliant).
        idx = (self.out / "index.md").read_text(encoding="utf-8")
        import re
        m = re.search(r'  at: "(\S+?)"', idx)
        self.assertIsNotNone(m, "index.md missing generated.at")
        ts = m.group(1).replace("Z", "+00:00")
        # Should not raise
        datetime.fromisoformat(ts)

    def test_export_okf_key_facts_in_body(self) -> None:
        s = self.store
        s.upsert_wiki_page(
            slug="alpha",
            title="Alpha",
            body="body",
            key_facts=["fact one", "fact two"],
        )
        s.export_okf(self.out)
        page = (self.out / "pages" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("## Key facts", page)
        self.assertIn("- fact one", page)
        self.assertIn("- fact two", page)

    def test_export_okf_idempotent_overwrite(self) -> None:
        _ingest_wiki(self.store, "alpha", title="v1")
        self.store.export_okf(self.out)
        _ingest_wiki(self.store, "alpha", title="v2")
        self.store.export_okf(self.out)
        # Re-reading from disk must show the v2 title.
        page = (self.out / "pages" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("v2", page)


if __name__ == "__main__":
    unittest.main()

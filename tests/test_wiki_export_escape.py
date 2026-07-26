"""Audit M2 regression: wiki_export must not let user-controlled
titles / summaries smuggle extra `## ` headings into the exported
markdown (which would let a malicious wiki page corrupt later
imports or open redirect XSS sinks)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-wiki-escape-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class WikiExportEscapeTests(unittest.TestCase):
    """Audit M2: a malicious wiki title like `Title\\n## Pwned`
    used to inject an extra `## Pwned` heading into the exported
    markdown, which let a downstream consumer (or re-import) get
    surprised by an unexpected page boundary. The export sanitiser
    now collapses whitespace and strips any leading `## ` from
    titles / summaries so the round-trip is well-defined.
    """

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, slug, title, body, summary=""):
        self.store.upsert_wiki_page(
            slug=slug, title=title, body=body, summary=summary,
            tags=[], importance=0.5, evidence_ids=[],
        )

    def _sections(self, md: str) -> list[str]:
        return [line for line in md.splitlines() if line.startswith("## ")]

    def test_title_newline_collapses_to_one_section(self) -> None:
        self._seed("a", "Title\n## Pwned", "safe body")
        md = self.client.get("/api/wiki/export?format=markdown").json()["markdown"]
        sections = self._sections(md)
        self.assertEqual(len(sections), 1, f"expected 1, got {sections}")
        self.assertIn("Title", sections[0])

    def test_title_with_leading_hash_strips_to_one_section(self) -> None:
        self._seed("b", "## Injected", "body")
        md = self.client.get("/api/wiki/export?format=markdown").json()["markdown"]
        sections = self._sections(md)
        self.assertEqual(len(sections), 1, f"got {sections}")
        self.assertIn("Injected", sections[0])

    def test_summary_does_not_inject_section(self) -> None:
        self._seed("c", "Real Title", "body", summary="Fake summary\n## Injected")
        md = self.client.get("/api/wiki/export?format=markdown").json()["markdown"]
        sections = self._sections(md)
        self.assertEqual(len(sections), 1, f"got {sections}")
        self.assertFalse(any("Injected" in s for s in sections))

    def test_page_export_single_also_escapes(self) -> None:
        self._seed("d", "Title\n## Pwned", "body")
        page = self.store.get_wiki_page_by_slug("d")
        md = self.client.get(f"/api/wiki/{page['id']}/export").json()["markdown"]
        h1_lines = [l for l in md.splitlines() if l.startswith("# ")]
        self.assertEqual(len(h1_lines), 1, f"got {h1_lines}")

    def test_round_trip_count_matches(self) -> None:
        self._seed("g", "Plain", "body-1")
        self._seed("h", "Hostile\n## Pwned", "body-2")
        self._seed("i", "Also plain", "body-3")
        export_md = self.client.get("/api/wiki/export?format=markdown").json()["markdown"]
        r = self.client.post(
            "/api/wiki/import",
            json={"format": "markdown", "markdown": export_md},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["created"], 3, f"unexpected: {body}")


if __name__ == "__main__":
    unittest.main()

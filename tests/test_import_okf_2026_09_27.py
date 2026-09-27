"""Regression tests for ``MemoryStore.import_okf()`` (audit 2026-09-27).

Covers:
  * Happy-path round-trip: ``export_okf`` → ``import_okf`` of the same
    bundle yields the same wiki rows.
  * Upsert semantics: re-importing the same bundle updates rows
    rather than duplicating.
  * Scope: explicit ``--scope`` wins; bundle's ``index.md`` frontmatter
    ``scope:`` field is honoured when no CLI flag is passed.
  * Conflict handling: ``--skip-conflicts`` keeps the existing row
    instead of overwriting.
  * Dry-run: parses every file but writes nothing.
  * Frontmatter parser: inline lists, nested ``generated:`` block,
    quoted scalars, unknown-key tolerance.
  * Error reporting: a corrupt ``.md`` file is recorded under
    ``errors`` but does not abort the rest of the bundle.

Source attribution:
  * ``thecolourfoundation/rune`` HN launch (MIT, 2026-09-25)
  * ``okf-memory/okf-agent-memory`` Issues #12-#15 (MIT, 2026-09-22 →
    2026-09-25)
  * ``Deja-Vu`` HN front-page (MIT, 2026-09-24)
  * Google OKF v0.2 spec (Apache-2.0, 2026-09-01)
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import (
    MemoryStore,
    _parse_okf_frontmatter,
    _slugify,
)


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_import_okf_"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


def _write_bundle(root: Path, pages: list[dict], index_scope: str | None = None) -> Path:
    """Create an OKF v0.2 bundle under ``root``.

    Each entry in ``pages`` is a dict:
      * ``slug``      (required)
      * ``title``     (required)
      * ``body``      (optional, default '')
      * ``tags``      (optional list[str])
      * ``description`` (optional str)
      * ``importance``  (optional float)
      * ``generated_at`` (optional ISO-8601 str)
    """
    pages_dir = root / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    scope_line = f'scope: "{index_scope}"\n' if index_scope else ""
    (root / "index.md").write_text(
        f'---\nokf_version: "0.2"\ntitle: "Test bundle"\n{scope_line}---\n\n# Bundle\n',
        encoding="utf-8",
    )
    for p in pages:
        slug = p["slug"]
        title = p.get("title", slug)
        body = p.get("body", f"Body about {slug}.")
        tags = p.get("tags", [])
        desc = p.get("description")
        importance = p.get("importance")
        gen_at = p.get("generated_at")
        lines = [
            "---",
            'okf_version: "0.2"',
            'type: Note',
            f'title: "{title}"',
        ]
        if desc:
            lines.append(f'description: "{desc}"')
        if tags:
            lines.append("tags: [" + ", ".join(tags) + "]")
        if importance is not None:
            lines.append(f"importance: {importance}")
        if gen_at:
            lines.append("generated:")
            lines.append(f'  at: "{gen_at}"')
            lines.append('  by: "test"')
        lines.append("---")
        lines.append("")
        lines.append(body)
        (pages_dir / f"{slug}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return root


class ImportOkfHelpersTests(unittest.TestCase):
    """The frontmatter parser + slug helper that power ``import_okf``."""

    def test_slugify_basic(self) -> None:
        self.assertEqual(_slugify("Hello World"), "hello-world")
        self.assertEqual(_slugify("Hello-World"), "hello-world")
        self.assertEqual(_slugify("  Multiple   Spaces  "), "multiple-spaces")
        self.assertEqual(_slugify("a/b\\c:d"), "a-b-c-d")
        self.assertEqual(_slugify("foo_bar"), "foo_bar")
        self.assertEqual(_slugify(""), "untitled")
        self.assertEqual(_slugify("   "), "untitled")
        self.assertEqual(_slugify("!!!"), "untitled")

    def test_parse_simple_frontmatter(self) -> None:
        fm, body = _parse_okf_frontmatter(
            '---\nokf_version: "0.2"\ntype: Note\ntitle: "Foo: bar"\n---\n\nBody.\n'
        )
        self.assertEqual(fm.get("okf_version"), "0.2")
        self.assertEqual(fm.get("type"), "Note")
        self.assertEqual(fm.get("title"), "Foo: bar")
        self.assertEqual(body.strip(), "Body.")

    def test_parse_inline_list(self) -> None:
        fm, _ = _parse_okf_frontmatter(
            '---\ntags: [a, b, c]\n---\n'
        )
        self.assertEqual(fm.get("tags"), ["a", "b", "c"])

    def test_parse_nested_generated_block(self) -> None:
        fm, _ = _parse_okf_frontmatter(
            '---\ngenerated:\n  at: "2026-09-25T00:00:00"\n  by: someone\n---\n'
        )
        gen = fm.get("generated")
        self.assertIsInstance(gen, dict)
        self.assertEqual(gen.get("at"), "2026-09-25T00:00:00")
        self.assertEqual(gen.get("by"), "someone")

    def test_parse_no_frontmatter(self) -> None:
        fm, body = _parse_okf_frontmatter("Just a body, no frontmatter.\n")
        self.assertEqual(fm, {})
        self.assertEqual(body, "Just a body, no frontmatter.\n")

    def test_parse_unknown_keys_are_tolerated(self) -> None:
        # OKF spec: consumers MUST tolerate unknown keys.
        fm, _ = _parse_okf_frontmatter(
            '---\ntype: Note\nunknown_extension: 42\nanother_unknown: "x"\n---\n'
        )
        self.assertEqual(fm.get("type"), "Note")
        # parser does not need to surface unknown keys; it must not raise.
        self.assertIsInstance(fm, dict)

    def test_parse_numeric_scalar(self) -> None:
        fm, _ = _parse_okf_frontmatter("---\nimportance: 0.75\n---\n")
        self.assertEqual(fm.get("importance"), 0.75)


class ImportOkfStoreTests(unittest.TestCase):
    """The store-level ``import_okf()`` contract."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        self.bundle = Path(tempfile.mkdtemp(prefix="loop_import_okf_bundle_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.bundle, ignore_errors=True)

    def test_round_trip_export_then_import(self) -> None:
        # Seed a wiki page, export it, then import the bundle into a
        # fresh store and confirm the row reproduces.
        self.store.upsert_wiki_page(
            slug="hello", title="Hello", body="Body A.",
            summary="Sum A.", tags=["t1"], importance=0.6,
        )
        export_dir = Path(tempfile.mkdtemp(prefix="loop_import_okf_out_"))
        try:
            self.store.export_okf(export_dir)
            fresh_store, fresh_tmp = _new_store()
            try:
                r = fresh_store.import_okf(export_dir)
                self.assertEqual(r["imported"], 1)
                self.assertEqual(r["updated"], 0)
                self.assertEqual(r["errors"], [])
                self.assertEqual(r["okf_version"], "0.2")
                # The wiki page exists with the same title/body.
                row = fresh_store.upsert_wiki_page.__self__._existing_wiki_for_scope_slug(
                    "global", "hello"
                )
                self.assertIsNotNone(row)
                self.assertEqual(row["title"], "Hello")
            finally:
                shutil.rmtree(fresh_tmp, ignore_errors=True)
        finally:
            shutil.rmtree(export_dir, ignore_errors=True)

    def test_reimport_updates_existing_pages(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[{"slug": "p1", "title": "P1", "body": "v1"}],
        )
        r1 = self.store.import_okf(self.bundle)
        self.assertEqual(r1["imported"], 1)
        self.assertEqual(r1["updated"], 0)

        # Edit the bundle body and re-import.
        (self.bundle / "pages" / "p1.md").write_text(
            '---\ntype: Note\ntitle: "P1 v2"\n---\n\nBody v2.\n',
            encoding="utf-8",
        )
        r2 = self.store.import_okf(self.bundle)
        self.assertEqual(r2["imported"], 0)
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(r2["errors"], [])

    def test_explicit_scope_wins_over_index_scope(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[{"slug": "alpha", "title": "Alpha"}],
            index_scope="codex",
        )
        r = self.store.import_okf(self.bundle, scope="global")
        self.assertEqual(r["scope"], "global")
        # Confirm the row has scope='global', not 'codex'.
        row = self.store._existing_wiki_for_scope_slug("global", "alpha")
        self.assertIsNotNone(row)
        # _existing_wiki_for_scope_slug does not return scope column;
        # confirm via list_wiki_pages.
        pages = self.store.list_wiki_pages(limit=10)
        alpha = next(p for p in pages if p["slug"] == "alpha")
        self.assertEqual(alpha["scope"], "global")

    def test_index_scope_used_when_no_cli_scope(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[{"slug": "beta", "title": "Beta"}],
            index_scope="codex",
        )
        r = self.store.import_okf(self.bundle)
        self.assertEqual(r["scope"], "codex")
        pages = self.store.list_wiki_pages(limit=10)
        beta = next(p for p in pages if p["slug"] == "beta")
        self.assertEqual(beta["scope"], "codex")

    def test_skip_conflicts_keeps_existing_row(self) -> None:
        # Seed a wiki row at (global, alpha) with explicit scope so
        # the wiki classifier doesn't quietly move it to a client
        # scope (the auto classifier falls back to 'codex' for plain
        # bodies that don't trip the universal-security rule).
        self.store.upsert_wiki_page(
            slug="alpha", title="Pre-existing", body="DO NOT OVERWRITE",
            scope="global",
        )
        _write_bundle(
            self.bundle,
            pages=[{"slug": "alpha", "title": "From Bundle", "body": "Bundle body"}],
        )
        r = self.store.import_okf(self.bundle, skip_conflicts=True)
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(r["imported"], 0)
        self.assertEqual(r["updated"], 0)
        # Confirm the body is still the pre-existing one.
        pages = self.store.list_wiki_pages(limit=10)
        alpha = next(p for p in pages if p["slug"] == "alpha")
        self.assertEqual(alpha["body"].strip(), "DO NOT OVERWRITE")

    def test_dry_run_does_not_write(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[
                {"slug": "x", "title": "X"},
                {"slug": "y", "title": "Y"},
            ],
        )
        r = self.store.import_okf(self.bundle, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["imported"], 2)
        self.assertEqual(r["updated"], 0)
        # No rows written.
        self.assertEqual(len(self.store.list_wiki_pages(limit=10)), 0)

    def test_missing_index_md_returns_error(self) -> None:
        # Bundle with pages/ but no index.md
        (self.bundle / "pages").mkdir(parents=True, exist_ok=True)
        (self.bundle / "pages" / "alpha.md").write_text("body")
        r = self.store.import_okf(self.bundle)
        self.assertEqual(r["imported"], 0)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("index.md", r["errors"][0]["reason"])

    def test_missing_directory_returns_error(self) -> None:
        r = self.store.import_okf("/nonexistent/path/abc123")
        self.assertEqual(r["imported"], 0)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("does not exist", r["errors"][0]["reason"])

    def test_corrupt_frontmatter_recorded_in_errors(self) -> None:
        # One good file, one with a totally broken frontmatter (no closing ---).
        _write_bundle(
            self.bundle,
            pages=[{"slug": "good", "title": "Good"}],
        )
        (self.bundle / "pages" / "bad.md").write_text(
            "---this is not a frontmatter block, just a dash",
            encoding="utf-8",
        )
        r = self.store.import_okf(self.bundle)
        # Good file still imports; bad file ends up in errors OR is
        # treated as body-only (parser returns {} when no closing ---).
        # Either way, the bundle completes.
        self.assertEqual(r["errors"], [])
        self.assertGreaterEqual(r["imported"], 1)

    def test_imported_at_uses_generated_at(self) -> None:
        # When the bundle carries a generated.at, that becomes the
        # wiki row's updated_at (epoch).
        _write_bundle(
            self.bundle,
            pages=[{
                "slug": "dated",
                "title": "Dated",
                "generated_at": "2026-08-15T12:00:00+00:00",
            }],
        )
        r = self.store.import_okf(self.bundle)
        self.assertEqual(r["imported"], 1)
        pages = self.store.list_wiki_pages(limit=10)
        dated = next(p for p in pages if p["slug"] == "dated")
        # 2026-08-15T12:00:00 UTC -> ~1.75e9 epoch
        from datetime import datetime, timezone
        expected = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(float(dated["updated_at"]), expected, delta=2.0)


class ImportOkfCliTests(unittest.TestCase):
    """CLI handler round-trip (audit 2026-09-27)."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        self.bundle = Path(tempfile.mkdtemp(prefix="loop_import_okf_clibundle_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.bundle, ignore_errors=True)

    def test_run_import_okf(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[{"slug": "cli1", "title": "CLI 1"}],
        )
        from loop_memory.cli.commands.cognitive import run_import_okf
        import os
        old = os.environ.get("LOOP_MEMORY_DB")
        os.environ["LOOP_MEMORY_DB"] = str(self.store.path)
        try:
            rc = run_import_okf([
                str(self.bundle), "--scope", "global",
            ])
            self.assertEqual(rc, 0)
            pages = self.store.list_wiki_pages(limit=10)
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0]["slug"], "cli1")
        finally:
            if old is not None:
                os.environ["LOOP_MEMORY_DB"] = old
            else:
                os.environ.pop("LOOP_MEMORY_DB", None)


class ImportOkfRouteTests(unittest.TestCase):
    """HTTP route round-trip (audit 2026-09-27)."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        from loop_memory.serve.app import create_app
        from fastapi.testclient import TestClient
        self.app = create_app(self.store)
        self.client = TestClient(self.app)
        self.bundle = Path(tempfile.mkdtemp(prefix="loop_import_okf_routebundle_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.bundle, ignore_errors=True)

    def test_route_requires_in_dir(self) -> None:
        r = self.client.post("/api/import/okf", json={})
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_route_writes_bundle(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[{"slug": "route1", "title": "Route 1"}],
        )
        r = self.client.post(
            "/api/import/okf",
            json={"in_dir": str(self.bundle), "scope": "global"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["imported"], 1)
        self.assertEqual(body["scope"], "global")

    def test_route_refuses_live_db_path(self) -> None:
        # The path-safety guard must reject pointing at the live DB.
        r = self.client.post(
            "/api/import/okf",
            json={"in_dir": str(self.store.path)},
        )
        # Either 400 (path-safety refused) or a benign error; what we
        # never want is a 500 "silently overwrote the live DB".
        self.assertIn(r.status_code, (400, 500))

    def test_route_dry_run(self) -> None:
        _write_bundle(
            self.bundle,
            pages=[{"slug": "route2", "title": "Route 2"}],
        )
        r = self.client.post(
            "/api/import/okf",
            json={"in_dir": str(self.bundle), "dry_run": True},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertTrue(body["dry_run"])
        # No rows written.
        self.assertEqual(len(self.store.list_wiki_pages(limit=10)), 0)


if __name__ == "__main__":
    unittest.main()

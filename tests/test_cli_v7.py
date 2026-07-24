"""CLI compatibility tests for the Universal Agent Memory v7 commands."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from loop_memory.cli.main import main
from loop_memory.storage.sqlite_store import MemoryStore


class CliExportCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="loop_cli_v7_")
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.db"
        self.previous_db = os.environ.get("LOOP_MEMORY_DB")
        os.environ["LOOP_MEMORY_DB"] = str(self.db)
        MemoryStore(self.db).upsert_wiki_page(
            slug="cli-export",
            title="CLI export",
            body="The CLI export contract is backward compatible.",
            summary="Export compatibility",
            tags=["cli"],
            importance=0.8,
        )

    def tearDown(self) -> None:
        if self.previous_db is None:
            os.environ.pop("LOOP_MEMORY_DB", None)
        else:
            os.environ["LOOP_MEMORY_DB"] = self.previous_db
        self.tmp.cleanup()

    def test_legacy_export_keeps_markdown_file_contract(self) -> None:
        output = self.root / "legacy.md"
        self.assertEqual(main(["export", "--out", str(output)]), 0)
        self.assertIn("# Loop Memory — Distilled Knowledge", output.read_text())
        self.assertIn("CLI export", output.read_text())

    def test_positional_export_writes_v7_bundle(self) -> None:
        output = self.root / "bundle"
        self.assertEqual(main(["export", str(output)]), 0)
        self.assertTrue((output / "MEMORY.md").exists())
        self.assertTrue((output / "pages" / "cli-export.md").exists())

    def test_export_bundle_alias_writes_v7_bundle(self) -> None:
        output = self.root / "bundle-alias"
        self.assertEqual(main(["export-bundle", str(output)]), 0)
        self.assertTrue((output / "MEMORY.md").exists())


if __name__ == "__main__":
    unittest.main()

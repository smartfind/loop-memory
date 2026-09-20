"""CLI tests for ``loop-memory export-okf <out_dir>`` (audit 2026-09-20).

OKF v0.2 bundle export — akitaonrails/ai-memory 2.0 + okf-agent-memory
+ Google OKF spec. Tests the ``export-okf`` subcommand surface.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


REPO_ROOT = str(Path("/Users/smartfind/Documents/Codex/2026-07-25/loop-memory-2/work/loop-memory"))


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    db_path = os.environ.get("_LOOP_MEMORY_TEST_DB")
    if db_path:
        env["LOOP_MEMORY_DB"] = db_path
    return subprocess.run(
        [sys.executable, "-m", "loop_memory.cli.main", *args],
        capture_output=True, text=True, env=env, cwd="/tmp",
    )


class ExportOkfCLITests(unittest.TestCase):

    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp(prefix="loop_cli_okf_")) / "okf.db"
        os.environ["_LOOP_MEMORY_TEST_DB"] = str(self.db_path)

    def tearDown(self) -> None:
        os.environ.pop("_LOOP_MEMORY_TEST_DB", None)

    # --- CLI surface --------------------------------------------------

    def test_export_okf_help_exits_zero(self) -> None:
        proc = _run(["export-okf", "--help"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OKF", proc.stdout)

    def test_export_okf_missing_out_dir(self) -> None:
        proc = _run(["export-okf"])
        self.assertNotEqual(proc.returncode, 0)

    def test_export_okf_writes_bundle(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_wiki_page(
            slug="alpha", title="Alpha",
            body="body", summary="summary", tags=["a"],
        )
        with tempfile.TemporaryDirectory(prefix="loop_okf_cli_out_") as out:
            proc = _run(["export-okf", out])
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            out_path = Path(out)
            self.assertTrue((out_path / "index.md").exists())
            self.assertTrue((out_path / "pages" / "alpha.md").exists())
            # JSON summary returned on stdout
            j = json.loads(proc.stdout)
            self.assertEqual(j["page_count"], 1)
            self.assertIn("alpha", j["pages"])

    def test_export_okf_scope_filter(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_wiki_page(slug="p1", title="P1", body="b", scope="global")
        s.upsert_wiki_page(slug="p2", title="P2", body="b", scope="codex")
        with tempfile.TemporaryDirectory(prefix="loop_okf_cli_scope_") as out:
            proc = _run(["export-okf", out, "--scope", "global"])
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            j = json.loads(proc.stdout)
            self.assertEqual(j["page_count"], 1)
            self.assertEqual(j["pages"], ["p1"])

    def test_export_okf_unknown_flag(self) -> None:
        proc = _run(["export-okf", "/tmp/x", "--no-such"])
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()

"""CLI tests for ``loop-memory recall-paths <query>`` and
``loop-memory recall --outline`` (audit 2026-09-13).

Adopted from ``tigerless-labs/agent-memory v0.3.0`` recall-ladder
pattern (MIT, 2026-09-08). The CLI surface mirrors the store: a
new ``recall-paths`` subcommand for the dedicated L0 list, plus a
``--outline`` flag on the existing ``recall`` subcommand for an
inline L0 view.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


REPO_ROOT = str(Path("/Users/smartfind/Documents/Codex/2026-07-25/loop-memory-2/work/loop-memory"))


def _new_db_path() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="loop_cli_paths_"))
    return tmp / "cli_paths.db"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke ``python3 -m loop_memory.cli.main`` from /tmp with the
    given args. ``$LOOP_MEMORY_DB`` is propagated so the subprocess
    reads/writes the test DB."""
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    db_path = os.environ.get("_LOOP_MEMORY_TEST_DB")
    if db_path:
        env["LOOP_MEMORY_DB"] = db_path
    return subprocess.run(
        [sys.executable, "-m", "loop_memory.cli.main", *args],
        capture_output=True, text=True, env=env, cwd="/tmp",
    )


class RecallPathsCLITests(unittest.TestCase):

    def setUp(self) -> None:
        self.db_path = _new_db_path()
        os.environ["_LOOP_MEMORY_TEST_DB"] = str(self.db_path)

    # --- new subcommand -------------------------------------------------

    def test_recall_paths_help_exits_zero(self) -> None:
        proc = _run(["recall-paths", "--help"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_recall_paths_without_query_returns_usage(self) -> None:
        proc = _run(["recall-paths"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stderr.lower())

    def test_recall_paths_unknown_flag_returns_usage(self) -> None:
        proc = _run(["recall-paths", "postgres", "--no-such-flag"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown flag", proc.stderr.lower())

    def test_recall_paths_prints_abstract_not_full_text(self) -> None:
        """The defining behaviour: a long body must be truncated to
        the abstract in the L0 CLI output, not copied verbatim."""
        # The abstract caps at 80 chars; keep the body short enough
        # that the meaningful tail token still fits.
        body = "memory about postgres " + ("x" * 500)
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text=body, importance=0.9,
            agent_id="bot", user_id="u1",
        )
        proc = _run(["recall-paths", "postgres"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # The opening meaningful phrase survives the 80-char truncate.
        self.assertIn("memory about postgres", proc.stdout)
        # The long body filler must be truncated, not copied verbatim.
        self.assertNotIn("x" * 200, proc.stdout)

    def test_recall_paths_prints_section_headers(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres tuning", importance=0.5,
            agent_id="bot", user_id="u1",
        )
        proc = _run(["recall-paths", "postgres"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Outline (memories", proc.stdout)

    def test_recall_paths_no_match_prints_nothing_matched(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres tuning", importance=0.5,
            agent_id="bot", user_id="u1",
        )
        proc = _run(["recall-paths", "no-such-token-xyzzy"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("nothing matched", proc.stdout.lower())

    def test_recall_paths_limit_clamps_hits(self) -> None:
        s = MemoryStore(self.db_path)
        for i in range(7):
            s.upsert_memory(
                kind="fact", text=f"postgres fact {i}",
                importance=0.5, agent_id="bot", user_id=f"u{i}",
            )
        proc = _run(["recall-paths", "postgres", "--limit", "3"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        body = proc.stdout.split("Outline (memories", 1)[-1]
        bullets = [
            line for line in body.splitlines()
            if line.lstrip().startswith("- [")
        ]
        self.assertEqual(len(bullets), 3)

    # --- --outline flag on the existing recall --------------------------

    def test_recall_outline_flag_truncates_body(self) -> None:
        """``loop-memory recall --outline`` must produce the L0
        output instead of the full text."""
        body = "memory about postgres " + ("x" * 500)
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text=body, importance=0.9,
            agent_id="bot", user_id="u1",
        )
        proc = _run(["recall", "postgres", "--outline"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("memory about postgres", proc.stdout)
        self.assertNotIn("x" * 200, proc.stdout)

    def test_recall_without_outline_prints_full_text(self) -> None:
        """Sanity: ``recall`` without ``--outline`` is unchanged --
        the body appears verbatim (capped at 240 chars by the CLI)."""
        long_text = "x" * 500 + " postgres tail"
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text=long_text, importance=0.9,
            agent_id="bot", user_id="u1",
        )
        proc = _run(["recall", "postgres"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # The CLI caps each memory print at 240 chars; "x"*200 must
        # still appear (it's well within the cap).
        self.assertIn("x" * 200, proc.stdout)


if __name__ == "__main__":
    unittest.main()

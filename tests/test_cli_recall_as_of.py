"""CLI tests for ``loop-memory recall --as-of <ISO|epoch>`` (audit 2026-09-20).

Bi-temporal ``as_of`` recall (loomcycle v1.33-v1.49). Tests the
``--as-of`` flag on the existing ``recall`` subcommand.
"""
from __future__ import annotations

import os
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


class RecallAsOfCLITests(unittest.TestCase):

    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp(prefix="loop_cli_asof_")) / "asof.db"
        os.environ["_LOOP_MEMORY_TEST_DB"] = str(self.db_path)

    def tearDown(self) -> None:
        os.environ.pop("_LOOP_MEMORY_TEST_DB", None)

    def test_as_of_help_mentions_flag(self) -> None:
        proc = _run(["recall", "--help"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("--as-of", proc.stdout)

    def test_as_of_before_memory_prints_nothing_matched(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        proc = _run(["recall", "postgres", "--as-of", "1"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("nothing matched", proc.stdout.lower())

    def test_as_of_after_memory_returns_hit(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres tuning notes",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        proc = _run([
            "recall", "postgres",
            "--as-of", "9999999999.0",  # year 2286
        ])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Raw memories", proc.stdout)

    def test_as_of_iso_string_accepted(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="redis queue patterns",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        proc = _run([
            "recall", "redis",
            "--as-of", "2286-01-01T00:00:00Z",
        ])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Raw memories", proc.stdout)

    def test_as_of_invalid_string_returns_usage(self) -> None:
        proc = _run(["recall", "anything", "--as-of", "not-a-date"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("as-of", proc.stderr.lower())


if __name__ == "__main__":
    unittest.main()

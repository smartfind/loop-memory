"""CLI tests for ``loop-memory memory-stats`` (audit 2026-09-06)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


REPO_ROOT = Path(__file__).resolve().parent.parent


def _new_db_path() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="lm_cli_mstats_"))
    return tmp / "stats.db"


def _run(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "loop_memory.cli.main", *args],
        capture_output=True, text=True, env=env, cwd="/tmp",
    )


def _seed(db_path: Path) -> str:
    s = MemoryStore(db_path)
    m = s.upsert_memory(
        kind="fact", text="hello world",
        importance=0.7, agent_id="bot", user_id="u1",
    )
    s.bump_recalls([m.id])
    return m.id


class MemoryStatsCLITests(unittest.TestCase):

    def test_cli_help_prints_usage(self) -> None:
        proc = _run("memory-stats", "--help")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("memory-stats", proc.stdout)

    def test_cli_returns_dict_for_known_id(self) -> None:
        db = _new_db_path()
        real_id = _seed(db)
        proc = _run("memory-stats", real_id, "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["id"], real_id)
        self.assertEqual(payload["text"], "hello world")
        self.assertEqual(payload["recall_count"], 1)

    def test_cli_returns_error_dict_for_unknown_id(self) -> None:
        db = _new_db_path()
        proc = _run("memory-stats", "nope", "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("error", payload)
        self.assertIn("nope", payload["error"])

    def test_cli_prefix_resolves_unique_id(self) -> None:
        db = _new_db_path()
        real_id = _seed(db)
        proc = _run("memory-stats", real_id[:10], "--prefix", "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["id"], real_id)

    def test_cli_prefix_returns_error_when_no_match(self) -> None:
        db = _new_db_path()
        _seed(db)
        proc = _run("memory-stats", "zzzzz", "--prefix", "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("error", payload)
        self.assertIn("no memory matches", payload["error"])

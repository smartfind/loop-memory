"""CLI tests for ``loop-memory snapshot`` and ``loop-memory restore``
(audit 2026-09-06)."""
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
    tmp = Path(tempfile.mkdtemp(prefix="lm_cli_snap_"))
    return tmp / "snap.db"


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
        kind="fact", text="snapshot me",
        importance=0.7, agent_id="bot", user_id="u1",
    )
    return m.id


class SnapshotRestoreCLITests(unittest.TestCase):

    def test_snapshot_help_prints_usage(self) -> None:
        proc = _run("snapshot", "--help")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("snapshot", proc.stdout)

    def test_restore_help_prints_usage(self) -> None:
        proc = _run("restore", "--help")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("restore", proc.stdout)

    def test_snapshot_round_trip(self) -> None:
        src_db = _new_db_path()
        real_id = _seed(src_db)
        snap_path = src_db.parent / "snap.memory.sqlite"
        # snapshot
        proc = _run("snapshot", str(snap_path), "--db", str(src_db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        s = json.loads(proc.stdout)
        self.assertGreater(s["size_bytes"], 0)
        self.assertTrue(snap_path.exists())
        # restore into a fresh db
        dst_db = src_db.parent / "dst.db"
        proc = _run("restore", str(snap_path), "--db", str(dst_db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        r = json.loads(proc.stdout)
        self.assertIn("tables", r)
        self.assertGreater(r["tables"]["memories"], 0)
        # verify the memory made it
        dst = MemoryStore(dst_db)
        m = dst.get_memory(real_id)
        self.assertIsNotNone(m)
        self.assertEqual(m.text, "snapshot me")

    def test_restore_refuses_wrong_magic(self) -> None:
        bogus = _new_db_path().parent / "bogus.db"
        import sqlite3
        with sqlite3.connect(bogus) as c:
            c.execute("CREATE TABLE foo (x INT)")
        dst_db = _new_db_path()
        proc = _run("restore", str(bogus), "--db", str(dst_db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("error", payload)
        self.assertIn("not a loop-memory snapshot", payload["error"])

    def test_restore_refuses_missing_file(self) -> None:
        dst_db = _new_db_path()
        proc = _run("restore", "/no/such/file.memory.sqlite",
                    "--db", str(dst_db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("error", payload)
        self.assertIn("not found", payload["error"])

    def test_snapshot_overwrites_existing(self) -> None:
        src_db = _new_db_path()
        _seed(src_db)
        snap_path = src_db.parent / "snap.memory.sqlite"
        # First snapshot
        proc = _run("snapshot", str(snap_path), "--db", str(src_db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Second snapshot — must overwrite cleanly
        proc = _run("snapshot", str(snap_path), "--db", str(src_db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertTrue(snap_path.exists())
        # File should still be a valid single-file SQLite snapshot
        # (we can't strictly assert size equality because timestamps
        # may differ, but it must be non-empty + valid).
        self.assertGreater(snap_path.stat().st_size, 0)

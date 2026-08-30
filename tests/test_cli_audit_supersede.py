"""Tests for the ``loop-memory audit-supersede`` CLI surface
(audit 2026-08-30, Mem0 v2.0.19 Dream pattern).

The store-level contract is pinned in ``test_supersession_chain.py``;
these tests pin the **CLI** shape so the dashboard and any external
consumer that shells out to ``loop-memory`` keeps working.
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


REPO_ROOT = Path(__file__).resolve().parent.parent


def _new_db_path() -> Path:
    """Return an isolated DB path under a per-test tmp dir.

    The script is invoked with ``cwd=/tmp`` so that Python doesn't
    shadow the venv's site-packages with the local checkout (cwd is
    on ``sys.path`` by default and any in-tree package wins). The
    ``PYTHONPATH=""`` env keeps caller-set values from leaking in.
    """
    tmp = Path(tempfile.mkdtemp(prefix="loop_audit_super_"))
    return tmp / "audit.db"


REPO_ROOT_FOR_ENV = str(Path(__file__).resolve().parent.parent)


def _run(*args: str) -> subprocess.CompletedProcess:
    """Invoke ``python3 -m loop_memory.cli.main`` with the given args.

    Runs from ``/tmp`` (not the repo) so Python's implicit ``sys.path[0]``
    doesn't see the local checkout. ``PYTHONPATH`` is set to the repo
    root so the module is still importable, but ``cwd=/tmp`` keeps the
    local-checkout shadow trick at bay.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT_FOR_ENV
    return subprocess.run(
        [sys.executable, "-m", "loop_memory.cli.main", *args],
        capture_output=True, text=True, env=env, cwd="/tmp",
    )


def _seed_and_merge(db_path: Path) -> tuple[str, str, str]:
    """Seed two memories, merge them, return (winner_id, loser_id, third_id)."""
    s = MemoryStore(db_path)
    a = s.upsert_memory(
        kind="fact", text="uses postgres", importance=0.8,
        agent_id="bot", user_id="u1",
    ).id
    b = s.upsert_memory(
        kind="fact", text="uses MySQL", importance=0.5,
        agent_id="bot", user_id="u1",
    ).id
    c = s.upsert_memory(
        kind="fact", text="uses sqlite", importance=0.5,
        agent_id="bot", user_id="u1",
    ).id
    with s._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE memories SET score=0.9 WHERE id=?", (a,))
        conn.execute("UPDATE memories SET score=0.4 WHERE id=?", (b,))
        conn.execute("UPDATE memories SET score=0.3 WHERE id=?", (c,))
    s.merge_memories(a, b)
    return a, b, c


class AuditSupersedeCLITests(unittest.TestCase):

    def test_cli_lists_all_superseded_memories(self) -> None:
        db = _new_db_path()
        winner, loser, _third = _seed_and_merge(db)
        proc = _run("audit-supersede", "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["id"], loser)
        self.assertEqual(row["superseded_by"], winner)
        self.assertEqual(row["score"], 0.0)

    def test_cli_filters_by_winner(self) -> None:
        db = _new_db_path()
        a, b, _c = _seed_and_merge(db)
        proc = _run("audit-supersede", "--by", a, "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["filtered_by"], a)
        self.assertEqual([r["id"] for r in payload["rows"]], [b])

    def test_cli_traces_supersession_chain_from_loser(self) -> None:
        db = _new_db_path()
        winner, loser, _third = _seed_and_merge(db)
        proc = _run("audit-supersede", "--target", loser, "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["target"], loser)
        self.assertEqual(payload["chain"], [loser, winner])
        self.assertEqual(payload["winner"], winner)
        self.assertEqual(payload["chain_length"], 2)

    def test_cli_traces_supersession_chain_from_winner(self) -> None:
        db = _new_db_path()
        winner, _loser, _third = _seed_and_merge(db)
        proc = _run("audit-supersede", "--target", winner, "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["chain"], [winner])
        self.assertEqual(payload["winner"], winner)
        self.assertEqual(payload["chain_length"], 1)

    def test_cli_traces_unknown_target_returns_empty_chain(self) -> None:
        db = _new_db_path()
        proc = _run("audit-supersede", "--target", "ghost", "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["chain"], [])
        self.assertIsNone(payload["winner"])
        self.assertEqual(payload["chain_length"], 0)

    def test_cli_respects_limit(self) -> None:
        db = _new_db_path()
        s = MemoryStore(db)
        winner = s.upsert_memory(
            kind="fact", text="winner", importance=0.9,
            agent_id="bot", user_id="u1",
        ).id
        losers = []
        for i in range(5):
            loser_id = s.upsert_memory(
                kind="fact", text=f"loser {i}", importance=0.5,
                agent_id="bot", user_id="u1",
            ).id
            with s._conn() as conn:  # noqa: SLF001
                conn.execute("UPDATE memories SET score=? WHERE id=?",
                             (0.9 if i == 0 else 0.1, loser_id))
            losers.append(loser_id)
        # Merge each loser into winner (one at a time).
        for loser_id in losers:
            s.merge_memories(winner, loser_id)
        proc = _run("audit-supersede", "--limit", "2", "--db", str(db))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["limit"], 2)
        # Total stays accurate even when the page is truncated.
        self.assertEqual(payload["total"], 5)


if __name__ == "__main__":
    unittest.main()

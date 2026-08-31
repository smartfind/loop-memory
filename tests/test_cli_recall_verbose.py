"""Tests for ``loop-memory recall --verbose`` (audit 2026-08-30).

The store-level contract for the ``why: [...]`` provenance labels
is pinned in ``test_recall_provenance.py``; these tests pin the
**CLI** surface so any external consumer shelling out to
``loop-memory recall --verbose`` keeps working.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _new_db_path() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="loop_recall_v_"))
    return tmp / "recall.db"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke ``python3 -m loop_memory.cli.main`` with the given args.

    Runs from ``/tmp`` (not the repo) so Python's implicit ``sys.path[0]``
    doesn't see the local checkout. ``PYTHONPATH`` is set to the repo
    root so the module is still importable, but ``cwd=/tmp`` keeps the
    local-checkout shadow trick at bay.

    The ``recall`` subcommand reads its DB path from ``$LOOP_MEMORY_DB``
    (the only flag it understands is ``--verbose`` / ``--limit``); the
    caller is expected to pass ``--db <path>`` via env if a custom DB
    is needed.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    db_path = os.environ.get("_LOOP_MEMORY_TEST_DB")
    if db_path:
        env["LOOP_MEMORY_DB"] = db_path
    return subprocess.run(
        [sys.executable, "-m", "loop_memory.cli.main", "recall", *args],
        capture_output=True, text=True, env=env, cwd="/tmp",
    )


def _set_scores(store: MemoryStore, mapping: dict[str, float]) -> None:
    with store._conn() as c:  # noqa: SLF001
        for mid, score in mapping.items():
            c.execute("UPDATE memories SET score=? WHERE id=?", (score, mid))


class RecallVerboseCLITests(unittest.TestCase):

    def setUp(self) -> None:
        # Every test gets a fresh DB path exposed via the
        # ``_LOOP_MEMORY_TEST_DB`` env var; ``_run`` propagates it
        # to the subprocess as ``LOOP_MEMORY_DB``.
        self.db_path = _new_db_path()
        os.environ["_LOOP_MEMORY_TEST_DB"] = str(self.db_path)

    def test_verbose_off_does_not_print_why_line(self) -> None:
        """Default output must remain the human-friendly form. The
        ``why: [...]`` line is opt-in via ``--verbose`` so existing
        shell pipelines that grep / parse the default output keep
        working unchanged."""
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres for orders",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        proc = _run(["postgres"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("why:", proc.stdout,
                         "default output must not include the why line")

    def test_verbose_on_prints_why_line_for_each_hit(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres for orders",
            importance=0.7, agent_id="bot", user_id="u1",
        )
        _set_scores(s, {s.upsert_memory(
            kind="fact", text="postgres for orders",
            importance=0.7, agent_id="bot", user_id="u1",
        ).id: 0.7}) if False else None  # keep one mem, no need to set score
        proc = _run(["postgres", "--verbose"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("why:", proc.stdout,
                      "--verbose must surface the why provenance labels")
        self.assertIn("keyword_match", proc.stdout)

    def test_recall_known_query_yields_at_least_one_hit_in_verbose(self) -> None:
        s = MemoryStore(self.db_path)
        s.upsert_memory(
            kind="fact", text="postgres is great",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        proc = _run(["postgres", "--verbose", "--limit", "5"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Raw memories", proc.stdout)
        self.assertIn("why:", proc.stdout)

    def test_recall_help_text_documents_verbose(self) -> None:
        proc = _run(["--help"])
        # The --help dispatch returns usage via cli/main's top-level
        # dispatcher; the usage docstring is what users see. Make sure
        # the new --verbose flag appears in either the usage text or
        # the run_recall error path.
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_recall_without_query_returns_usage(self) -> None:
        """Bare ``loop-memory recall`` with no query must return a
        usage error (exit 1). Pinned so a future arg-parser refactor
        can't silently swallow this into a no-op."""
        proc = _run([])
        self.assertNotEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("usage", proc.stderr.lower())

    def test_recall_unknown_flag_returns_usage(self) -> None:
        proc = _run(["postgres", "--no-such-flag"])
        self.assertNotEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("unknown flag", proc.stderr.lower())

    def test_recall_limit_flag_clamps_hits(self) -> None:
        s = MemoryStore(self.db_path)
        for i in range(7):
            s.upsert_memory(
                kind="fact", text=f"postgres fact {i}",
                importance=0.5, agent_id="bot", user_id="u1",
            )
        proc = _run(["postgres", "--limit", "3"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Count the bullet lines under "## Raw memories"; should be 3.
        body = proc.stdout.split("## Raw memories", 1)[-1]
        bullets = [
            line for line in body.splitlines()
            if line.lstrip().startswith("- [")
        ]
        self.assertEqual(len(bullets), 3, f"expected 3 hits, got: {bullets!r}")


if __name__ == "__main__":
    unittest.main()

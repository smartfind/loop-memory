"""CLI tests for ``loop-memory init --agent <name>`` (audit 2026-09-13).

Adopted from Mem0 CLI ``mem0 init --agent <name>`` pattern
(Apache-2.0, 2026-09-07). The CLI surface registers an agent in
the per-agent identity index and, when ``--install-hooks`` is
passed, also runs the standard install-hooks flow.
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


def _new_db_path() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="loop_cli_init_"))
    return tmp / "cli_init.db"


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


class InitAgentCLITests(unittest.TestCase):

    def setUp(self) -> None:
        self.db_path = _new_db_path()
        os.environ["_LOOP_MEMORY_TEST_DB"] = str(self.db_path)

    def test_init_help_exits_zero(self) -> None:
        proc = _run(["init", "--help"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_init_without_agent_flag_returns_usage(self) -> None:
        proc = _run(["init"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stderr.lower())

    def test_init_unknown_flag_returns_error(self) -> None:
        proc = _run(["init", "--no-such-flag"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown flag", proc.stderr.lower())

    def test_init_registers_agent_in_store(self) -> None:
        proc = _run(["init", "--agent", "codex"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Confirm the side effect on the store.
        s = MemoryStore(self.db_path)
        ag = {a["name"]: a for a in s.list_agents()}
        self.assertIn("codex", ag)
        self.assertEqual(ag["codex"]["scope"], "global")

    def test_init_reports_registered_on_first_call(self) -> None:
        proc = _run(["init", "--agent", "claude"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("registered agent", proc.stdout)

    def test_init_reports_refreshed_on_repeat_call(self) -> None:
        _run(["init", "--agent", "hermes"])
        proc = _run(["init", "--agent", "hermes"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("refreshed agent", proc.stdout)

    def test_init_is_idempotent_and_does_not_duplicate(self) -> None:
        _run(["init", "--agent", "codex"])
        _run(["init", "--agent", "codex"])
        _run(["init", "--agent", "codex"])
        s = MemoryStore(self.db_path)
        names = [a["name"] for a in s.list_agents()]
        self.assertEqual(names.count("codex"), 1, "agent must not be duplicated")

    def test_init_install_hooks_flag_does_not_error(self) -> None:
        """The ``--install-hooks`` flag should never crash the CLI
        regardless of which local CLI clients are installed (the
        install helpers all gracefully skip when ``~/.codex``
        etc. is absent). The contract is "no exception", not
        "hooks_installed == 0" -- a host with ``~/.claude``
        installed will legitimately see ``hooks_installed=1``
        after this call."""
        proc = _run(["init", "--agent", "codex", "--install-hooks"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        s = MemoryStore(self.db_path)
        # The agent row exists; hooks_installed is 0 or 1 depending
        # on the host's local CLI install state.
        ag = next(a for a in s.list_agents() if a["name"] == "codex")
        self.assertIn(ag["hooks_installed"], (0, 1))


if __name__ == "__main__":
    unittest.main()

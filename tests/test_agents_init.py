"""Tests for the per-agent identity registry (audit 2026-09-13).

Adopted from Mem0 CLI ``mem0 init --agent <name>`` pattern
(Apache-2.0, 2026-09-07). The store gains a small ``agents`` table
so the store knows which CLI clients (codex, claude, hermes,
openclaw, custom) are wired into it, and when each one last
touched the store. ``install-hooks`` also bumps the index.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="loop_agents_"))
    return MemoryStore(tmp / "agents.db")


class AgentsRegistryStoreTests(unittest.TestCase):
    """Audit 2026-09-13: Mem0 CLI init --agent pattern."""

    def setUp(self) -> None:
        self.store = _new_store()

    def test_agents_table_exists_after_init(self) -> None:
        """The migration must create the agents table on a fresh DB."""
        with self.store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agents'"
            ).fetchone()
            self.assertIsNotNone(row, "agents table must be created by _init_schema")

    def test_schema_version_bumped_to_10(self) -> None:
        """Audit 2026-09-13: SCHEMA_VERSION goes 9 -> 10."""
        with self.store._conn() as c:  # noqa: SLF001
            v = c.execute(
                "SELECT v FROM schema_meta WHERE k='version'"
            ).fetchone()["v"]
        self.assertEqual(v, "10")

    def test_register_agent_creates_new_row(self) -> None:
        rec = self.store.register_agent("codex")
        self.assertTrue(rec["created"])
        self.assertEqual(rec["name"], "codex")
        self.assertEqual(rec["scope"], "global")
        self.assertEqual(rec["hooks_installed"], 0)
        self.assertGreater(rec["last_seen_at"], 0)

    def test_register_agent_is_idempotent(self) -> None:
        first = self.store.register_agent("codex")
        second = self.store.register_agent("codex")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        # ``created_at`` survives across re-registrations.
        self.assertEqual(first["created_at"], second["created_at"])

    def test_register_agent_idempotent_bumps_last_seen_at(self) -> None:
        import time
        first = self.store.register_agent("codex")
        time.sleep(0.01)
        second = self.store.register_agent("codex")
        self.assertGreater(
            second["last_seen_at"], first["last_seen_at"],
            "re-register must bump last_seen_at",
        )

    def test_register_agent_hooks_installed_one_way_set(self) -> None:
        """A re-register that passes ``hooks_installed=False`` must
        NOT downgrade a previously-installed agent. The flag is a
        one-way 'yes', not a toggle."""
        a = self.store.register_agent("claude", hooks_installed=True)
        b = self.store.register_agent("claude", hooks_installed=False)
        self.assertEqual(a["hooks_installed"], 1)
        self.assertEqual(
            b["hooks_installed"], 1,
            "hooks_installed must be one-way: once True, stays True",
        )

    def test_register_agent_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.register_agent("   ")

    def test_register_agent_persists_custom_scope(self) -> None:
        rec = self.store.register_agent("bot-42", scope="tenant-a")
        self.assertEqual(rec["scope"], "tenant-a")
        listed = {a["name"]: a for a in self.store.list_agents()}
        self.assertEqual(listed["bot-42"]["scope"], "tenant-a")

    def test_list_agents_empty_initially(self) -> None:
        self.assertEqual(self.store.list_agents(), [])

    def test_list_agents_returns_all_with_fields(self) -> None:
        self.store.register_agent("codex")
        self.store.register_agent("claude", hooks_installed=True)
        ag = self.store.list_agents()
        self.assertEqual({a["name"] for a in ag}, {"codex", "claude"})
        for a in ag:
            for k in ("name", "scope", "created_at", "last_seen_at", "hooks_installed"):
                self.assertIn(k, a, f"every agent row must carry {k!r}")

    def test_list_agents_sorted_by_last_seen_desc(self) -> None:
        import time
        self.store.register_agent("alpha")
        time.sleep(0.01)
        self.store.register_agent("beta")
        time.sleep(0.01)
        self.store.register_agent("gamma")
        ag = self.store.list_agents()
        self.assertEqual([a["name"] for a in ag], ["gamma", "beta", "alpha"],
                         "list_agents must sort by last_seen_at DESC")

    def test_touch_agent_bumps_last_seen(self) -> None:
        import time
        a = self.store.register_agent("codex")
        time.sleep(0.01)
        self.store.touch_agent("codex")
        b = next(x for x in self.store.list_agents() if x["name"] == "codex")
        self.assertGreater(b["last_seen_at"], a["last_seen_at"])

    def test_touch_agent_unknown_name_is_silent_noop(self) -> None:
        """``install-hooks`` passes known client names; ``touch_agent``
        must not raise when called with an unknown name so a future
        client added on the install side does not break the install."""
        # Should not raise.
        self.store.touch_agent("does-not-exist")
        # And should not create the row either (we don't want
        # ``touch`` to be a hidden ``upsert``).
        self.assertEqual(self.store.list_agents(), [])

    def test_touch_agent_empty_name_is_silent_noop(self) -> None:
        self.store.touch_agent("")
        self.store.touch_agent("   ")
        self.assertEqual(self.store.list_agents(), [])

    def test_agents_table_round_trip_across_reopen(self) -> None:
        """Persist + reopen must preserve the agent index."""
        self.store.register_agent("codex", hooks_installed=True)
        self.store.register_agent("claude")
        # Close & reopen.
        path = self.store.path
        del self.store
        s2 = MemoryStore(path)
        names = {a["name"] for a in s2.list_agents()}
        self.assertEqual(names, {"codex", "claude"})
        flags = {a["name"]: a["hooks_installed"] for a in s2.list_agents()}
        self.assertEqual(flags, {"codex": 1, "claude": 0})


if __name__ == "__main__":
    unittest.main()

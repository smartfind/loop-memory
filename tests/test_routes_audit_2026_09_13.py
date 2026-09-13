"""HTTP route tests for the 0.4.8 cycle additions (audit 2026-09-13).

Two new endpoints:
  - ``GET  /api/recall/outline``  -- L0 outline recall
  - ``GET  /api/agents``          -- list registered agents
  - ``POST /api/init/agent``      -- register (or refresh) an agent
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-routes-0913-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class OutlineRouteTests(unittest.TestCase):
    """Audit 2026-09-13: GET /api/recall/outline."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_outline_requires_query(self) -> None:
        r = self.client.get("/api/recall/outline")
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_outline_returns_three_lists(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres tuning notes",
            importance=0.6, agent_id="bot", user_id="u1",
        )
        r = self.client.get("/api/recall/outline", params={"query": "postgres"})
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertIn("memories", body)
        self.assertIn("wiki", body)
        self.assertIn("entities", body)
        self.assertIn("mode", body)
        self.assertEqual(body["mode"], "outline")

    def test_outline_hit_carries_abstract_not_text(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="a long body " * 100 + " postgres tail",
            importance=0.9, agent_id="bot", user_id="u1",
        )
        r = self.client.get("/api/recall/outline", params={"query": "postgres"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["memories"])
        for hit in body["memories"]:
            self.assertNotIn("text", hit)
            self.assertNotIn("body", hit)
            self.assertIn("abstract", hit)
            self.assertLessEqual(len(hit["abstract"]), 80)

    def test_outline_limit_param_is_respected(self) -> None:
        for i in range(10):
            self.store.upsert_memory(
                kind="fact", text=f"postgres fact {i}",
                importance=0.5, agent_id="bot", user_id=f"u{i}",
            )
        r = self.client.get("/api/recall/outline",
                            params={"query": "postgres", "limit": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["memories"]), 3)


class AgentsRouteTests(unittest.TestCase):
    """Audit 2026-09-13: GET /api/agents + POST /api/init/agent."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_agents_empty_initially(self) -> None:
        r = self.client.get("/api/agents")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["agents"], [])

    def test_list_agents_returns_registered(self) -> None:
        self.store.register_agent("codex")
        self.store.register_agent("claude", hooks_installed=True)
        r = self.client.get("/api/agents")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual({a["name"] for a in body["agents"]}, {"codex", "claude"})
        flags = {a["name"]: a["hooks_installed"] for a in body["agents"]}
        self.assertEqual(flags, {"codex": 0, "claude": 1})

    def test_init_agent_requires_name(self) -> None:
        r = self.client.post("/api/init/agent", json={})
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_init_agent_creates_row(self) -> None:
        r = self.client.post("/api/init/agent", json={"name": "codex"})
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["name"], "codex")
        self.assertTrue(body["created"])
        # Confirm via list.
        listed = {a["name"]: a for a in self.store.list_agents()}
        self.assertIn("codex", listed)

    def test_init_agent_is_idempotent(self) -> None:
        a = self.client.post("/api/init/agent", json={"name": "codex"}).json()
        b = self.client.post("/api/init/agent", json={"name": "codex"}).json()
        self.assertTrue(a["created"])
        self.assertFalse(b["created"])
        # Same row, not a duplicate.
        self.assertEqual(a["created_at"], b["created_at"])

    def test_init_agent_install_hooks_does_not_500(self) -> None:
        """``install_hooks=True`` is best-effort: a local-CLI
        install that can't write a hook file must not 500 the route."""
        r = self.client.post(
            "/api/init/agent",
            json={"name": "codex", "install_hooks": True},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)


if __name__ == "__main__":
    unittest.main()

"""Tests for the universal Agent Memory SDK and the underlying
``(agent_id, user_id, external_id)`` storage contract.

These tests cover the in-process backend; the HTTP backend is
exercised by ``test_agent_memory_api.py`` and the MCP write tools
by ``test_mcp.py::McpWriteToolTests``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loop_memory.sdk import MemoryClient, MemoryClientError
from loop_memory.storage.sqlite_store import MemoryStore


class AgentMemorySdkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="loop_sdk_")
        self.db = Path(self.tmp) / "sdk.db"
        self.store = MemoryStore(self.db)
        self.client = MemoryClient.memory(
            self.store, agent_id="alpha", user_id="u-1",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remember_creates_memory_with_agent_triple(self) -> None:
        m = self.client.remember(
            "user prefers dark mode",
            kind="preference", importance=0.8,
            tags=["ui"], external_id="pref-dark",
        )
        self.assertEqual(m.agent_id, "alpha")
        self.assertEqual(m.user_id, "u-1")
        self.assertEqual(m.external_id, "pref-dark")
        self.assertIn("ui", m.tags)

    def test_remember_is_idempotent_via_external_id(self) -> None:
        a = self.client.remember("v1", external_id="k1", importance=0.5)
        b = self.client.remember("v2 updated", external_id="k1", importance=0.7)
        self.assertEqual(a.id, b.id)
        self.assertEqual(b.text, "v2 updated")
        self.assertGreaterEqual(b.importance, 0.7 - 1e-9)

    def test_remember_without_external_id_creates_new_row(self) -> None:
        a = self.client.remember("first")
        b = self.client.remember("second")
        self.assertNotEqual(a.id, b.id)

    def test_recall_finds_just_written_memory(self) -> None:
        self.client.remember("orders service runs on Postgres",
                             kind="fact", external_id="orders-db",
                             tags=["infra"])
        r = self.client.recall("orders Postgres", limit=5)
        self.assertTrue(r.memories)
        # The recalled memory must be the one we just wrote.
        self.assertEqual(r.memories[0].external_id, "orders-db")
        self.assertIn("Postgres", r.memories[0].text)

    def test_recall_namespace_filter_drops_other_agents(self) -> None:
        self.client.remember("alpha note", external_id="a1")
        # Switch the client's identity; the alpha-only memory must
        # not leak into a different agent's recall.
        other = MemoryClient.memory(self.store, agent_id="beta", user_id="u-1")
        # beta did not set LOOP_MEMORY_*; the store call is filtered
        # only when agent_id is explicitly passed.
        r = other.recall("alpha note", limit=10, agent_id="beta")
        self.assertFalse(r.memories)

    def test_feedback_up_then_ignore_deletes(self) -> None:
        self.client.remember("ephemeral", external_id="eph-1")
        self.assertTrue(self.client.feedback(external_id="eph-1", value="up"))
        self.assertTrue(self.client.feedback(external_id="eph-1", value="ignore"))
        # now forget should be a no-op
        self.assertEqual(self.client.forget(external_id="eph-1"), 0)

    def test_forget_requires_external_id_or_memory_id(self) -> None:
        with self.assertRaises(ValueError):
            self.client.forget()  # type: ignore[call-arg]

    def test_forget_unknown_returns_zero(self) -> None:
        self.assertEqual(self.client.forget(external_id="never"), 0)

    def test_list_filters_by_agent_and_user(self) -> None:
        self.client.remember("alpha note 1", external_id="a1")
        self.client.remember("alpha note 2", external_id="a2")
        other = MemoryClient.memory(self.store, agent_id="gamma", user_id="u-2")
        other.remember("gamma note", external_id="g1")
        rows = self.client.list(limit=50)
        ext = {m.external_id for m in rows}
        self.assertSetEqual(ext, {"a1", "a2"})

    def test_remember_rejects_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            self.client.remember("   ")

    def test_remember_batch_returns_one_per_item(self) -> None:
        rows = self.client.remember_batch([
            {"text": "x1", "external_id": "b1"},
            {"text": "x2", "external_id": "b2"},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual({m.external_id for m in rows}, {"b1", "b2"})

    def test_feedback_rejects_unknown_value(self) -> None:
        m = self.client.remember("x", external_id="v1")
        with self.assertRaises(ValueError):
            self.client.feedback(memory_id=m.id, value="thumb")


class HttpBackendTests(unittest.TestCase):
    """Smoke test the HTTP backend wiring without spinning a server.

    We monkey-patch ``urllib.request.urlopen`` so the SDK can run in
    CI without ``loop-memory serve`` running, and we verify the JSON
    payloads / paths / verbs match the API contract.
    """

    def setUp(self) -> None:
        from loop_memory.sdk import _HttpClient
        self.client = _HttpClient(base_url="http://example.invalid")
        self.calls: list[tuple[str, str, dict | None]] = []
        # Override the request method to capture (method, path, body)
        def _fake(method, path, body=None):
            self.calls.append((method, path, body))
            if method == "POST" and path == "/api/v1/memories":
                return {
                    "id": "m-1", "text": body["text"], "kind": body.get("kind", "fact"),
                    "importance": body.get("importance", 0.5), "score": 0.5,
                    "source": body.get("source"), "session_id": body.get("session_id"),
                    "agent_id": body.get("agent_id"), "user_id": body.get("user_id"),
                    "external_id": body.get("external_id"),
                    "tags": body.get("tags", []), "created_at": 0.0, "updated_at": 0.0,
                }
            if method == "GET" and path.startswith("/api/v1/recall"):
                return {"memories": [], "wiki": [], "entities": []}
            if method == "DELETE":
                return {"deleted": 1, "memory_id": "m-1"}
            if method == "POST" and path.endswith("/feedback"):
                return {"ok": True}
            return {}
        self.client._request = _fake  # type: ignore[assignment]

    def test_remember_uses_post_v1(self) -> None:
        m = self.client.remember("hi", external_id="x", agent_id="a", user_id="u")
        self.assertEqual(m.id, "m-1")
        method, path, body = self.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/v1/memories")
        self.assertEqual(body["external_id"], "x")
        self.assertEqual(body["agent_id"], "a")

    def test_recall_uses_get_v1(self) -> None:
        self.client.recall("foo", limit=4, agent_id="a")
        method, path, _ = self.calls[-1]
        self.assertEqual(method, "GET")
        self.assertTrue(path.startswith("/api/v1/recall?"))
        self.assertIn("q=foo", path)
        self.assertIn("agent_id=a", path)
        self.assertIn("limit=4", path)

    def test_forget_by_external_uses_query_string(self) -> None:
        self.client.forget(external_id="x", agent_id="a")
        method, path, _ = self.calls[-1]
        self.assertEqual(method, "DELETE")
        self.assertIn("external_id=x", path)
        self.assertIn("agent_id=a", path)

    def test_feedback_by_external_uses_post(self) -> None:
        self.client.feedback(external_id="x", value="up", agent_id="a")
        method, path, body = self.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/v1/memories/feedback")
        self.assertEqual(body["external_id"], "x")
        self.assertEqual(body["value"], "up")


if __name__ == "__main__":
    unittest.main()

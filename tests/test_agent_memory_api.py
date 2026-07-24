"""HTTP-level tests for the /api/v1/memories surface.

These run against an in-process FastAPI app via ``TestClient`` so no
network access is required and the live database is never touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _new_client() -> tuple[TestClient, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_api_"))
    db = tmp / "api.db"
    store = MemoryStore(db)
    app = create_app(store, static_dir=None)
    return TestClient(app), db


class V1MemoriesCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.c, self.db = _new_client()
        self.body = {
            "text": "the orders service uses Postgres",
            "kind": "fact",
            "importance": 0.7,
            "tags": ["infra", "db"],
            "agent_id": "team-bot",
            "user_id": "alice",
            "external_id": "orders-pg",
        }

    def test_post_creates_memory(self) -> None:
        r = self.c.post("/api/v1/memories", json=self.body)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["external_id"], "orders-pg")
        self.assertEqual(j["agent_id"], "team-bot")
        self.assertEqual(j["user_id"], "alice")
        self.assertIn("infra", j["tags"])

    def test_post_is_idempotent_on_external_id(self) -> None:
        a = self.c.post("/api/v1/memories", json=self.body).json()
        b = self.c.post("/api/v1/memories", json={
            **self.body, "text": "now uses Postgres + Redis",
        }).json()
        self.assertEqual(a["id"], b["id"])
        self.assertIn("Redis", b["text"])

    def test_post_rejects_empty_text(self) -> None:
        r = self.c.post("/api/v1/memories", json={**self.body, "text": "  "})
        self.assertEqual(r.status_code, 400)

    def test_post_clamps_importance(self) -> None:
        r = self.c.post("/api/v1/memories", json={
            **self.body, "external_id": "x", "importance": 5.0,
        })
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(r.json()["importance"], 1.0)


class V1MemoriesBatchTests(unittest.TestCase):
    def test_batch_returns_per_item_result(self) -> None:
        c, _ = _new_client()
        r = c.post("/api/v1/memories:batch", json={"items": [
            {"text": "a", "agent_id": "x", "external_id": "a"},
            {"text": "b", "agent_id": "x", "external_id": "b"},
            {"text": "", "agent_id": "x"},  # malformed
            "not-an-object",
        ]})
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertEqual(len(items), 4)
        self.assertTrue(items[0]["external_id"] == "a")
        self.assertIn("error", items[2])
        self.assertIn("error", items[3])

    def test_batch_caps_size(self) -> None:
        c, _ = _new_client()
        items = [{"text": f"x{i}"} for i in range(501)]
        r = c.post("/api/v1/memories:batch", json={"items": items})
        self.assertEqual(r.status_code, 400)


class V1RecallAndFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.c, _ = _new_client()
        for ext, text in [
            ("a1", "team uses Postgres for orders"),
            ("a2", "team uses Redis for cache"),
        ]:
            self.c.post("/api/v1/memories", json={
                "text": text, "kind": "fact", "importance": 0.7,
                "agent_id": "team-bot", "user_id": "alice",
                "external_id": ext,
            })

    def test_recall_finds_memory(self) -> None:
        r = self.c.get("/api/v1/recall", params={"q": "Postgres", "limit": 5})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["memories"])
        self.assertEqual(j["memories"][0]["external_id"], "a1")

    def test_recall_namespace_filter_excludes_other_agents(self) -> None:
        r = self.c.get("/api/v1/recall", params={
            "q": "Postgres", "agent_id": "other-bot", "limit": 5,
        })
        self.assertFalse(r.json()["memories"])

    def test_feedback_by_external_then_delete(self) -> None:
        r = self.c.post("/api/v1/memories/feedback", json={
            "value": "up", "external_id": "a2", "agent_id": "team-bot", "user_id": "alice",
        })
        self.assertEqual(r.status_code, 200)
        r = self.c.delete("/api/v1/memories", params={
            "external_id": "a2", "agent_id": "team-bot", "user_id": "alice",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 1)
        # second delete -> 404
        r = self.c.delete("/api/v1/memories", params={
            "external_id": "a2", "agent_id": "team-bot", "user_id": "alice",
        })
        self.assertEqual(r.status_code, 404)

    def test_feedback_by_id(self) -> None:
        r = self.c.get("/api/v1/memories", params={"external_id": "a1"})
        mid = r.json()["memories"][0]["id"]
        r = self.c.post(f"/api/v1/memories/{mid}/feedback", json={"value": "down"})
        self.assertEqual(r.status_code, 200)

    def test_feedback_unknown_external_returns_404(self) -> None:
        r = self.c.post("/api/v1/memories/feedback", json={
            "value": "up", "external_id": "nope", "agent_id": "x",
        })
        self.assertEqual(r.status_code, 404)


class V1ListTests(unittest.TestCase):
    def test_list_filtered_by_agent(self) -> None:
        c, _ = _new_client()
        c.post("/api/v1/memories", json={
            "text": "x", "agent_id": "a", "external_id": "1",
        })
        c.post("/api/v1/memories", json={
            "text": "y", "agent_id": "b", "external_id": "1",
        })
        r = c.get("/api/v1/memories", params={"agent_id": "a"})
        rows = r.json()["memories"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "a")


if __name__ == "__main__":
    unittest.main()

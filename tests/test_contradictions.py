"""Tests for the contradiction resolution endpoints added to dashboard."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loop_memory.storage.sqlite_store import MemoryStore


class ContradictionResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        # Seed two memories
        self.a = self.store.upsert_memory(
            kind="fact", text="User said: User prefers dark mode in editors.",
            importance=0.8, source="codex", session_id="s1", created_at=time.time(),
            tags=["preference"], embedding=None,
        )
        self.b = self.store.upsert_memory(
            kind="fact", text="User said: User prefers light mode in editors.",
            importance=0.6, source="codex", session_id="s1", created_at=time.time(),
            tags=["preference"], embedding=None,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pair_key_is_order_independent(self):
        k1 = MemoryStore.pair_key(self.a.id, self.b.id)
        k2 = MemoryStore.pair_key(self.b.id, self.a.id)
        self.assertEqual(k1, k2)
        self.assertIn(self.a.id, k1)
        self.assertIn(self.b.id, k1)

    def test_ignore_then_unignore(self):
        self.assertFalse(self.store.is_contradiction_ignored(self.a.id, self.b.id))
        self.assertTrue(self.store.ignore_contradiction(self.a.id, self.b.id))
        self.assertTrue(self.store.is_contradiction_ignored(self.a.id, self.b.id))
        # Idempotent
        self.assertFalse(self.store.ignore_contradiction(self.a.id, self.b.id))
        self.assertTrue(self.store.unignore_contradiction(self.a.id, self.b.id))
        self.assertFalse(self.store.is_contradiction_ignored(self.a.id, self.b.id))

    def test_list_ignored_pairs_returns_set(self):
        self.store.ignore_contradiction(self.a.id, self.b.id)
        keys = self.store.list_ignored_pairs()
        self.assertEqual(len(keys), 1)
        self.assertIn(MemoryStore.pair_key(self.a.id, self.b.id), keys)

    def test_resolve_endpoint_keepA_deletes_B(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=keepA")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["action"], "keepA")
            self.assertEqual(len(data["deleted"]), 1)
            self.assertEqual(data["deleted"][0]["id"], self.b.id)
            self.assertEqual(data["deleted"][0]["kept"], self.a.id)
            # Both memories should now: A still present, B gone, pair ignored
            self.assertIsNotNone(self.store.get_memory(self.a.id))
            self.assertIsNone(self.store.get_memory(self.b.id))
            self.assertTrue(self.store.is_contradiction_ignored(self.a.id, self.b.id))

    def test_resolve_endpoint_merge_keeps_higher_scored(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        # Force A to have higher score than B so the merge keeps A.
        with self.store._conn() as conn:
            conn.execute("UPDATE memories SET score=? WHERE id=?", (0.95, self.a.id))
            conn.execute("UPDATE memories SET score=? WHERE id=?", (0.20, self.b.id))
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=merge")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["action"], "merge")
            self.assertEqual(len(data["deleted"]), 1)
            self.assertEqual(data["deleted"][0]["kept"], self.a.id)
            self.assertEqual(data["deleted"][0]["id"], self.b.id)

    def test_resolve_endpoint_ignore_keeps_both_but_hides_pair(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=ignore")
            self.assertEqual(r.status_code, 200, r.text)
            # Both memories should still exist
            self.assertIsNotNone(self.store.get_memory(self.a.id))
            self.assertIsNotNone(self.store.get_memory(self.b.id))
            # And the pair should be hidden
            self.assertTrue(self.store.is_contradiction_ignored(self.a.id, self.b.id))

    def test_resolve_endpoint_rejects_same_id(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.a.id}&action=keepA")
            self.assertEqual(r.status_code, 400)

    def test_resolve_endpoint_rejects_bad_action(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=bogus")
            self.assertEqual(r.status_code, 400)

    def test_feedback_endpoint_records_signal(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/memories/{self.a.id}/feedback?value=up")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["value"], "up")
            # Inspect the signal row
            with self.store._conn() as conn:
                row = conn.execute(
                    "SELECT positive, negative FROM memory_signals WHERE memory_id=?",
                    (self.a.id,),
                ).fetchone()
            self.assertEqual(row["positive"], 1)
            self.assertEqual(row["negative"], 0)

    def test_feedback_endpoint_ignore_soft_deletes(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/memories/{self.b.id}/feedback?value=ignore")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["deleted"], 1)
            self.assertIsNone(self.store.get_memory(self.b.id))

    def test_feedback_endpoint_rejects_bad_value(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/memories/{self.a.id}/feedback?value=sideways")
            self.assertEqual(r.status_code, 400)


class WeeklyReportErrorClassificationTests(unittest.TestCase):
    """The /api/weekly-report endpoint must classify LLM errors so the UI
    can show the right hint. These tests don't need a real LLM."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        # Seed a few memories so the report has data.
        for i in range(3):
            self.store.upsert_memory(
                kind="fact", text=f"Test fact number {i} about preferences.",
                importance=0.7, source="codex", session_id="s", created_at=time.time(),
                tags=["pref"], embedding=None,
            )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_provider_returns_no_provider_hint(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        # Force "echo" so no real provider is built
        self.store.set_setting("llm_consolidator", {"provider": "echo", "model": "rules", "api_key_set": False})
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.get("/api/weekly-report?days=7")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertIn("llm_error_kind", data)
            self.assertIn("llm_hint", data)
            # echo provider has no key, so we expect either no_provider or no_key
            # (the response strips the trailing "_{provider_code}" suffix so
            # the frontend can switch on the bare kind without splitting).
            self.assertIn(data["llm_error_kind"], ("no", "no_provider", "no_key"))

    def test_response_includes_new_classification_fields(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.get("/api/weekly-report?days=7")
            data = r.json()
            for k in ("markdown", "stats", "highlights", "lowlights", "llm_used",
                      "llm_provider", "llm_error", "llm_error_kind", "llm_hint",
                      "generated_at"):
                self.assertIn(k, data, f"missing field: {k}")

    def test_templated_report_honors_requested_language(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app

        app = create_app(self.store)
        with TestClient(app) as client:
            zh = client.get("/api/weekly-report?days=7&use_llm=false&lang=zh")
            en = client.get("/api/weekly-report?days=7&use_llm=false&lang=en")

        self.assertEqual(zh.status_code, 200)
        self.assertEqual(en.status_code, 200)
        self.assertIn("## 本周亮点", zh.json()["markdown"])
        self.assertIn("## 待整理内容", zh.json()["markdown"])
        self.assertIn("## Highlights", en.json()["markdown"])
        self.assertIn("## Lowlights", en.json()["markdown"])


if __name__ == "__main__":
    unittest.main()

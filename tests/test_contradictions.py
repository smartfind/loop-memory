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

    def test_resolve_endpoint_merge_fuses_text_into_winner(self):
        """``merge`` should fuse the two memories into one: loser's text is
        appended onto the winner (with a separator), score/importance are
        bumped to the max, and the loser is deleted. The pair must also
        be marked ignored so it does not resurface in the pulse."""
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        # Force A (winner) higher than B (loser) on score.
        with self.store._conn() as conn:
            conn.execute("UPDATE memories SET score=? WHERE id=?", (0.95, self.a.id))
            conn.execute("UPDATE memories SET score=? WHERE id=?", (0.20, self.b.id))
            conn.execute("UPDATE memories SET importance=? WHERE id=?", (0.55, self.a.id))
            conn.execute("UPDATE memories SET importance=? WHERE id=?", (0.40, self.b.id))
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=merge")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            # Action reports the new semantics, not the old 'deleted' array.
            self.assertEqual(data["action"], "merge")
            self.assertTrue(data.get("merged"))
            self.assertEqual(data["winner"], self.a.id)
            self.assertEqual(data["loser"], self.b.id)
            self.assertTrue(data["appended"])

        # Winner still exists, now with appended loser's text.
        winner = self.store.get_memory(self.a.id)
        self.assertIsNotNone(winner)
        a_text = winner.text
        # The winner's original text + the separator + loser's text.
        self.assertIn(self.b.text, a_text)
        self.assertIn("---", a_text)
        # Loser is gone.
        self.assertIsNone(self.store.get_memory(self.b.id))
        # Importance / score are the max of the pair.
        self.assertAlmostEqual(winner.importance, 0.55, places=4)
        self.assertAlmostEqual(winner.score, 0.95, places=4)
        # The pair is now hidden so the pulse will not resurface it.
        self.assertTrue(self.store.is_contradiction_ignored(self.a.id, self.b.id))

    def test_resolve_endpoint_merge_skips_append_when_subset(self):
        """If the loser's text is already contained in the winner's, the
        merge must not duplicate text; it should still delete the loser."""
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        # Make B's text a strict substring of A's so the merge is a no-op
        # for content even though it still deletes the loser row.
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE memories SET score=?, text=? WHERE id=?",
                (0.9, "User preferences collected so far: " + self.b.text, self.a.id),
            )
            conn.execute(
                "UPDATE memories SET score=? WHERE id=?",
                (0.1, self.b.id),
            )
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=merge")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertTrue(data["merged"])
            self.assertFalse(data["appended"])
        winner = self.store.get_memory(self.a.id)
        self.assertIsNotNone(winner)
        self.assertEqual(winner.text.count("---"), 0)
        self.assertIsNone(self.store.get_memory(self.b.id))

    def test_resolve_endpoint_merge_tie_keeps_a(self):
        """Ties should resolve to side A; new semantics still report winner."""
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        with self.store._conn() as conn:
            conn.execute("UPDATE memories SET score=? WHERE id=?", (0.5, self.a.id))
            conn.execute("UPDATE memories SET score=? WHERE id=?", (0.5, self.b.id))
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post(f"/api/contradictions/resolve?a={self.a.id}&b={self.b.id}&action=merge")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["winner"], self.a.id)
            self.assertEqual(data["loser"], self.b.id)

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
            # force=true bypasses the per-ISO-week cache so the test
            # actually exercises the no-provider path; otherwise a stale
            # cache hit would mask the error classification.
            r = c.get("/api/weekly-report?days=7&force=true")
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

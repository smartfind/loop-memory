"""HTTP route tests for the 0.4.9 cycle additions (audit 2026-09-20).

Two new endpoints / params:
  - ``POST /api/export/okf``             -- OKF v0.2 bundle export
  - ``GET  /api/recall?as_of=...``       -- bi-temporal recall
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
    tmp = Path(tempfile.mkdtemp(prefix="loop-routes-0920-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class ExportOkfRouteTests(unittest.TestCase):
    """Audit 2026-09-20: POST /api/export/okf."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_okf_requires_out_dir(self) -> None:
        r = self.client.post("/api/export/okf", json={})
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_export_okf_writes_bundle(self) -> None:
        self.store.upsert_wiki_page(
            slug="alpha", title="Alpha",
            body="body", summary="summary", tags=["t"],
        )
        with tempfile.TemporaryDirectory() as out:
            r = self.client.post(
                "/api/export/okf",
                json={"out_dir": out},
            )
            self.assertEqual(r.status_code, 200, msg=r.text)
            body = r.json()
            self.assertEqual(body["page_count"], 1)
            self.assertIn("alpha", body["pages"])
            self.assertTrue((Path(out) / "pages" / "alpha.md").exists())

    def test_export_okf_scope_filter(self) -> None:
        self.store.upsert_wiki_page(slug="p1", title="P1", body="b", scope="global")
        self.store.upsert_wiki_page(slug="p2", title="P2", body="b", scope="codex")
        with tempfile.TemporaryDirectory() as out:
            r = self.client.post(
                "/api/export/okf",
                json={"out_dir": out, "scope": "global"},
            )
            self.assertEqual(r.status_code, 200, msg=r.text)
            body = r.json()
            self.assertEqual(body["page_count"], 1)
            self.assertEqual(body["pages"], ["p1"])


class RecallAsOfRouteTests(unittest.TestCase):
    """Audit 2026-09-20: GET /api/recall?as_of=…"""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_as_of_iso(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        r = self.client.get(
            "/api/recall",
            params={"query": "postgres",
                    "as_of": "2286-01-01T00:00:00Z"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["mode"], "as_of")
        self.assertTrue(body["memories"], msg=body)

    def test_recall_as_of_invalid_string_returns_400(self) -> None:
        r = self.client.get(
            "/api/recall",
            params={"query": "anything", "as_of": "not-a-date"},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)

    def test_recall_without_as_of_still_works(self) -> None:
        """The pre-0.4.9 contract: no ``as_of`` means legacy /
        hybrid recall. We must preserve byte-for-byte behaviour."""
        self.store.upsert_memory(
            kind="fact", text="postgres tuning",
            importance=0.5, agent_id="bot", user_id="u1",
        )
        r = self.client.get("/api/recall", params={"query": "postgres"})
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        # Mode should NOT be 'as_of' when no param was passed.
        self.assertNotEqual(body["mode"], "as_of")
        self.assertIn(body["mode"], ("hybrid", "legacy"))


if __name__ == "__main__":
    unittest.main()

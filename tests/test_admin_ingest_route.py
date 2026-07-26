"""Regression test for the H1 audit finding.

``POST /api/admin/ingest`` used to map to ``ingest_config_get``
(returning config JSON) because an orphan @app.post decorator was
sitting on top of it, while the real ``def ingest(...)`` had no
decorator.  This test pins the contract: POST now triggers ingest.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-ingest-route-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class AdminIngestRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_ingest_does_not_return_config(self) -> None:
        """POST must NOT return the cadence config (the regression)."""
        with mock.patch(
            "loop_memory.ingest.loader.get_loader"
        ) as get_loader, mock.patch(
            "loop_memory.ingest.loader.default_paths",
            return_value={"codex": "/tmp"},
        ), mock.patch(
            "loop_memory.backends.embedding.HashingEmbedder"
        ):
            fake_loader = mock.MagicMock()
            fake_loader.discover.return_value = []
            get_loader.return_value = fake_loader

            r = self.client.post("/api/admin/ingest?source=codex")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("idle_seconds", body)
        self.assertNotIn("poll_seconds", body)
        self.assertIn("files", body)
        self.assertEqual(body["files"], 0)

    def test_get_ingest_config_still_works(self) -> None:
        r = self.client.get("/api/admin/ingest/config")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("idle_seconds", body)
        self.assertIn("poll_seconds", body)

    def test_post_and_get_have_different_handlers(self) -> None:
        post_handlers = [
            r.endpoint for r in self.app.routes
            if hasattr(r, "path") and r.path == "/api/admin/ingest"
            and "POST" in getattr(r, "methods", set())
        ]
        get_cfg_handlers = [
            r.endpoint for r in self.app.routes
            if hasattr(r, "path") and r.path == "/api/admin/ingest/config"
            and "GET" in getattr(r, "methods", set())
        ]
        self.assertEqual(len(post_handlers), 1)
        self.assertEqual(len(get_cfg_handlers), 1)
        self.assertIsNot(post_handlers[0], get_cfg_handlers[0])


if __name__ == "__main__":
    unittest.main()

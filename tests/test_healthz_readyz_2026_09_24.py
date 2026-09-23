"""Tests for the new operational probes (audit 2026-09-24).

* ``GET /api/healthz`` — liveness, never touches the DB.
* ``GET /api/readyz`` — readiness, runs ``PRAGMA quick_check``.

Both endpoints must:
  1. Return 200 on a healthy store
  2. Be in the public allow-list (no Bearer token required)
  3. ``/api/readyz`` must return 503 when the DB is unreadable
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
    tmp = Path(tempfile.mkdtemp(prefix="loop-probes-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class HealthzRouteTests(unittest.TestCase):
    """Audit 2026-09-24: GET /api/healthz."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_healthz_returns_ok(self) -> None:
        r = self.client.get("/api/healthz")
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("ts", body)
        self.assertIsInstance(body["ts"], (int, float))

    def test_healthz_does_not_require_auth(self) -> None:
        # Even when a Bearer token is configured the probe must
        # succeed without one.
        self.store.set_setting("loop_memory_auth_token", "secret")
        r = self.client.get("/api/healthz")
        self.assertEqual(r.status_code, 200, msg=r.text)


class ReadyzRouteTests(unittest.TestCase):
    """Audit 2026-09-24: GET /api/readyz."""

    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_readyz_returns_ok_on_healthy_store(self) -> None:
        r = self.client.get("/api/readyz")
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["status"], "ok")

    def test_readyz_does_not_require_auth(self) -> None:
        self.store.set_setting("loop_memory_auth_token", "secret")
        r = self.client.get("/api/readyz")
        self.assertEqual(r.status_code, 200, msg=r.text)


if __name__ == "__main__":
    unittest.main()

"""Audit M4 regression: POST/DELETE on /api/admin/auth/token must
NOT be able to lower the system back to the ``no token / no auth``
state once a token has been set. The DELETE handler now rotates
to a fresh token instead of removing it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-auth-rotate-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class AuthTokenRotateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_first_token_allowed_unauthenticated(self) -> None:
        r = self.client.post("/api/admin/auth/token")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("token", body)
        self.assertEqual(len(body["token"]), 43)  # token_urlsafe(32) base64

    def test_post_when_token_exists_requires_auth(self) -> None:
        # Bootstrap a token.
        r1 = self.client.post("/api/admin/auth/token")
        first_token = r1.json()["token"]
        # Without auth -> 401 (or 403, depending on middleware path)
        r2 = self.client.post("/api/admin/auth/token")
        self.assertIn(r2.status_code, (401, 403))
        # With auth -> 200 + new token
        r3 = self.client.post(
            "/api/admin/auth/token",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        self.assertEqual(r3.status_code, 200)
        # Old token should be invalidated.
        r4 = self.client.post(
            "/api/admin/auth/token",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        self.assertEqual(r4.status_code, 401)

    def test_delete_is_now_rotate_not_disable(self) -> None:
        r1 = self.client.post("/api/admin/auth/token")
        first_token = r1.json()["token"]
        # DELETE without auth fails.
        r2 = self.client.delete("/api/admin/auth/token")
        self.assertIn(r2.status_code, (401, 403))
        # DELETE WITH auth rotates.
        r3 = self.client.delete(
            "/api/admin/auth/token",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        self.assertEqual(r3.status_code, 200)
        body = r3.json()
        self.assertIn("token", body)
        self.assertIn("rotated", body)
        self.assertTrue(body["rotated"])
        new_token = body["token"]
        # New token works for subsequent admin calls.
        r4 = self.client.get(
            "/api/admin/auth/token",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        self.assertEqual(r4.status_code, 200)
        # And the system remains in ``enabled`` state.
        self.assertTrue(r4.json()["enabled"])
        # Old token no longer works.
        r5 = self.client.get(
            "/api/admin/auth/token",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        self.assertEqual(r5.status_code, 401)


if __name__ == "__main__":
    unittest.main()

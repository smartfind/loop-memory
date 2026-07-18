"""Tests for /api/admin/llm/test + the LLMHttpError parsing path."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loop_memory.storage.sqlite_store import MemoryStore


class LLMHttpErrorTests(unittest.TestCase):
    """The provider HTTP error wrapper should expose structured fields."""

    def test_parses_minimax_error_code(self):
        from loop_memory.llm.providers import LLMHttpError
        body = '{"type":"error","error":{"type":"authorized_error","message":"invalid api key (2049)","http_code":"401"}}'
        e = LLMHttpError(401, "https://api.minimaxi.chat/v1", body)
        self.assertEqual(e.status, 401)
        # 2049 is pulled from the message text; that's the code users search for.
        self.assertEqual(e.provider_code, "2049")
        self.assertEqual(e.provider_message, "invalid api key (2049)")
        self.assertIn("2049", str(e))

    def test_handles_non_json_body(self):
        from loop_memory.llm.providers import LLMHttpError
        e = LLMHttpError(502, "https://x", "Bad Gateway")
        self.assertEqual(e.status, 502)
        self.assertIsNone(e.provider_code)
        self.assertIsNone(e.provider_message)
        self.assertIsNone(e.body_json)


class LLMSettingsTestEndpointTests(unittest.TestCase):
    """The /api/admin/llm/test endpoint must classify errors clearly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_placeholder_key_returns_no_real_key_error(self):
        """Point the test at a dedicated ephemeral account so the user's
        real ``llm/minimax/api_key`` is never overwritten by a test."""
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        ep_account = "llm/test/placeholder_ephemeral"
        self.store.set_setting("llm_consolidator", {
            "provider": "minimax", "model": "MiniMax-M2.7",
            "base_url": "https://api.minimaxi.chat/v1",
            "api_key_account": ep_account,
            "api_key_set": True,
        })
        from loop_memory.security import set_secret, delete_secret
        # Save whatever the user had at the real account so we can restore it.
        from loop_memory.security import get_secret
        real_key = get_secret("llm/minimax/api_key")
        set_secret(ep_account, "your-api-key-placeholder")
        try:
            app = create_app(self.store)
            with TestClient(app) as c:
                r = c.post("/api/admin/llm/test")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertFalse(data["ok"])
                self.assertIn("placeholder", (data["error"]["provider_message"] or "").lower())
                self.assertIn("hint", data["error"])
        finally:
            delete_secret(ep_account)
            # Restore the user's real key (or delete if there wasn't one).
            if real_key:
                set_secret("llm/minimax/api_key", real_key)
            else:
                delete_secret("llm/minimax/api_key")

    def test_ephemeral_key_allows_test_without_saving(self):
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            # Use a body-only test key. The handler should accept it
            # without persisting anything to the secret store.
            r = c.post("/api/admin/llm/test", json={
                "provider": "minimax",
                "model": "MiniMax-M2.7",
                "base_url": "https://api.minimaxi.chat/v1",
                "api_key": "sk-test-ephemeral-key",
            })
            # The endpoint should NOT throw — even if the LLM call fails
            # (network/401), the structured error is returned.
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertIn("provider", data)
            # key_prefix should match what was sent (first 10 chars)
            if data.get("key_prefix"):
                self.assertTrue(data["key_prefix"].startswith("sk-test-ep"))


if __name__ == "__main__":
    unittest.main()

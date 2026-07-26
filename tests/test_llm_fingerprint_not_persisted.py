"""Audit M3 regression: api_key_fingerprint / api_key_saved_at
must NOT be persisted to the settings table (they're
response-only). Persisting exposes a 36-bit brute-force target
for an attacker with DB read access."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.security import account_for, set_secret
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-fp-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class LlmFingerprintNotPersistedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw_db_settings(self) -> dict:
        """Read directly from the SQLite settings table (bypassing
        the store's get_setting helper, which might filter fields)."""
        conn = sqlite3.connect(str(self.tmp / "db.sqlite"))
        try:
            row = conn.execute(
                "SELECT v FROM settings WHERE k = ?",
                ("llm_consolidator",),
            ).fetchone()
            if not row:
                return {}
            return json.loads(row[0])
        finally:
            conn.close()

    def test_put_with_key_does_not_store_fingerprint_in_db(self) -> None:
        # Seed a fake provider/key through the secret backend so the
        # PUT can run end-to-end without opening macOS Keychain during
        # tests. Using the file backend via set_secret() is fine.
        fake_key = "sk-test-" + "x" * 40
        with mock.patch(
            "loop_memory.security.secrets._pick_backend",
            return_value=mock.MagicMock(),
        ):
            # Simpler: just call set_secret directly, accepting the
            # default backend choice. The secrets backend is irrelevant
            # to this test -- we're checking what lands in the
            # *settings* table.
            from loop_memory.security.secrets import _pick_backend
            account = account_for("openai", "api_key")
            with mock.patch.dict("os.environ", {"LOOP_MEMORY_SECRET_BACKEND": "file"}):
                set_secret(account, fake_key)
            r = self.client.put(
                "/api/admin/llm/config",
                json={"provider": "openai", "model": "gpt-x", "api_key": fake_key},
            )
            self.assertEqual(r.status_code, 200, r.text)
            # The *response* may include fingerprint (still useful for UI).
            # What we forbid is the DB storing it.
            body = r.json()
            # Fingerprint may or may not be present in response;
            # but the saved config snapshot passed back must not have
            # the *persisted* form (frontend reads API again to fetch).
            # The thing we want is that `get_setting("llm_consolidator")`
            # does NOT carry api_key_fingerprint / api_key_saved_at.
            saved = self._raw_db_settings()
            self.assertNotIn("api_key_fingerprint", saved,
                "fingerprint must not be persisted; got db row: " + json.dumps(saved))
            self.assertNotIn("api_key_saved_at", saved,
                "saved_at must not be persisted; got db row: " + json.dumps(saved))
            # Sanity: the api_key itself is also not stored.
            self.assertNotIn("api_key", saved)


if __name__ == "__main__":
    unittest.main()

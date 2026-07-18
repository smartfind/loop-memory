"""Tests for the secret-store abstraction."""

from __future__ import annotations

import os
import tempfile
import unittest

from loop_memory.security import (
    account_for,
    delete_secret,
    get_secret,
    has_secret,
    set_secret,
)


class SecretStoreRoundtrip(unittest.TestCase):
    def test_set_get_delete(self):
        acct = account_for("test_provider")
        try:
            set_secret(acct, "secret-xyz")
            self.assertTrue(has_secret(acct))
            self.assertEqual(get_secret(acct), "secret-xyz")
        finally:
            delete_secret(acct)
        self.assertFalse(has_secret(acct))
        self.assertIsNone(get_secret(acct))

    def test_empty_value_deletes(self):
        acct = account_for("test_provider_empty")
        set_secret(acct, "value")
        self.assertTrue(has_secret(acct))
        set_secret(acct, "")
        self.assertFalse(has_secret(acct))


class FileFallback(unittest.TestCase):
    def test_file_fallback_roundtrip(self):
        from loop_memory.security.secrets import FileSecretStore
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LOOP_MEMORY_DATA_DIR"] = tmp
            s = FileSecretStore()
            self.assertTrue(s.path.exists())
            self.assertEqual(s.get("x"), None)
            s.set("x", "hello")
            self.assertEqual(s.get("x"), "hello")
            self.assertTrue(s.delete("x"))
            self.assertIsNone(s.get("x"))
            # 0o600 perm check
            mode = s.path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


class AccountNames(unittest.TestCase):
    def test_stable(self):
        self.assertEqual(account_for("openai"), "llm/openai/api_key")
        self.assertEqual(account_for("anthropic", "access_token"),
                         "llm/anthropic/access_token")


if __name__ == "__main__":
    unittest.main()

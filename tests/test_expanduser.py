import unittest

from loop_memory.storage.sqlite_store import MemoryStore


class ExpandUserTests(unittest.TestCase):
    def test_tilde_path_is_expanded(self):
        """MemoryStore should expand ``~`` to the user's home so callers can
        use ``~/.loop_memory/loop_memory.db`` regardless of cwd (otherwise
        sqlite3 silently creates a stray db in cwd)."""
        store = MemoryStore("~/loop_memory_test_expanduser.db")
        self.assertFalse(str(store.path).startswith("~"))
        self.assertTrue(str(store.path).endswith("loop_memory_test_expanduser.db"))
        self.assertTrue(store.path.exists())
        # write + read round trip
        m = store.upsert_memory(kind="fact", text="tilde works", importance=0.5)
        self.assertEqual(store.get_memory(m.id).text, "tilde works")
        # cleanup
        store.path.unlink(missing_ok=True)

if __name__ == "__main__":
    unittest.main()

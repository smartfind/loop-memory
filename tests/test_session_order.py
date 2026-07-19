"""Regression test: list_sessions() orders by last-activity time, not
started_at, so a long-running session that started days ago still rises
to the top once it receives new turns."""
import os
import tempfile
import time
import unittest

from loop_memory.storage.sqlite_store import MemoryStore


class ListSessionsActivityOrderTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        try:
            os.unlink(self.db_path)
        except Exception:
            pass

    def test_active_session_rises_to_top_over_newer_short_lived(self):
        # 1) Insert an "old" session that started 9 days ago and only
        #    just got a new turn.
        now = time.time()
        old_started = now - 9 * 86400
        old_sid = self.store.upsert_session(
            source="codex",
            external_id="old-conv",
            title="long-running conversation",
            started_at=old_started,
            ended_at=now,                   # last activity = now
            message_count=3165,
            metadata={"kind": "summary"},
        )

        # 2) Insert several "fresh" sessions that started recently but
        #    haven't been touched for hours. These should sort BELOW
        #    the active long-running one.
        for i in range(3):
            self.store.upsert_session(
                source="openclaw",
                external_id=f"cron-{i}",
                title=f"cron report #{i}",
                started_at=now - 3600,         # 1h ago
                ended_at=now - 1800,           # last activity 30m ago
                message_count=50,
                metadata={"kind": "summary"},
            )

        listed = self.store.list_sessions(limit=10)
        ids = [s.id for s in listed]

        # The long-running active session must be at position 0
        self.assertEqual(
            ids[0], old_sid.id,
            "active session with stale started_at was buried; "
            f"got order: {[(s.external_id, s.started_at, s.ended_at) for s in listed]}",
        )

        # And the three cron sessions must come AFTER it
        cron_ids = {s.id for s in listed if s.external_id and s.external_id.startswith("cron-")}
        self.assertEqual(len(cron_ids), 3)
        for cron_id in cron_ids:
            self.assertGreater(ids.index(cron_id), 0)


if __name__ == "__main__":
    unittest.main()

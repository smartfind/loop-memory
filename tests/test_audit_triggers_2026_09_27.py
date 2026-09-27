"""Regression tests for DB-level ``audit_log`` enforcement
(audit 2026-09-27, memory-eternal v0.4.3 ``enforceAudit()`` pattern).

The contract under test: every INSERT / UPDATE / DELETE against
``memories`` / ``wiki_pages`` / ``relations`` / ``consolidation_runs``
MUST append a row to ``audit_log`` in the same transaction — even
when the writer opens the SQLite file with the stdlib ``sqlite3``
module directly and bypasses the Python wrapper.

This is the DB-level invariant: a Python-level audit write is
cheap and carries caller context (``loop-memory CLI``, ``serve``),
but the DB trigger is the floor. The two paths coexist.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_audit_trig_"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class AuditTriggersInstalledTests(unittest.TestCase):
    """Schema-level invariants: the triggers exist + audit_log exists."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_twelve_triggers_installed(self) -> None:
        # 3 ops (INSERT/UPDATE/DELETE) x 4 tables (memories,
        # wiki_pages, relations, consolidation_runs) = 12.
        names = [
            "trg_memories_ai", "trg_memories_au", "trg_memories_ad",
            "trg_wiki_pages_ai", "trg_wiki_pages_au", "trg_wiki_pages_ad",
            "trg_relations_ai", "trg_relations_au", "trg_relations_ad",
            "trg_consolidation_runs_ai", "trg_consolidation_runs_au",
            "trg_consolidation_runs_ad",
        ]
        with self.store._conn() as c:
            present = {
                row["name"]
                for row in c.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'trg_%_a%'"
                ).fetchall()
            }
        for n in names:
            self.assertIn(n, present, msg=f"missing trigger {n}")

    def test_stats_includes_audit_log_count(self) -> None:
        st = self.store.stats()
        self.assertIn("audit_log", st)
        self.assertIn("audit_triggers", st)
        self.assertEqual(st["audit_log"], 0)
        self.assertEqual(st["audit_triggers"], 12)

    def test_install_idempotent_on_reopen(self) -> None:
        # Reopening the same SQLite path must not duplicate or fail
        # (CREATE TRIGGER IF NOT EXISTS is a no-op the second time).
        s2 = MemoryStore(str(self.store.path))
        st = s2.stats()
        self.assertEqual(st["audit_triggers"], 12)


class AuditTriggersDirectSqliteWritesTests(unittest.TestCase):
    """A raw ``sqlite3`` writer that bypasses the Python wrapper
    MUST still produce ``audit_log`` rows.
    """

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        # Open a SECOND connection that the store does NOT see, so the
        # raw writes are guaranteed not to come from the Python wrapper.
        self.raw = sqlite3.connect(str(self.store.path))
        self.raw.execute("PRAGMA foreign_keys=ON")

    def tearDown(self) -> None:
        self.raw.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_direct_memories_insert_audited(self) -> None:
        self.raw.execute(
            "INSERT INTO memories (id, kind, text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("m1", "fact", "raw", 1.0, 1.0),
        )
        self.raw.commit()
        rows = self.store.list_audit_log(entity="memories", op="INSERT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "db_trigger")
        self.assertEqual(rows[0]["entity_id"], "m1")

    def test_direct_memories_update_audited(self) -> None:
        self.raw.execute(
            "INSERT INTO memories (id, kind, text, created_at, updated_at) "
            "VALUES ('m1','fact','v1',1.0,1.0)"
        )
        self.raw.execute(
            "UPDATE memories SET text='v2' WHERE id='m1'"
        )
        self.raw.commit()
        rows = self.store.list_audit_log(entity="memories", op="UPDATE")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_id"], "m1")

    def test_direct_memories_delete_audited(self) -> None:
        self.raw.execute(
            "INSERT INTO memories (id, kind, text, created_at, updated_at) "
            "VALUES ('m1','fact','v1',1.0,1.0)"
        )
        self.raw.execute("DELETE FROM memories WHERE id='m1'")
        self.raw.commit()
        rows = self.store.list_audit_log(entity="memories", op="DELETE")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_id"], "m1")

    def test_direct_wiki_pages_insert_audited(self) -> None:
        # The wiki_pages table has a non-null constraint on slug
        # and we need a rowid-derived id; we let SQLite pick.
        self.raw.execute(
            "INSERT INTO wiki_pages (slug, title, body, scope, "
            "version, created_at, updated_at) "
            "VALUES ('raw-slug','Raw','raw body','global',1,1.0,1.0)"
        )
        self.raw.commit()
        rows = self.store.list_audit_log(entity="wiki_pages", op="INSERT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "db_trigger")

    def test_direct_relations_insert_audited(self) -> None:
        # relations need entities src + dst; we set up minimal ones.
        self.raw.execute(
            "INSERT INTO entities (id, name, kind, weight, created_at, "
            "updated_at) VALUES ('e1','A','concept',0.5,1.0,1.0)"
        )
        self.raw.execute(
            "INSERT INTO entities (id, name, kind, weight, created_at, "
            "updated_at) VALUES ('e2','B','concept',0.5,1.0,1.0)"
        )
        self.raw.execute(
            "INSERT INTO relations (id, src, dst, kind, weight, "
            "evidence_ids, created_at) "
            "VALUES ('r1','e1','e2','relates_to',0.5,'[]',1.0)"
        )
        self.raw.commit()
        rows = self.store.list_audit_log(entity="relations", op="INSERT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_id"], "r1")

    def test_list_audit_log_filters(self) -> None:
        # Mixed writes; filter by op='INSERT' and entity='memories'.
        self.raw.execute(
            "INSERT INTO memories (id, kind, text, created_at, updated_at) "
            "VALUES ('a','fact','x',1.0,1.0)"
        )
        self.raw.execute(
            "INSERT INTO entities (id, name, kind, weight, created_at, "
            "updated_at) VALUES ('e1','A','concept',0.5,1.0,1.0)"
        )
        self.raw.execute(
            "UPDATE memories SET text='y' WHERE id='a'"
        )
        self.raw.commit()
        rows = self.store.list_audit_log(entity="memories", op="INSERT")
        self.assertEqual(len(rows), 1)
        rows = self.store.list_audit_log(op="UPDATE")
        self.assertEqual(len(rows), 1)
        # entities are not under audit-trigger coverage this cycle
        # (deliberate scope cap — only the four mutation-heavy tables
        # get triggers). Total = 1 memories INSERT + 1 memories UPDATE.
        rows = self.store.list_audit_log()
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()

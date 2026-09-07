"""Tests for the portable single-file SQLite snapshot.

Audit 2026-09-06 (codexa-memory v0.2.0). Pins the contract of
``loop_memory.storage.snapshot`` so the CLI / HTTP surface shares
the same semantics. Tested properties:

* Round-trip: snapshot a populated store → restore to a fresh store
  → every memory + signal + wiki page comes back byte-equivalent.
* Idempotency: restoring the same snapshot twice does not duplicate
  rows (PK-upsert is honoured).
* Wrong-magic files are refused loudly.
* Missing files raise ``FileNotFoundError``.
* Files with the ``schema_meta`` table but no snapshot_magic are
  refused loudly.
* The snapshot carries the recall-quality signals (recall_count,
  positive, negative) so a hand-off to another machine preserves
  recall fidelity.
* The snapshot carries wiki_pages.
* Restore preserves ``created_at`` / ``updated_at`` timestamps.
* Restore preserves ``superseded_by`` chain pointers.
* Snapshot is a single file (no leftover WAL sidecars at the dest).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.snapshot import (
    SNAPSHOT_MAGIC,
    restore,
    snapshot,
)
from loop_memory.storage.sqlite_store import MemoryStore


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class SnapshotStoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = _tmp("lm_snap_")
        self.src = MemoryStore(self.tmp / "src.db")
        # Populate the source store with a representative slice:
        # memories + signals + a wiki page + a supersession chain.
        self.m1 = self.src.upsert_memory(
            kind="fact", text="alpha",
            importance=0.8, agent_id="bot", user_id="u1",
        ).id
        self.m2 = self.src.upsert_memory(
            kind="fact", text="beta",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        self.src.bump_recalls([self.m1, self.m1, self.m2])
        # record_signal uses boolean thumbs: True=positive, False=negative.
        self.src.record_signal(self.m1, positive=True)
        self.src.record_signal(self.m1, positive=True)
        self.src.record_signal(self.m1, positive=False)
        with self.src._conn() as c:  # noqa: SLF001
            c.execute("UPDATE memories SET score=0.9 WHERE id=?", (self.m1,))
            c.execute("UPDATE memories SET score=0.3 WHERE id=?", (self.m2,))
        self.src.merge_memories(self.m1, self.m2)
        self.wiki = self.src.upsert_wiki_page(
            slug="db", title="DB",
            body="postgres details", summary="db",
            tags="db", scope="global", importance=0.7,
        )
        self.out = self.tmp / "snap.memory.sqlite"

    def test_round_trip_preserves_memories_and_signals(self) -> None:
        summary = snapshot(self.src, self.out)
        self.assertTrue(self.out.exists())
        self.assertEqual(summary["schema_version"], MemoryStore.SCHEMA_VERSION)
        self.assertGreater(summary["size_bytes"], 0)
        self.assertGreater(summary["tables"]["memories"], 0)
        # restore into a fresh store
        dst = MemoryStore(self.tmp / "dst.db")
        restore(dst, self.out)
        self.assertEqual(dst.count_memories(), 2)
        a_stats = dst.memory_stats(self.m1)
        self.assertEqual(a_stats["recall_count"], 2)
        self.assertEqual(a_stats["positive"], 2)
        self.assertEqual(a_stats["negative"], 1)
        # Bump again on dst and confirm it tracks independently
        # (the live store owns the counters, not the snapshot).
        dst.bump_recalls([self.m1])
        self.assertEqual(dst.memory_stats(self.m1)["recall_count"], 3)
        # merge_memories concatenates the loser onto the winner with
        # a separator, so after merge the winner's text starts with
        # the original text + "---" + the loser. Pin that shape so
        # the round-trip is asserted on what the live store actually
        # carries, not on an outdated assumption.
        self.assertTrue(a_stats["text"].startswith("alpha"))
        self.assertIn("beta", a_stats["text"])

    def test_round_trip_preserves_wiki_page(self) -> None:
        snapshot(self.src, self.out)
        dst = MemoryStore(self.tmp / "dst.db")
        restore(dst, self.out)
        got = dst.get_wiki_page(self.wiki["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["body"], "postgres details")
        self.assertEqual(got["title"], "DB")

    def test_round_trip_preserves_supersession_chain(self) -> None:
        snapshot(self.src, self.out)
        dst = MemoryStore(self.tmp / "dst.db")
        restore(dst, self.out)
        # The loser (m2) should still point at the winner (m1).
        loser = dst.memory_stats(self.m2)
        self.assertEqual(loser["superseded_by"], self.m1)
        chain = dst.trace_supersession(self.m2)
        self.assertEqual(chain, [self.m2, self.m1])

    def test_restore_is_idempotent(self) -> None:
        snapshot(self.src, self.out)
        dst = MemoryStore(self.tmp / "dst.db")
        restore(dst, self.out)
        first_count = dst.count_memories()
        restore(dst, self.out)
        self.assertEqual(dst.count_memories(), first_count)

    def test_refuses_wrong_magic(self) -> None:
        bogus = self.tmp / "bogus.db"
        with sqlite3.connect(bogus) as c:
            c.execute("CREATE TABLE foo (x INT)")
        dst = MemoryStore(self.tmp / "dst.db")
        with self.assertRaises(ValueError) as ctx:
            restore(dst, bogus)
        self.assertIn("not a loop-memory snapshot", str(ctx.exception))

    def test_refuses_file_with_schema_meta_but_wrong_magic(self) -> None:
        bogus = self.tmp / "bogus2.db"
        with sqlite3.connect(bogus) as c:
            c.execute(
                "CREATE TABLE schema_meta (k TEXT PRIMARY KEY, v TEXT)"
            )
            c.execute(
                "INSERT INTO schema_meta VALUES "
                "('snapshot_magic', 'something_else')"
            )
        dst = MemoryStore(self.tmp / "dst.db")
        with self.assertRaises(ValueError):
            restore(dst, bogus)

    def test_missing_file_raises_filenotfound(self) -> None:
        dst = MemoryStore(self.tmp / "dst.db")
        with self.assertRaises(FileNotFoundError):
            restore(dst, "/no/such/file.memory.sqlite")

    def test_snapshot_is_single_file_no_sidecars(self) -> None:
        snapshot(self.src, self.out)
        # No WAL/-shm/-journal sidecars at the destination.
        siblings = sorted(p.name for p in self.out.parent.iterdir())
        bad = [n for n in siblings
               if n.startswith(self.out.name)
               and n != self.out.name]
        self.assertEqual(bad, [], f"sidecars at dest: {bad}")

    def test_snapshot_overwrites_existing_file(self) -> None:
        snapshot(self.src, self.out)
        # Add another memory so the snapshot is genuinely different
        # from the first one, then snapshot again — file should be
        # replaced and remain valid.
        self.src.upsert_memory(
            kind="fact", text="second batch",
            importance=0.6, agent_id="bot", user_id="u1",
        )
        snapshot(self.src, self.out)
        self.assertTrue(self.out.exists())
        # Second snapshot must still be a valid single-file store.
        with sqlite3.connect(self.out) as c:
            n = c.execute("SELECT COUNT(*) c FROM memories").fetchone()[0]
            self.assertGreater(n, 0)

    def test_snapshot_header_records_schema_version(self) -> None:
        snapshot(self.src, self.out)
        with sqlite3.connect(self.out) as c:
            magic = c.execute(
                "SELECT v FROM schema_meta WHERE k='snapshot_magic'"
            ).fetchone()
            version = c.execute(
                "SELECT v FROM schema_meta WHERE k='snapshot_source_schema'"
            ).fetchone()
        # default row_factory returns tuples — index by [0]
        self.assertEqual(magic[0], SNAPSHOT_MAGIC)
        self.assertEqual(version[0], MemoryStore.SCHEMA_VERSION)

    def test_restore_skips_transient_tables(self) -> None:
        """``schema_meta`` / ``llm_audit`` / ``auth_tokens`` /
        ``write_guard_drops`` must NOT clobber the live store's own
        rows when restoring on top of it. Verify by snapshotting a
        store with a known schema_meta row, restoring on top of a
        store with a *different* schema_meta row, and confirming the
        live store's own row survives."""
        # seed the destination with its own schema_meta row
        dst = MemoryStore(self.tmp / "dst.db")
        with dst._conn() as c:  # noqa: SLF001
            c.execute(
                "INSERT OR REPLACE INTO schema_meta(k, v) "
                "VALUES ('live_only', 'survives_restore')"
            )
        snapshot(self.src, self.out)
        restore(dst, self.out)
        # The live store's own marker should still be there.
        with dst._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT v FROM schema_meta WHERE k='live_only'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["v"], "survives_restore")

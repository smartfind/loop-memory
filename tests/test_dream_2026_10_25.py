"""The dream pass (audit 2026-10-25, loop-memory 0.4.16).

``loop_memory.jobs.dream`` is the sleep-time consolidation pass. Its
contract:

* **never deletes** -- a stale row is soft-expired (``invalid_at``),
  so the history survives and ``recall_as_of`` can still answer
  "what did we believe back then?";
* **utility-aware, not age-only** -- a row that is past its deadline
  but is recalled often is *protected*;
* **dry run by default** -- no memory *data* is changed unless
  ``apply=True`` (the only write a dry run performs is the
  ``cognitive_audit`` ``suggest`` row that records the plan);
* **audited** -- every decision lands in ``cognitive_audit``.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from loop_memory.jobs.dream import Dreamer, dream, memory_utility
from loop_memory.storage.sqlite_store import MemoryStore


class MemoryUtilityTests(unittest.TestCase):
    """The pure utility blend."""

    def test_bounded_to_unit_range(self) -> None:
        now = time.time()
        for imp in (0.0, 0.5, 1.0):
            for count in (0, 5, 500):
                u = memory_utility(
                    importance=imp, recall_count=count,
                    created_at=now, now=now,
                )
                self.assertGreaterEqual(u, 0.0)
                self.assertLessEqual(u, 1.0)

    def test_importance_raises_utility(self) -> None:
        now = time.time()
        low = memory_utility(importance=0.1, recall_count=0,
                             created_at=now, now=now)
        high = memory_utility(importance=0.9, recall_count=0,
                              created_at=now, now=now)
        self.assertGreater(high, low)

    def test_usage_raises_utility(self) -> None:
        now = time.time()
        unused = memory_utility(importance=0.5, recall_count=0,
                                created_at=now, now=now)
        used = memory_utility(importance=0.5, recall_count=8,
                              created_at=now, now=now)
        self.assertGreater(used, unused)

    def test_recency_raises_utility(self) -> None:
        now = time.time()
        fresh = memory_utility(importance=0.5, recall_count=0,
                               created_at=now, now=now)
        stale = memory_utility(importance=0.5, recall_count=0,
                               created_at=now - 86400 * 30, now=now)
        self.assertGreater(fresh, stale)


class DreamPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "dream.db"))
        self.now = time.time()
        self.old = self.now - 86400 * 200

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- dry run ------------------------------------------------------

    def test_dry_run_writes_nothing(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        report = dream(self.store, apply=False)
        self.assertEqual(report.expired, 1)
        self.assertFalse(report.applied)
        self.assertIsNone(self.store.get_memory(m.id).invalid_at)

    def test_dry_run_does_not_touch_the_score_column(self) -> None:
        """Regression (audit 2026-10-25, fix): the decay stage used to
        call ``rescore_all`` unconditionally, so a dry run still
        rewrote ``memories.score`` for every row -- breaking the
        "nothing is written unless apply=True" contract."""
        m = self.store.upsert_memory(kind="fact", text="a fact worth scoring")
        with self.store._conn() as c:
            c.execute("UPDATE memories SET score=0.01 WHERE id=?", (m.id,))
        before = self.store.get_memory(m.id).score
        self.assertEqual(before, 0.01)

        report = dream(self.store, apply=False)
        self.assertEqual(
            self.store.get_memory(m.id).score, before,
            "a dry run must not rewrite the score column",
        )
        # ...but it still reports the size of the decay job.
        self.assertGreaterEqual(report.rescored, 1)

    def test_apply_does_rescore(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="a fact worth scoring")
        with self.store._conn() as c:
            c.execute("UPDATE memories SET score=0.01 WHERE id=?", (m.id,))
        report = dream(self.store, apply=True)
        self.assertGreaterEqual(report.rescored, 1)
        self.assertNotEqual(self.store.get_memory(m.id).score, 0.01)

    def test_rescore_all_dry_run_writes_nothing(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="a fact worth scoring")
        with self.store._conn() as c:
            c.execute("UPDATE memories SET score=0.01 WHERE id=?", (m.id,))
        n = self.store.rescore_all(dry_run=True)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.store.get_memory(m.id).score, 0.01)

    def test_dry_run_reports_the_action(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        report = dream(self.store, apply=False)
        actions = [a for a in report.actions if a.kind == "expire"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "suggest")

    # --- apply --------------------------------------------------------

    def test_apply_soft_expires_instead_of_deleting(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        report = dream(self.store, apply=True)
        self.assertTrue(report.applied)
        self.assertEqual(report.expired, 1)
        row = self.store.get_memory(m.id)
        self.assertIsNotNone(row, "the dream pass must never delete a row")
        self.assertIsNotNone(row.invalid_at)

    def test_expired_row_leaves_the_current_view(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
            importance=0.8,
        )
        dream(self.store, apply=True)
        r = self.store.recall("deploy pipeline", limit=10, latest_only=True)
        self.assertEqual(
            [m["text"] for m in r["memories"]],
            ["the deploy pipeline runs on gitlab ci"],
        )

    def test_history_is_still_reachable_after_expiry(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        dream(self.store, apply=True)
        past = self.store.recall_as_of("deploy pipeline", self.now - 86400,
                                       limit=10)
        self.assertEqual(
            [m["text"] for m in past["memories"]],
            ["the deploy pipeline runs on github actions"],
        )

    # --- utility awareness --------------------------------------------

    def test_frequently_recalled_row_is_protected(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the user deploys on fridays",
            importance=0.5, created_at=self.old, updated_at=self.old,
        )
        for _ in range(8):
            self.store.bump_recalls([m.id])
        report = dream(self.store, apply=True)
        self.assertEqual(report.expired, 0)
        self.assertEqual(report.kept, 1)
        self.assertIsNone(self.store.get_memory(m.id).invalid_at)

    def test_keep_action_is_reported(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the user deploys on fridays",
            importance=0.5, created_at=self.old, updated_at=self.old,
        )
        for _ in range(8):
            self.store.bump_recalls([m.id])
        report = dream(self.store, apply=False)
        kinds = [a.kind for a in report.actions]
        self.assertIn("keep", kinds)

    def test_explicit_ttl_is_honoured(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="temporary note",
            importance=0.1, ttl=60.0,
            created_at=self.now - 3600, updated_at=self.now - 3600,
        )
        report = dream(self.store, apply=True)
        self.assertEqual(report.expired, 1)
        self.assertIsNotNone(self.store.get_memory(m.id).invalid_at)

    def test_fresh_rows_are_untouched(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the user prefers vim keybindings",
            importance=0.6,
        )
        report = dream(self.store, apply=True)
        self.assertEqual(report.expired, 0)
        self.assertIsNone(self.store.get_memory(m.id).invalid_at)

    # --- consolidation -------------------------------------------------

    def test_near_duplicates_are_merged_not_deleted(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request",
            agent_id="c", user_id="u",
        )
        b = self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request!",
            agent_id="c", user_id="u",
        )
        report = dream(self.store, apply=True)
        self.assertEqual(report.superseded, 1)
        self.assertIsNotNone(self.store.get_memory(a.id))
        self.assertIsNotNone(self.store.get_memory(b.id))
        superseded = {row["id"] for row in self.store.list_superseded()}
        self.assertTrue(superseded & {a.id, b.id})

    def test_distinct_facts_are_not_merged(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on staging",
            agent_id="c", user_id="u",
        )
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on production",
            agent_id="c", user_id="u",
        )
        report = dream(self.store, apply=True)
        self.assertEqual(report.superseded, 0)

    def test_different_namespaces_are_not_merged(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request",
            agent_id="c1", user_id="u",
        )
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request!",
            agent_id="c2", user_id="u",
        )
        report = dream(self.store, apply=True)
        self.assertEqual(report.superseded, 0)

    def test_superseded_rows_are_not_re_merged(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request",
            agent_id="c", user_id="u",
        )
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request!",
            agent_id="c", user_id="u",
        )
        dream(self.store, apply=True)
        before = len(self.store.list_superseded())
        dream(self.store, apply=True)
        self.assertEqual(len(self.store.list_superseded()), before)
        self.assertIsNotNone(self.store.get_memory(a.id))

    # --- audit ---------------------------------------------------------

    def test_actions_are_audited(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        dream(self.store, apply=True, record_audit=True)
        rows = self.store.list_audit(kind="expire", limit=10)
        self.assertTrue(rows)

    def test_audit_can_be_disabled(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        dream(self.store, apply=True, record_audit=False)
        rows = self.store.list_audit(kind="expire", limit=10)
        self.assertFalse(rows)

    # --- report shape --------------------------------------------------

    def test_report_to_dict_shape(self) -> None:
        report = dream(self.store, apply=False)
        d = report.to_dict()
        for key in ("actions", "considered", "rescored", "expired", "kept",
                    "superseded", "applied", "elapsed_ms", "stages"):
            self.assertIn(key, d)

    def test_dreamer_class_is_usable_directly(self) -> None:
        rpt = Dreamer(self.store, apply=False).run()
        self.assertEqual(rpt.considered, 0)

    def test_empty_store_is_safe(self) -> None:
        report = dream(self.store, apply=True)
        self.assertEqual(report.considered, 0)
        self.assertEqual(report.expired, 0)


class ExpireReviveTests(unittest.TestCase):
    """The store-level soft expiry primitives."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "expire.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_expire_and_revive(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="hello")
        self.assertEqual(self.store.expire_memory(m.id), 1)
        self.assertIsNotNone(self.store.get_memory(m.id).invalid_at)
        self.assertEqual(self.store.revive_memory(m.id), 1)
        self.assertIsNone(self.store.get_memory(m.id).invalid_at)

    def test_expire_is_idempotent(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="hello")
        self.store.expire_memory(m.id)
        self.assertEqual(self.store.expire_memory(m.id), 0)

    def test_revive_unknown_id_is_a_noop(self) -> None:
        self.assertEqual(self.store.revive_memory("nope"), 0)

    def test_expire_unknown_id_is_a_noop(self) -> None:
        self.assertEqual(self.store.expire_memory("nope"), 0)


if __name__ == "__main__":
    unittest.main()

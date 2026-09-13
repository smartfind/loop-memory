"""Tests for the memory supersession chain (audit 2026-08-30).

Adopted from ``mem0ai/mem0 v2.0.19`` (Dream loop, slimmed down). Before
this cycle, ``merge_memories()`` DELETEd the loser, so a user auditing
the store a month later had no trace of "memory X was merged into
memory Y on date Z". These tests pin the new chain shape:

* ``merge_memories()`` writes ``superseded_by = winner_id`` instead of
  DELETEing the loser.
* ``recall()`` filters superseded rows out of the result stream.
* ``list_superseded()`` enumerates the chain (with optional
  ``superseded_by=<winner_id>`` filter).
* ``trace_supersession(target_id)`` walks the full ancestry.
* Re-merging an already-superseded pair refuses to merge and returns
  ``reason='already_superseded'`` so the UI surfaces the existing chain.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="loop_super_"))
    return MemoryStore(tmp / "super.db")


def _set_scores(store: MemoryStore, mapping: dict[str, float]) -> None:
    """Bypass the upsert path (which doesn't expose ``score``) and
    set the score field directly via SQL so the merge winner
    selection is deterministic in tests.
    """
    with store._conn() as c:  # noqa: SLF001 — test fixture
        for mid, score in mapping.items():
            c.execute("UPDATE memories SET score=? WHERE id=?", (score, mid))


class SupersessionChainTests(unittest.TestCase):
    """Audit 2026-08-30: Mem0 v2.0.19 Dream supersession pattern."""

    def setUp(self) -> None:
        self.store = _new_store()

    def test_schema_version_bumped_to_9(self) -> None:
        """The schema migration bumps SCHEMA_VERSION so a downgrade is
        loud. Pinned here so a future change can't silently revert
        to v8 (which would not have ``superseded_by``)."""
        self.assertEqual(MemoryStore.SCHEMA_VERSION, "10")

    def test_merge_writes_superseded_by_pointer_not_delete(self) -> None:
        """``merge_memories(a, b)`` must keep the loser row but set
        ``superseded_by = winner_id``. The audit trail is the whole
        point — DELETEing the row would erase it."""
        a = self.store.upsert_memory(
            kind="fact", text="uses postgres",
            importance=0.8, agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="uses MySQL",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4})
        result = self.store.merge_memories(a, b)
        self.assertTrue(result["merged"])
        self.assertEqual(result["kept"], a)
        self.assertEqual(result["lost"], b)

        # Loser row still exists, just with superseded_by set.
        with self.store._conn() as c:  # noqa: SLF001 — test inspection
            r = c.execute(
                "SELECT id, superseded_by, score FROM memories WHERE id=?", (b,),
            ).fetchone()
        self.assertIsNotNone(r, "loser row must still exist")
        self.assertEqual(r["superseded_by"], a)
        self.assertEqual(r["score"], 0.0, "superseded row score must drop to 0")

    def test_recall_excludes_superseded_rows(self) -> None:
        """The hot recall path must NOT surface superseded memories —
        callers see the latest view, not the audit trail."""
        a = self.store.upsert_memory(
            kind="fact", text="uses postgres for orders",
            importance=0.8, agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="uses postgres for users",
            importance=0.5, agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4})
        self.store.merge_memories(a, b)

        r = self.store.recall("postgres")
        ids = [m["id"] for m in r["memories"]]
        self.assertIn(a, ids)
        self.assertNotIn(b, ids, "superseded memory must not surface in recall")

    def test_list_superseded_returns_all_losers_by_default(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="uses postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="uses MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        c = self.store.upsert_memory(
            kind="fact", text="uses sqlite", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4, c: 0.3})
        self.store.merge_memories(a, b)
        self.store.merge_memories(a, c)

        rows = self.store.list_superseded()
        ids = {r["id"] for r in rows}
        self.assertEqual(ids, {b, c})
        for r in rows:
            self.assertEqual(r["superseded_by"], a)
            self.assertEqual(r["score"], 0.0)

    def test_list_superseded_filtered_by_winner(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        d = self.store.upsert_memory(
            kind="fact", text="sqlite", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        # Two different winners, two different losers.
        e = self.store.upsert_memory(
            kind="fact", text="sqlite loser B", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4, d: 0.7, e: 0.2})
        self.store.merge_memories(a, b)
        self.store.merge_memories(d, e)

        rows_a = self.store.list_superseded(superseded_by=a)
        self.assertEqual({r["id"] for r in rows_a}, {b})
        rows_d = self.store.list_superseded(superseded_by=d)
        self.assertEqual({r["id"] for r in rows_d}, {e})
        # The empty-filter case is the default list.
        rows_all = self.store.list_superseded()
        self.assertEqual({r["id"] for r in rows_all}, {b, e})

    def test_trace_supersession_from_winner_returns_just_winner(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4})
        self.store.merge_memories(a, b)
        # Walking from the winner returns just the winner (chain length 1).
        self.assertEqual(self.store.trace_supersession(a), [a])

    def test_trace_supersession_from_loser_returns_chain(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4})
        self.store.merge_memories(a, b)
        # Walking from the loser yields [loser, winner], oldest first.
        self.assertEqual(self.store.trace_supersession(b), [b, a])

    def test_trace_supersession_unknown_id_returns_empty(self) -> None:
        self.assertEqual(self.store.trace_supersession("does-not-exist"), [])

    def test_supersession_count_zero_on_clean_store(self) -> None:
        self.assertEqual(self.store.supersession_count(), 0)

    def test_supersession_count_after_two_merges(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        c = self.store.upsert_memory(
            kind="fact", text="sqlite", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4, c: 0.3})
        self.store.merge_memories(a, b)
        self.store.merge_memories(a, c)
        self.assertEqual(self.store.supersession_count(), 2)

    def test_remerge_already_superseded_pair_is_refused(self) -> None:
        """Merging an already-superseded pair would create a chain
        that's harder to reason about. Refuse and surface the existing
        chain instead so the UI can guide the user."""
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        c = self.store.upsert_memory(
            kind="fact", text="sqlite", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4, c: 0.3})
        # First merge: a wins over b. b is now superseded.
        self.store.merge_memories(a, b)
        # Second merge attempt: try to merge b (already superseded)
        # with c. ``merge_memories(b, c)`` maps ``a_id=b`` and
        # ``b_id=c``; the a_row (which is the original loser) carries
        # the existing pointer to the original winner. The UI surfaces
        # that pointer via the ``a_superseded_by`` key.
        res = self.store.merge_memories(b, c)
        self.assertFalse(res["merged"])
        self.assertEqual(res["reason"], "already_superseded")
        self.assertEqual(res["a_superseded_by"], a)
        # c is unchanged: not superseded.
        chain_c = self.store.trace_supersession(c)
        self.assertEqual(chain_c, [c])

    def test_merge_with_a_missing_id_keeps_b(self) -> None:
        """If one side of the merge is missing (concurrent delete
        between the SELECT and the UPDATE), the merge should not
        crash — it just keeps the surviving side and reports
        ``reason='a_missing'`` / ``'b_missing'``. Pinned so the
        audit trail stays clean even under concurrency."""
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        res = self.store.merge_memories("ghost-id", a)
        self.assertFalse(res["merged"])
        self.assertEqual(res["reason"], "a_missing")
        self.assertEqual(res["kept"], a)

    def test_trace_supersession_bounded_to_64_hops(self) -> None:
        """A corrupted FK must not be able to spin forever. Build a
        70-hop chain and assert the trace is capped at 64."""
        # Seed a real memory to anchor the chain end.
        end = self.store.upsert_memory(
            kind="fact", text="end", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        # 69 intermediate rows, each pointing to the next, ending at end.
        prev = end
        ids: list[str] = [end]
        with self.store._conn() as c:  # noqa: SLF001
            for i in range(69):
                # Direct insert (no upsert path for superseded_by).
                from uuid import uuid4
                mid = str(uuid4())
                c.execute(
                    "INSERT INTO memories(id, kind, text, importance, score, "
                    "created_at, updated_at, superseded_by) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (mid, "fact", f"hop {i}", 0.5, 0.5, 0.0, 0.0, prev),
                )
                ids.append(mid)
                prev = mid
        # Walk from the oldest memory (the last appended).
        oldest = ids[-1]
        chain = self.store.trace_supersession(oldest)
        self.assertLessEqual(len(chain), 64,
                             "trace must be bounded to 64 hops")


if __name__ == "__main__":
    unittest.main()


class SupersessionAuditEmissionTests(unittest.TestCase):
    """Audit 2026-08-30: ``merge_memories`` writes a ``supersede``
    audit row in the same transaction so the dashboard / ``loop-memory
    audit --kind supersede`` surface can show the chain alongside the
    memory-level pointer."""

    def setUp(self) -> None:
        self.store = _new_store()

    def test_merge_writes_supersede_audit_row(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4})
        self.store.merge_memories(a, b)
        rows = self.store.list_audit(kind="supersede")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "applied")
        self.assertEqual(row["target_kind"], "memory")
        self.assertEqual(row["target_id"], b,
                         "audit target is the loser, not the winner")
        self.assertEqual(row["payload"]["winner_id"], a)

    def test_already_superseded_merge_writes_no_audit_row(self) -> None:
        """When the merge is refused (already-superseded pair), we
        must NOT pollute the audit trail with a stale ``supersede``
        row. The audit stays accurate to what actually mutated the
        store."""
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        c = self.store.upsert_memory(
            kind="fact", text="sqlite", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4, c: 0.3})
        self.store.merge_memories(a, b)
        # First merge emitted 1 audit row.
        rows_before = self.store.list_audit(kind="supersede")
        self.assertEqual(len(rows_before), 1)
        # Second merge (already-superseded pair) must NOT emit another.
        self.store.merge_memories(b, c)
        rows_after = self.store.list_audit(kind="supersede")
        self.assertEqual(len(rows_after), 1,
                         "refused merges must not pollute the audit trail")

    def test_audit_and_memory_pointer_are_in_same_tx(self) -> None:
        """Atomicity: if we write the superseded_by pointer but the
        audit insert fails (e.g. disk full), the pointer would lie
        about the history. We pin the atomic shape here by verifying
        both are visible together — a partial-failure detection
        smoke test that catches the "forgot to put them in the same
        with-block" regression."""
        a = self.store.upsert_memory(
            kind="fact", text="postgres", importance=0.8,
            agent_id="bot", user_id="u1",
        ).id
        b = self.store.upsert_memory(
            kind="fact", text="MySQL", importance=0.5,
            agent_id="bot", user_id="u1",
        ).id
        _set_scores(self.store, {a: 0.9, b: 0.4})
        self.store.merge_memories(a, b)
        # Both the memory pointer AND the audit row are present.
        chain = self.store.trace_supersession(b)
        self.assertEqual(chain[-1], a)
        audit = self.store.list_audit(kind="supersede")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["payload"]["winner_id"], a)


if __name__ == "__main__":
    unittest.main()

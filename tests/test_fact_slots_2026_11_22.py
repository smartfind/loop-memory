"""Fact slots need one identity and one clock (audit 2026-11-22, 0.4.23).

The weekly ecosystem sync of 2026-11-16 -> 2026-11-22 produced one
theme that applies directly to Loop Memory's logical-fact slots
(``subject_key``): **a slot has an identity, and it has a clock.**

``TencentCloud/TencentDB-Agent-Memory`` v2.0.4 (2026-11-19) shipped
exactly that: "整理按对话发生的时间排序，后到的更早事实不再覆盖较新的
结论" -- organise by when the conversation happened, so a later-arriving
earlier fact no longer overwrites a newer conclusion.

Reading that against Loop Memory's own write path turned up four real
defects, all in ``MemoryStore.upsert_memory``:

1. **A write could leave its namespace.** The namespace predicate was
   ``((agent_id IS ? AND user_id IS ?) OR (agent_id IS NULL AND
   user_id IS NULL AND ? IS NULL))`` -- the second branch was gated on
   the *user_id* parameter, so any caller with an agent but no user
   also matched the **global** rows. Two consequences, both confirmed
   by reproduction:

   * an agent-scoped write **superseded a global row** of the same
     slot, so shared knowledge silently disappeared from every other
     caller's recall and was replaced by one agent's private value;
   * an agent-scoped write with the same text as a global row
     **collapsed onto it and overwrote it**, re-labelling shared
     knowledge with the agent's own ``agent_id`` -- every other
     caller lost the memory.

2. **A slot was ordered by arrival, not by fact time.** Supersession
   was "whatever was written last wins", so replaying an old
   transcript let a stale fact take the slot away from the current
   conclusion.

3. **``POST /api/v1/memories`` validated ``created_at`` and then
   dropped it**, so the row was stamped with "now" -- which is what
   made the caller's fact time indistinguishable from its arrival
   time. The batch route and the SDK already honoured it.

4. **A global ``external_id`` re-push created a duplicate row.**
   The unique index ``(agent_id, user_id, external_id)`` cannot catch
   it because SQLite treats NULLs as distinct inside a unique index,
   and the re-route was guarded by ``agent_id is not None``.

The contract pinned here:

* a write only ever touches rows in its own namespace -- the global
  namespace (NULL, NULL) is a namespace like any other;
* a slot keeps the version with the greatest **fact time**
  (``COALESCE(valid_at, created_at)``); a late-arriving older fact is
  recorded as the historical version instead of replacing what we
  already know;
* the fact time survives the whole HTTP + SDK surface;
* ``external_id`` is idempotent in every namespace, the global one
  included.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore

TERM = "velvetorbit"


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_slot_"))
    return MemoryStore(tmp / "slot.db"), tmp


class NamespaceWriteIsolationTests(unittest.TestCase):
    """A write must not leave its namespace (audit 2026-11-22)."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_agent_write_does_not_supersede_a_global_slot(self) -> None:
        """The defect: a global fact vanished when an agent wrote."""
        glob = self.store.upsert_memory(
            kind="fact", text=f"{TERM} shared global setting",
            subject_key="probe.slot",
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} agent private setting",
            subject_key="probe.slot", agent_id="bot-x",
        )
        row = self.store.get_memory(glob.id)
        self.assertIsNone(
            row.superseded_by,
            "an agent-scoped write must not supersede a global row",
        )
        texts = [m["text"] for m in self.store.recall(f"{TERM}", limit=10)["memories"]]
        self.assertIn(f"{TERM} shared global setting", texts)

    def test_an_agent_write_does_not_hijack_a_global_row(self) -> None:
        """The defect: the agent overwrote the global row in place."""
        glob = self.store.upsert_memory(
            kind="fact", text=f"{TERM} shared sentence about kubernetes",
        )
        agent = self.store.upsert_memory(
            kind="fact", text=f"{TERM} shared sentence about kubernetes",
            agent_id="bot-y",
        )
        self.assertNotEqual(
            glob.id, agent.id,
            "the agent write must be its own row, not the global one",
        )
        row = self.store.get_memory(glob.id)
        self.assertIsNone(row.agent_id, "the global row kept its identity")
        self.assertEqual(self.store.get_memory(agent.id).agent_id, "bot-y")

    def test_a_user_scoped_write_does_not_touch_the_global_namespace(self) -> None:
        glob = self.store.upsert_memory(
            kind="fact", text=f"{TERM} global note", subject_key="probe.slot",
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} user note", subject_key="probe.slot",
            user_id="alice",
        )
        self.assertIsNone(self.store.get_memory(glob.id).superseded_by)

    def test_two_agent_namespaces_keep_their_own_slots(self) -> None:
        a = self.store.upsert_memory(
            kind="fact", text=f"{TERM} value from codex",
            agent_id="codex", subject_key="probe.slot",
        )
        b = self.store.upsert_memory(
            kind="fact", text=f"{TERM} value from claude",
            agent_id="claude", subject_key="probe.slot",
        )
        self.assertIsNone(self.store.get_memory(a.id).superseded_by)
        self.assertIsNone(self.store.get_memory(b.id).superseded_by)

    def test_the_same_namespace_still_supersedes(self) -> None:
        """Regression: the normal versioning path must keep working."""
        first = self.store.upsert_memory(
            kind="fact", text=f"{TERM} version one",
            agent_id="codex", user_id="u1", subject_key="probe.slot",
        )
        second = self.store.upsert_memory(
            kind="fact", text=f"{TERM} version two",
            agent_id="codex", user_id="u1", subject_key="probe.slot",
        )
        self.assertEqual(self.store.get_memory(first.id).superseded_by, second.id)
        self.assertIsNone(self.store.get_memory(second.id).superseded_by)

    def test_a_global_write_still_supersedes_a_global_slot(self) -> None:
        """Regression: the global namespace versions itself normally."""
        first = self.store.upsert_memory(
            kind="fact", text=f"{TERM} global version one", subject_key="probe.slot",
        )
        second = self.store.upsert_memory(
            kind="fact", text=f"{TERM} global version two", subject_key="probe.slot",
        )
        self.assertEqual(self.store.get_memory(first.id).superseded_by, second.id)


class ExternalIdIdempotencyTests(unittest.TestCase):
    """``external_id`` is idempotent in every namespace (0.4.23)."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_global_external_id_repush_updates_in_place(self) -> None:
        """The defect: the global namespace duplicated the row."""
        first = self.store.upsert_memory(
            kind="fact", text=f"{TERM} first version", external_id="ext-1",
        )
        second = self.store.upsert_memory(
            kind="fact", text=f"{TERM} second version", external_id="ext-1",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.store.count_memories(), 1)

    def test_an_agent_external_id_repush_updates_in_place(self) -> None:
        first = self.store.upsert_memory(
            kind="fact", text=f"{TERM} first version",
            agent_id="bot-x", external_id="ext-1",
        )
        second = self.store.upsert_memory(
            kind="fact", text=f"{TERM} second version",
            agent_id="bot-x", external_id="ext-1",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.store.count_memories(), 1)

    def test_an_external_id_stays_inside_its_namespace(self) -> None:
        glob = self.store.upsert_memory(
            kind="fact", text=f"{TERM} global row", external_id="shared-ext",
        )
        agent = self.store.upsert_memory(
            kind="fact", text=f"{TERM} agent row",
            agent_id="bot-x", external_id="shared-ext",
        )
        self.assertNotEqual(glob.id, agent.id)
        self.assertEqual(self.store.count_memories(), 2)


class FactTimeOrderingTests(unittest.TestCase):
    """A slot is ordered by fact time, not by arrival (0.4.23)."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        self.now = time.time()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_late_arriving_older_fact_keeps_the_newer_conclusion(self) -> None:
        """The defect: the stale value took the slot."""
        newer = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-2",
            subject_key="infra.dbhost", created_at=self.now - 100,
        )
        older = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-1",
            subject_key="infra.dbhost", created_at=self.now - 3600,
        )
        self.assertIsNone(
            self.store.get_memory(newer.id).superseded_by,
            "the newer conclusion must keep the slot",
        )
        self.assertEqual(
            self.store.get_memory(older.id).superseded_by, newer.id,
            "the stale arrival becomes the historical version",
        )
        texts = [m["text"] for m in self.store.recall(f"{TERM} the host is", limit=10)["memories"]]
        self.assertIn(f"{TERM} the host is db-2", texts)
        self.assertNotIn(f"{TERM} the host is db-1", texts)

    def test_the_stale_arrival_is_kept_not_deleted(self) -> None:
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-2",
            subject_key="infra.dbhost", created_at=self.now - 100,
        )
        older = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-1",
            subject_key="infra.dbhost", created_at=self.now - 3600,
        )
        self.assertIsNotNone(
            self.store.get_memory(older.id), "history survives, nothing is deleted"
        )

    def test_a_newer_fact_arriving_later_still_takes_the_slot(self) -> None:
        older = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-1",
            subject_key="infra.dbhost", created_at=self.now - 3600,
        )
        newer = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-2",
            subject_key="infra.dbhost", created_at=self.now - 100,
        )
        self.assertEqual(self.store.get_memory(older.id).superseded_by, newer.id)
        self.assertIsNone(self.store.get_memory(newer.id).superseded_by)

    def test_latest_only_answers_with_the_fact_time_winner(self) -> None:
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-2",
            subject_key="infra.dbhost", created_at=self.now - 100,
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-1",
            subject_key="infra.dbhost", created_at=self.now - 3600,
        )
        res = self.store.recall(f"{TERM} the host is", limit=10, latest_only=True)
        texts = [m["text"] for m in res["memories"]]
        self.assertIn(f"{TERM} the host is db-2", texts)
        self.assertNotIn(f"{TERM} the host is db-1", texts)

    def test_valid_at_wins_over_created_at(self) -> None:
        """The fact time is ``COALESCE(valid_at, created_at)``.

        The first row was written first and carries an *older*
        ``created_at`` than the second one, but its validity window
        starts later -- so it is the newer fact and it keeps the
        slot. Ordering by arrival, or by ``created_at`` alone, would
        both pick the wrong row.
        """
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} value with the later window",
            subject_key="probe.slot",
            created_at=self.now - 500, valid_at=self.now - 50,
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} value with the earlier window",
            subject_key="probe.slot", created_at=self.now - 100,
        )
        live = [r for r in self.store.list_memories(limit=10) if not r.superseded_by]
        self.assertEqual(len(live), 1)
        self.assertIn("later window", live[0].text)

    def test_the_chain_still_answers_as_of(self) -> None:
        """The supersession chain keeps its historical meaning."""
        old = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-1",
            subject_key="infra.dbhost",
            created_at=self.now - 3600, updated_at=self.now - 3600,
        )
        new = self.store.upsert_memory(
            kind="fact", text=f"{TERM} the host is db-2",
            subject_key="infra.dbhost",
            created_at=self.now - 100, updated_at=self.now - 100,
        )
        self.assertEqual(self.store.get_memory(old.id).superseded_by, new.id)
        as_of = self.store.recall_as_of(f"{TERM} the host is", as_of_ts=self.now - 1000)
        texts = [m["text"] for m in as_of["memories"]]
        self.assertIn(f"{TERM} the host is db-1", texts)
        self.assertNotIn(f"{TERM} the host is db-2", texts)


class CreatedAtIsHonouredTests(unittest.TestCase):
    """The caller's fact time survives the whole write surface."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        self.client = TestClient(create_app(self.store))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_single_memories_route_stores_created_at(self) -> None:
        """The defect: the route validated the value and dropped it."""
        r = self.client.post("/api/v1/memories", json={
            "text": f"{TERM} recorded a long time ago", "created_at": 1_000_000.0,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["created_at"], 1_000_000.0)

    def test_the_batch_route_stores_created_at(self) -> None:
        r = self.client.post("/api/v1/memories:batch", json={"items": [
            {"text": f"{TERM} batch recorded long ago", "created_at": 2_000_000.0},
        ]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"][0]["created_at"], 2_000_000.0)

    def test_the_single_route_and_the_batch_route_agree(self) -> None:
        self.client.post("/api/v1/memories", json={
            "text": f"{TERM} single route", "created_at": 3_000_000.0,
        })
        self.client.post("/api/v1/memories:batch", json={"items": [
            {"text": f"{TERM} batch route", "created_at": 3_000_000.0},
        ]})
        for row in self.store.list_memories(limit=10):
            self.assertEqual(row.created_at, 3_000_000.0)

    def test_a_bad_created_at_is_still_rejected(self) -> None:
        r = self.client.post("/api/v1/memories", json={
            "text": f"{TERM} nonsense", "created_at": "not-a-number",
        })
        self.assertEqual(r.status_code, 400)

    def test_the_batch_route_accepts_a_subject_key(self) -> None:
        """A logical slot must be writable in bulk."""
        r = self.client.post("/api/v1/memories:batch", json={"items": [
            {"text": f"{TERM} bulk value one", "subject_key": "probe.slot"},
        ]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"][0]["subject_key"], "probe.slot")

    def test_the_in_process_sdk_stores_created_at(self) -> None:
        from loop_memory.sdk import MemoryClient
        sdk = MemoryClient.memory(self.store)
        m = sdk.remember(f"{TERM} sdk recorded long ago", created_at=1_500_000.0)
        self.assertEqual(m.created_at, 1_500_000.0)

    def test_the_sdk_writes_a_slot_in_the_agents_namespace(self) -> None:
        from loop_memory.sdk import MemoryClient
        sdk = MemoryClient.memory(self.store, agent_id="sdkbot")
        sdk.remember(f"{TERM} sdk slot value", subject_key="probe.slot")
        rows = self.store.list_memories(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent_id, "sdkbot")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

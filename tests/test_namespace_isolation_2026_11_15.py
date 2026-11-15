"""Agent / user namespace isolation for recall.

Audit 2026-11-15 (loop-memory 0.4.22).

Two real defects surfaced by the weekly ecosystem sync:

1. **The namespace filter was a silent no-op on the legacy path.**
   ``GET /api/v1/recall?agent_id=X`` was documented as "Unified
   recall filtered to a single Agent namespace", but the legacy
   ``MemoryStore.recall()`` never returned the ``agent_id`` /
   ``user_id`` columns, so the route's in-Python filter saw
   ``None``/``None`` on every row and let all of them through. An
   agent asking for its own namespace was handed **every other
   agent's memories** as well.

2. **The namespace filter ran after the LIMIT.** On the hybrid path
   the filter did work, but it ran on the store's already-trimmed
   result set, so a foreign namespace could spend the caller's whole
   result budget: an agent could get *zero* hits while its own
   matching rows sat further down the ranking.

The fix pushes the predicate into the store's SQL -- into every
retrieval channel (BM25, semantic, entity) -- so the ``LIMIT`` counts
only the caller's own rows.

Upstream pattern references (no code copied):

* ``plastic-labs/honcho`` v3.6.0 (2026-11-10, AGPL-3.0) made
  ``tenant_id`` lead every primary key with Postgres row-level
  security scoping every read and write, and refused to boot when
  isolation could not be established.
* ``EverMind-AI/EverOS`` v1.6.0 (2026-11-11) made deletion
  owner-scoped and answered ``503 SESSION_BUSY`` rather than
  deleting half of a session.
* ``thedotmack/claude-mem`` v15.18.3 (2026-11-12) made each host
  register its own hooks under its own identity.
* ``vectorize-io/hindsight`` #6280 (2026-11-10) stopped a bank clone
  from taking over another bank's alias.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore, _scope_clause


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_ns_"))
    db = tmp / "ns.db"
    return MemoryStore(db), db


def _new_client() -> tuple[TestClient, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_ns_api_"))
    db = tmp / "ns_api.db"
    store = MemoryStore(db)
    app = create_app(store, static_dir=None)
    return TestClient(app), db


# The shared query term. Every seeded memory contains it so the
# lexical channels all fire; only the namespace differs.
TERM = "zephyrus"


class ScopeClauseTests(unittest.TestCase):
    """Unit tests for the SQL fragment builder."""

    def test_no_filter_returns_empty_fragment(self) -> None:
        sql, params = _scope_clause(None, None)
        self.assertEqual(sql, "")
        self.assertEqual(params, [])

    def test_empty_string_means_unfiltered(self) -> None:
        # A blank query parameter must never *widen* the result set
        # by accident -- it means "not specified".
        sql, params = _scope_clause("", "")
        self.assertEqual(sql, "")
        self.assertEqual(params, [])

    def test_agent_filter_allows_global_rows(self) -> None:
        sql, params = _scope_clause("a1", None)
        self.assertIn("agent_id IS NULL", sql)
        self.assertIn("agent_id = ?", sql)
        self.assertEqual(params, ["a1"])

    def test_user_filter_allows_global_rows(self) -> None:
        sql, params = _scope_clause(None, "u1")
        self.assertIn("user_id IS NULL", sql)
        self.assertIn("user_id = ?", sql)
        self.assertEqual(params, ["u1"])

    def test_both_filters_are_anded(self) -> None:
        sql, params = _scope_clause("a1", "u1")
        self.assertEqual(sql.count(" AND "), 2)  # leading AND + one joiner
        self.assertEqual(params, ["a1", "u1"])

    def test_fragment_is_safe_to_paste(self) -> None:
        # The fragment carries its own leading " AND " so a call site
        # cannot produce "WHERE a AND (b) (c)" -- the syntax error
        # that a missing connector causes.
        sql, _ = _scope_clause("a1", None)
        self.assertTrue(sql.startswith(" AND "))
        self.assertFalse(sql.startswith(" AND  "))


class StoreNamespaceTests(unittest.TestCase):
    """The predicate must live in the store, not in the caller."""

    def setUp(self) -> None:
        self.store, self.db = _new_store()
        # agentB rows rank higher on purpose.
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} rollout uses canary",
            importance=0.95, source="codex", agent_id="agentB", user_id="u1",
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} rollout uses blue green",
            importance=0.50, source="codex", agent_id="agentA", user_id="u1",
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} rollout global note",
            importance=0.40, source="codex", agent_id=None, user_id=None,
        )

    def test_legacy_recall_excludes_foreign_namespace(self) -> None:
        r = self.store.recall(TERM, limit=10, agent_id="agentA")
        texts = [m["text"] for m in r["memories"]]
        self.assertNotIn(f"{TERM} rollout uses canary", texts)
        self.assertIn(f"{TERM} rollout uses blue green", texts)

    def test_hybrid_recall_excludes_foreign_namespace(self) -> None:
        r = self.store.recall_hybrid(TERM, limit=10, agent_id="agentA")
        texts = [m["text"] for m in r["memories"]]
        self.assertNotIn(f"{TERM} rollout uses canary", texts)
        self.assertIn(f"{TERM} rollout uses blue green", texts)

    def test_global_rows_stay_visible_to_a_scoped_caller(self) -> None:
        for fn in (self.store.recall, self.store.recall_hybrid):
            r = fn(TERM, limit=10, agent_id="agentA")
            texts = [m["text"] for m in r["memories"]]
            self.assertIn(f"{TERM} rollout global note", texts)

    def test_unscoped_recall_is_unchanged(self) -> None:
        # None means "no restriction" -- the legacy see-everything
        # behaviour every existing caller relies on.
        for fn in (self.store.recall, self.store.recall_hybrid):
            r = fn(TERM, limit=10)
            self.assertEqual(len(r["memories"]), 3)

    def test_empty_agent_id_means_unrestricted(self) -> None:
        r = self.store.recall(TERM, limit=10, agent_id="")
        self.assertEqual(len(r["memories"]), 3)

    def test_user_id_scoping(self) -> None:
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} rollout belongs to bob",
            importance=0.9, source="codex", agent_id="agentA", user_id="bob",
        )
        r = self.store.recall(TERM, limit=10, agent_id="agentA", user_id="alice")
        texts = [m["text"] for m in r["memories"]]
        self.assertNotIn(f"{TERM} rollout belongs to bob", texts)
        r = self.store.recall(TERM, limit=10, agent_id="agentA", user_id="bob")
        texts = [m["text"] for m in r["memories"]]
        self.assertIn(f"{TERM} rollout belongs to bob", texts)

    def test_recall_reports_the_namespace_of_each_hit(self) -> None:
        # The output has to carry the namespace, otherwise a filter
        # applied on top of it can never tell the rows apart.
        r = self.store.recall(TERM, limit=10)
        by_text = {m["text"]: m for m in r["memories"]}
        self.assertEqual(by_text[f"{TERM} rollout uses canary"]["agent_id"], "agentB")
        self.assertEqual(by_text[f"{TERM} rollout uses canary"]["user_id"], "u1")
        self.assertIsNone(by_text[f"{TERM} rollout global note"]["agent_id"])

    def test_recall_as_of_is_scoped(self) -> None:
        import time
        r = self.store.recall_as_of(TERM, time.time(), limit=10, agent_id="agentA")
        texts = [m["text"] for m in r["memories"]]
        self.assertNotIn(f"{TERM} rollout uses canary", texts)


class NamespaceBudgetTests(unittest.TestCase):
    """The scoped row set must own the whole result budget.

    Before 0.4.22 the filter ran *after* the store's ``LIMIT``, so a
    foreign namespace could push the caller's own rows out of the
    answer entirely.
    """

    def setUp(self) -> None:
        self.store, self.db = _new_store()
        # 20 foreign rows that all rank above the caller's own row.
        for i in range(20):
            self.store.upsert_memory(
                kind="fact", text=f"{TERM} foreign record number {i}",
                importance=0.9, source="codex", agent_id="agentB", user_id="u1",
            )
        # The caller's own row ranks last.
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} the one row that belongs to me",
            importance=0.1, source="codex", agent_id="agentA", user_id="u1",
        )

    def test_legacy_recall_still_finds_the_own_row(self) -> None:
        r = self.store.recall(TERM, limit=5, agent_id="agentA")
        texts = [m["text"] for m in r["memories"]]
        self.assertIn(f"{TERM} the one row that belongs to me", texts)

    def test_hybrid_recall_still_finds_the_own_row(self) -> None:
        r = self.store.recall_hybrid(TERM, limit=5, agent_id="agentA")
        texts = [m["text"] for m in r["memories"]]
        self.assertIn(f"{TERM} the one row that belongs to me", texts)

    def test_the_budget_is_not_shared(self) -> None:
        # Every returned row belongs to the caller -- the foreign rows
        # must not appear at all, let alone eat the budget.
        r = self.store.recall_hybrid(TERM, limit=5, agent_id="agentA")
        for m in r["memories"]:
            self.assertIn(m["agent_id"], (None, "agentA"))


class ChannelScopeTests(unittest.TestCase):
    """Each retrieval channel must carry the predicate.

    The hybrid path fuses three channels (BM25, semantic, entity).
    If only one of them is scoped, the foreign rows still reach the
    fusion and still eat the budget.
    """

    def setUp(self) -> None:
        self.store, self.db = _new_store()
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} foreign deployment note",
            importance=0.9, source="codex", agent_id="agentB", user_id="u1",
            embedding=[1.0, 0.0, 0.0],
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} own deployment note",
            importance=0.5, source="codex", agent_id="agentA", user_id="u1",
            embedding=[1.0, 0.0, 0.0],
        )

    def test_bm25_channel_is_scoped(self) -> None:
        from loop_memory.storage.retrieval import bm25_search
        rows = bm25_search(self.store, TERM, kind="memories", limit=10,
                           agent_id="agentA", user_id="u1")
        self.assertEqual(len(rows), 1)

    def test_bm25_channel_without_a_filter_is_unchanged(self) -> None:
        from loop_memory.storage.retrieval import bm25_search
        rows = bm25_search(self.store, TERM, kind="memories", limit=10)
        self.assertEqual(len(rows), 2)

    def test_semantic_channel_is_scoped(self) -> None:
        rows = self.store.search_by_embedding([1.0, 0.0, 0.0], top_k=10,
                                              agent_id="agentA", user_id="u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent_id, "agentA")

    def test_semantic_channel_without_a_filter_is_unchanged(self) -> None:
        rows = self.store.search_by_embedding([1.0, 0.0, 0.0], top_k=10)
        self.assertEqual(len(rows), 2)

    def test_entity_channel_is_scoped(self) -> None:
        # The entity channel only fires when the memory is linked to
        # an entity, so link one to each namespace first.
        for agent, label in (("agentA", "own"), ("agentB", "foreign")):
            mem = self.store.upsert_memory(
                kind="fact", text=f"{TERM} {label} entity note",
                importance=0.5, source="codex", agent_id=agent, user_id="u1",
            )
            self.store.upsert_entity(name="Zephyrus", kind="project")
            self.store.upsert_entity_mention(mem.id, "Zephyrus")
        rows = self.store.search_memories_by_entity_names(
            ["zephyrus"], limit=10, agent_id="agentA", user_id="u1",
        )
        self.assertEqual(len(rows), 1)

    def test_entity_channel_without_a_filter_is_unchanged(self) -> None:
        for agent, label in (("agentA", "own"), ("agentB", "foreign")):
            mem = self.store.upsert_memory(
                kind="fact", text=f"{TERM} {label} entity note",
                importance=0.5, source="codex", agent_id=agent, user_id="u1",
            )
            self.store.upsert_entity(name="Zephyrus", kind="project")
            self.store.upsert_entity_mention(mem.id, "Zephyrus")
        rows = self.store.search_memories_by_entity_names(["zephyrus"], limit=10)
        self.assertEqual(len(rows), 2)


class HttpNamespaceTests(unittest.TestCase):
    """The HTTP surface -- where the leak was observable."""

    def setUp(self) -> None:
        self.c, self.db = _new_client()
        self.c.post("/api/v1/memories", json={
            "text": f"{TERM} team rollout uses canary",
            "kind": "fact", "importance": 0.95,
            "agent_id": "team-bot", "user_id": "alice",
            "external_id": "foreign-1",
        })
        self.c.post("/api/v1/memories", json={
            "text": f"{TERM} team rollout uses blue green",
            "kind": "fact", "importance": 0.5,
            "agent_id": "other-bot", "user_id": "alice",
            "external_id": "own-1",
        })
        self.c.post("/api/v1/memories", json={
            "text": f"{TERM} team rollout shared note",
            "kind": "fact", "importance": 0.4,
            "external_id": "global-1",
        })

    def _texts(self, r) -> list[str]:
        return [m["text"] for m in r.json()["memories"]]

    def test_hybrid_mode_excludes_other_agents(self) -> None:
        r = self.c.get("/api/v1/recall", params={
            "q": TERM, "agent_id": "other-bot", "limit": 10,
        })
        self.assertEqual(r.status_code, 200)
        texts = self._texts(r)
        self.assertNotIn(f"{TERM} team rollout uses canary", texts)
        self.assertIn(f"{TERM} team rollout uses blue green", texts)

    def test_legacy_mode_excludes_other_agents(self) -> None:
        # This is the path that leaked: the legacy store never
        # returned ``agent_id``, so the route filter was a no-op.
        r = self.c.get("/api/v1/recall", params={
            "q": TERM, "agent_id": "other-bot", "limit": 10, "mode": "legacy",
        })
        self.assertEqual(r.status_code, 200)
        texts = self._texts(r)
        self.assertNotIn(f"{TERM} team rollout uses canary", texts)

    def test_global_memories_stay_visible(self) -> None:
        r = self.c.get("/api/v1/recall", params={
            "q": TERM, "agent_id": "other-bot", "limit": 10,
        })
        self.assertIn(f"{TERM} team rollout shared note", self._texts(r))

    def test_unscoped_recall_still_sees_everything(self) -> None:
        r = self.c.get("/api/v1/recall", params={"q": TERM, "limit": 10})
        self.assertEqual(len(self._texts(r)), 3)

    def test_scoped_recall_keeps_its_own_budget(self) -> None:
        # 12 foreign rows rank above the caller's own row.
        for i in range(12):
            self.c.post("/api/v1/memories", json={
                "text": f"{TERM} bulk foreign row number {i}",
                "kind": "fact", "importance": 0.9,
                "agent_id": "team-bot", "user_id": "alice",
                "external_id": f"bulk-{i}",
            })
        r = self.c.get("/api/v1/recall", params={
            "q": TERM, "agent_id": "other-bot", "limit": 5,
        })
        texts = self._texts(r)
        self.assertIn(f"{TERM} team rollout uses blue green", texts)
        for t in texts:
            self.assertNotIn("bulk foreign row", t)

    def test_dashboard_recall_accepts_a_namespace(self) -> None:
        r = self.c.get("/api/recall", params={
            "query": TERM, "agent_id": "other-bot", "limit": 10,
        })
        self.assertEqual(r.status_code, 200)
        texts = self._texts(r)
        self.assertNotIn(f"{TERM} team rollout uses canary", texts)

    def test_a_blank_namespace_means_no_restriction(self) -> None:
        # An empty query parameter must never narrow the answer to
        # "match nothing" -- it means "not specified".
        r = self.c.get("/api/v1/recall", params={
            "q": TERM, "agent_id": "", "user_id": "", "limit": 10,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self._texts(r)), 3)

    def test_list_memories_keeps_its_exact_match_semantics(self) -> None:
        # ``GET /api/v1/memories`` filters by exact match and has
        # always excluded global rows. 0.4.22 must not change it.
        r = self.c.get("/api/v1/memories", params={"agent_id": "other-bot"})
        texts = [m["text"] for m in r.json()["memories"]]
        self.assertEqual(len(texts), 1)
        self.assertIn(f"{TERM} team rollout uses blue green", texts)


class McpNamespaceTests(unittest.TestCase):
    """The MCP server is started once per client, so the environment
    already identifies the caller."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="loop_ns_mcp_")
        self.db = Path(self.tmp) / "mcp.db"
        self.prev_db = os.environ.get("LOOP_MEMORY_DB")
        self.prev_agent = os.environ.get("LOOP_MEMORY_AGENT_ID")
        self.prev_user = os.environ.get("LOOP_MEMORY_USER_ID")
        os.environ["LOOP_MEMORY_DB"] = str(self.db)
        self.store = MemoryStore(self.db)
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} foreign rollout note",
            importance=0.9, source="codex", agent_id="agentB", user_id="u1",
        )
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} own rollout note",
            importance=0.5, source="codex", agent_id="agentA", user_id="u1",
        )

    def tearDown(self) -> None:
        for key, prev in (
            ("LOOP_MEMORY_DB", self.prev_db),
            ("LOOP_MEMORY_AGENT_ID", self.prev_agent),
            ("LOOP_MEMORY_USER_ID", self.prev_user),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    def _recall(self, **kwargs) -> str:
        from loop_memory.mcp import tool_recall
        out = tool_recall({"query": TERM, **kwargs})
        return "\n".join(c.get("text", "") for c in out)

    def test_recall_is_scoped_by_the_environment(self) -> None:
        os.environ["LOOP_MEMORY_AGENT_ID"] = "agentA"
        os.environ["LOOP_MEMORY_USER_ID"] = "u1"
        text = self._recall()
        self.assertIn("own rollout note", text)
        self.assertNotIn("foreign rollout note", text)

    def test_an_explicit_agent_id_overrides_the_environment(self) -> None:
        os.environ["LOOP_MEMORY_AGENT_ID"] = "agentA"
        text = self._recall(agent_id="agentB")
        self.assertIn("foreign rollout note", text)
        self.assertNotIn("own rollout note", text)

    def test_an_empty_agent_id_means_no_restriction(self) -> None:
        os.environ["LOOP_MEMORY_AGENT_ID"] = "agentA"
        text = self._recall(agent_id="")
        self.assertIn("own rollout note", text)
        self.assertIn("foreign rollout note", text)

    def test_without_the_environment_nothing_is_scoped(self) -> None:
        os.environ.pop("LOOP_MEMORY_AGENT_ID", None)
        os.environ.pop("LOOP_MEMORY_USER_ID", None)
        text = self._recall()
        self.assertIn("own rollout note", text)
        self.assertIn("foreign rollout note", text)

    def test_the_tool_schema_documents_the_namespace(self) -> None:
        from loop_memory.mcp import TOOLS
        schema = next(t for t in TOOLS if t["name"] == "recall")["inputSchema"]
        self.assertIn("agent_id", schema["properties"])
        self.assertIn("user_id", schema["properties"])


class SdkNamespaceTests(unittest.TestCase):
    """A client bound to a namespace reads from it (0.4.22).

    Before this release ``MemoryClient.memory(store, agent_id="x")``
    *wrote* into the "x" namespace but *read* from everywhere -- the
    asymmetry that made the leak invisible in the SDK tests.
    """

    def setUp(self) -> None:
        from loop_memory.sdk import MemoryClient
        self.tmp = tempfile.mkdtemp(prefix="loop_ns_sdk_")
        self.store = MemoryStore(Path(self.tmp) / "sdk.db")
        self.alpha = MemoryClient.memory(self.store, agent_id="alpha", user_id="u1")
        self.beta = MemoryClient.memory(self.store, agent_id="beta", user_id="u1")
        self.alpha.remember(f"{TERM} alpha only note", external_id="a1")
        self.beta.remember(f"{TERM} beta only note", external_id="b1")

    def test_a_bound_client_reads_from_its_own_namespace(self) -> None:
        r = self.beta.recall(TERM, limit=10)
        texts = [m.text for m in r.memories]
        self.assertIn(f"{TERM} beta only note", texts)
        self.assertNotIn(f"{TERM} alpha only note", texts)

    def test_an_empty_agent_id_opens_the_whole_store(self) -> None:
        # The escape hatch: ``agent_id=""`` means "no restriction".
        r = self.beta.recall(TERM, limit=10, agent_id="")
        texts = [m.text for m in r.memories]
        self.assertIn(f"{TERM} beta only note", texts)
        self.assertIn(f"{TERM} alpha only note", texts)

    def test_an_explicit_agent_id_overrides_the_bound_one(self) -> None:
        r = self.beta.recall(TERM, limit=10, agent_id="alpha")
        texts = [m.text for m in r.memories]
        self.assertIn(f"{TERM} alpha only note", texts)
        self.assertNotIn(f"{TERM} beta only note", texts)

    def test_global_memories_stay_visible(self) -> None:
        self.store.upsert_memory(
            kind="fact", text=f"{TERM} shared note",
            importance=0.4, source="codex",
        )
        r = self.beta.recall(TERM, limit=10)
        texts = [m.text for m in r.memories]
        self.assertIn(f"{TERM} shared note", texts)


class CliNamespaceTests(unittest.TestCase):
    """``loop-memory recall --agent-id`` (audit 2026-11-15)."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="loop_ns_cli_")
        self.db_path = Path(self.tmp) / "cli.db"
        self.prev = os.environ.get("_LOOP_MEMORY_TEST_DB")
        os.environ["_LOOP_MEMORY_TEST_DB"] = str(self.db_path)
        store = MemoryStore(self.db_path)
        store.upsert_memory(
            kind="fact", text=f"{TERM} foreign rollout note",
            importance=0.9, source="codex", agent_id="agentB", user_id="u1",
        )
        store.upsert_memory(
            kind="fact", text=f"{TERM} own rollout note",
            importance=0.5, source="codex", agent_id="agentA", user_id="u1",
        )

    def tearDown(self) -> None:
        if self.prev is None:
            os.environ.pop("_LOOP_MEMORY_TEST_DB", None)
        else:
            os.environ["_LOOP_MEMORY_TEST_DB"] = self.prev

    def _run(self, args: list[str]):
        import subprocess
        import sys
        repo_root = str(Path(__file__).resolve().parent.parent)
        env = os.environ.copy()
        env["PYTHONPATH"] = repo_root
        env["LOOP_MEMORY_DB"] = str(self.db_path)
        return subprocess.run(
            [sys.executable, "-m", "loop_memory.cli.main", "recall", *args],
            capture_output=True, text=True, env=env, cwd="/tmp",
        )

    def test_agent_id_scopes_the_output(self) -> None:
        proc = self._run([TERM, "--agent-id", "agentA"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("own rollout note", proc.stdout)
        self.assertNotIn("foreign rollout note", proc.stdout)

    def test_without_the_flag_everything_is_visible(self) -> None:
        proc = self._run([TERM])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("own rollout note", proc.stdout)
        self.assertIn("foreign rollout note", proc.stdout)

    def test_user_id_scopes_the_output(self) -> None:
        proc = self._run([TERM, "--user-id", "u1", "--agent-id", "agentA"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("own rollout note", proc.stdout)
        self.assertNotIn("foreign rollout note", proc.stdout)


if __name__ == "__main__":
    unittest.main()

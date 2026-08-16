"""Tests for the v7 Universal Agent Memory surface:

* ``MemoryStore`` schema v7 (wiki_versions, cognitive_audit, auth_tokens)
* ``jobs.graph`` semantic edges, subgraph, adaptive scoring
* ``jobs.cognitive`` cognitive_sleep + audit
* ``export`` bundle round-trip + fork
* SDK namespace / graph / cognitive / export extensions
* HTTP /api/v1/* routes
* MCP v7 tools
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.export import export_bundle, fork_snapshot, import_bundle
from loop_memory.jobs.cognitive import cognitive_sleep
from loop_memory.jobs.graph import (
    adaptive_score,
    graph_boost,
    subgraph_for,
    upsert_semantic_edge,
)
from loop_memory.sdk import MemoryClient
from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop_v7_"))
    db = tmp / "v7.db"
    return MemoryStore(db), db


# ---------------------------------------------------------------------------
# Schema v7
# ---------------------------------------------------------------------------


class SchemaV7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db = _new_store()

    def test_wiki_versions_crud(self) -> None:
        pid = self.store.upsert_wiki_page(slug="x", title="X", body="b",
                                           summary="s", tags=["t"], importance=0.7)["id"]
        v1 = self.store.snapshot_wiki_version(pid)
        self.assertEqual(v1["version"], 1)
        self.store.upsert_wiki_page(slug="x", title="X2", body="b2",
                                     summary="s", tags=["t"], importance=0.8)
        v2 = self.store.snapshot_wiki_version(pid)
        self.assertEqual(v2["version"], 2)
        rows = self.store.list_wiki_versions(pid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["version"], 2)

    def test_cognitive_audit_crud(self) -> None:
        a = self.store.record_audit(kind="forget", action="suggest",
                                     target_kind="memory", target_id="m1",
                                     reason="low_score", score=0.1)
        self.assertEqual(a["kind"], "forget")
        self.assertEqual(self.store.list_audit(kind="forget")[0]["id"], a["id"])

    def test_auth_tokens_issue_verify_revoke(self) -> None:
        t = self.store.issue_token(user_id="u1", agent_id="bot", label="x")
        v = self.store.verify_token(t["token"])
        self.assertEqual(v["user_id"], "u1")
        self.assertTrue(self.store.revoke_token(t["id"]))
        self.assertIsNone(self.store.verify_token(t["token"]))

    def test_auth_tokens_expire(self) -> None:
        t = self.store.issue_token(user_id="u1", expires_in=-1)
        self.assertIsNone(self.store.verify_token(t["token"]))


# ---------------------------------------------------------------------------
# Graph + 3D adaptive scoring
# ---------------------------------------------------------------------------


class GraphJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = _new_store()

    def test_upsert_semantic_edge(self) -> None:
        upsert_semantic_edge(self.store, "Alice", "Hangzhou",
                             kind="lives_in", weight=0.9)
        ents = self.store.entity_by_name("Alice")
        self.assertIsNotNone(ents)
        rels = self.store.related_entities("Alice")
        self.assertIn("Hangzhou", rels)

    def test_upsert_semantic_edge_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            upsert_semantic_edge(self.store, "", "x")
        with self.assertRaises(ValueError):
            upsert_semantic_edge(self.store, "x", "x")

    def test_subgraph_for_returns_related_entities(self) -> None:
        self.store.upsert_memory(kind="fact",
                                 text="Alice works on Project Atlas using Postgres",
                                 importance=0.7, agent_id="bot")
        self.store.rebuild_entity_mentions()
        upsert_semantic_edge(self.store, "Alice", "Hangzhou", kind="lives_in", weight=0.9)
        sg = subgraph_for(self.store, "Where does Alice live?")
        names = [n["name"] for n in sg.nodes]
        self.assertIn("Alice", names)
        self.assertIn("Hangzhou", names)

    def test_adaptive_score_is_bounded(self) -> None:
        for importance, recall, expected_blend in [
            (0.0, 0, 0.0),
            (1.0, 100, 0.4 + 0.25 * 1.0 + 0.15 * 0.0),  # max importance+usage
            (0.5, 0, 0.2),
        ]:
            s = adaptive_score(importance=importance, created_at=0, now=1.0,
                               recall_count=recall, last_recalled_at=0)
            self.assertGreaterEqual(s.blended, 0.0)
            self.assertLessEqual(s.blended, 1.0)

    def test_graph_boost_after_rebuild(self) -> None:
        from loop_memory.graph.build import KnowledgeGraph
        self.store.upsert_memory(kind="fact",
                                 text="Alice works on Atlas using Postgres",
                                 importance=0.8, agent_id="bot", user_id="u1")
        self.store.upsert_memory(kind="fact",
                                 text="Atlas uses Postgres for orders",
                                 importance=0.7, agent_id="bot", user_id="u1")
        self.store.upsert_memory(kind="preference",
                                 text="Alice prefers dark mode UI",
                                 importance=0.5, agent_id="bot", user_id="u1")
        # Rebuild BOTH relations (co-occurs_with) AND entity_mentions
        # so the 1-hop graph in ``graph_boost`` has something to walk.
        KnowledgeGraph(self.store).rebuild(clear=True)
        self.store.rebuild_entity_mentions()
        ids = [r.id for r in self.store.list_memories(limit=10)]
        boosts = graph_boost(self.store, "Alice Atlas", ids)
        # At least one of the Alice+Atlas memories should have a boost
        self.assertTrue(boosts, "graph_boost should find at least one boost")
        self.assertGreater(max(b.boost for b in boosts.values()), 0.0)

    def test_recall_hybrid_adaptive_keeps_dashboard_compatible(self) -> None:
        self.store.upsert_memory(kind="fact", text="alpha alpha alpha",
                                 importance=0.7, agent_id="bot", user_id="u1")
        out_default = self.store.recall_hybrid("alpha", limit=5)
        out_adaptive = self.store.recall_hybrid("alpha", limit=5, adaptive=True)
        # Both must return the same shape; adaptive just adds scores.
        self.assertIn("memories", out_default)
        self.assertIn("memories", out_adaptive)
        self.assertTrue(out_adaptive.get("adaptive"))
        # The adaptive result should have _adaptive on the first hit
        if out_adaptive["memories"]:
            self.assertIn("_adaptive", out_adaptive["memories"][0])


# ---------------------------------------------------------------------------
# Cognitive sleep
# ---------------------------------------------------------------------------


class CognitiveSleepObservabilityTests(unittest.TestCase):
    """Audit 2026-08-16: progress + deadline observability.

    The cognitive sweep now exposes per-stage timings (``stages``)
    on the report, an optional ``progress`` callback that fires once
    per stage, and a ``deadline_seconds`` budget that aborts the
    sweep with ``aborted=True`` and an ``abort_reason`` that names
    the stage it stalled in. These tests pin the new contract.
    """

    def setUp(self) -> None:
        self.store, _ = _new_store()

    def test_stages_present_in_dry_run(self) -> None:
        self.store.upsert_memory(kind="fact", text="ok", importance=0.6)
        rpt = cognitive_sleep(self.store, apply=False)
        # All seven stages must be recorded, even when the sweep
        # is short and some of them are zero-cost.
        expected = {"scan", "stale", "merge", "contradict", "apply", "audit"}
        # ``stale`` and ``audit`` are split across the single
        # sweep pass above, so we accept either ordering.
        self.assertTrue(expected.issubset(set(rpt.stages)),
                        f"missing stages: {expected - set(rpt.stages)}")
        self.assertFalse(rpt.aborted)
        self.assertEqual(rpt.abort_reason, "")
        d = rpt.to_dict()
        self.assertIn("stages", d)
        self.assertIn("aborted", d)

    def test_progress_callback_receives_each_stage(self) -> None:
        self.store.upsert_memory(kind="fact", text="ok", importance=0.6)
        seen: list[str] = []
        rpt = cognitive_sleep(
            self.store, apply=False,
            progress=lambda stage: seen.append(stage),
        )
        # At minimum scan / stale / merge / contradict / apply / audit.
        for expected in ("scan", "stale", "merge", "contradict", "apply", "audit"):
            self.assertIn(expected, seen, f"missing stage {expected!r} in {seen}")
        self.assertFalse(rpt.aborted)

    def test_progress_callback_exception_does_not_break_sweep(self) -> None:
        self.store.upsert_memory(kind="fact", text="ok", importance=0.6)
        def bad(stage: str) -> None:
            raise RuntimeError(f"boom {stage}")
        rpt = cognitive_sleep(self.store, apply=False, progress=bad)
        self.assertFalse(rpt.aborted)

    def test_deadline_aborts_with_stage_named(self) -> None:
        # Seed many memories so the near-duplicate O(n^2) stage
        # has enough work to actually overrun a sub-second budget.
        from loop_memory.jobs import cognitive as cog
        for i in range(40):
            self.store.upsert_memory(
                kind="fact",
                text=f"Postgres orders table is used by service number {i} for writes "
                     f"and reads with replication in zone {i % 3}",
                importance=0.5, agent_id="bot",
            )
        rpt = cog.cognitive_sleep(self.store, apply=False, deadline_seconds=0.0)
        self.assertTrue(rpt.aborted, "deadline_seconds=0 should abort the sweep")
        self.assertTrue(rpt.abort_reason.startswith("deadline exceeded"),
                        f"abort_reason must start with 'deadline exceeded': {rpt.abort_reason!r}")
        # The reason names the stage where the budget fired; this
        # is the contract the EverOS-style "stalls must name the
        # stage" pattern asks for.
        self.assertTrue(any(stage in rpt.abort_reason
                            for stage in ("scan", "stale", "merge",
                                          "contradict", "apply", "audit")),
                        f"abort_reason must name a stage: {rpt.abort_reason!r}")

    def test_no_deadline_means_no_abort(self) -> None:
        self.store.upsert_memory(kind="fact", text="hello world", importance=0.5)
        rpt = cognitive_sleep(self.store, apply=False, deadline_seconds=None)
        self.assertFalse(rpt.aborted)


class GetSignalsBulkTests(unittest.TestCase):
    """Audit 2026-08-16: bulk signal fetch for the cognitive sweep.

    ``get_signals`` was added to replace the per-memory ``get_signal``
    round-trip the cognitive sweep used to do. These tests pin the
    contract: same shape as ``get_signal`` for missing rows, single
    SQL hit, and the cognitive sweep no longer talks to ``_conn``
    during the low-value gate.
    """

    def setUp(self) -> None:
        self.store, _ = _new_store()

    def test_get_signals_matches_get_signal_for_present_rows(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="hello", importance=0.5)
        self.store.bump_recalls([m.id])
        self.store.bump_recalls([m.id])
        bulk = self.store.get_signals([m.id])
        single = self.store.get_signal(m.id)
        self.assertEqual(set(bulk[m.id].keys()), set(single.keys()))
        self.assertEqual(bulk[m.id]["recall_count"], 2)
        self.assertEqual(single["recall_count"], 2)

    def test_get_signals_returns_zero_shape_for_missing_rows(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="x", importance=0.5)
        bulk = self.store.get_signals([m.id, "ghost-id"])
        self.assertEqual(bulk["ghost-id"], {
            "recall_count": 0, "positive": 0, "negative": 0,
            "last_recalled_at": None, "last_feedback_at": None,
        })
        # Present row keeps its real values.
        self.assertEqual(bulk[m.id]["recall_count"], 0)

    def test_get_signals_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(self.store.get_signals([]), {})
        # ``""`` and ``None`` are filtered out; non-empty whitespace
        # survives and gets a zero-shape row, mirroring the existing
        # ``get_signal`` per-id contract.
        self.assertEqual(self.store.get_signals(["", None]), {})

    def test_cognitive_sleep_does_not_call_get_signal_internally(self) -> None:
        """Pin that the N+1 anti-pattern is gone.

        ``get_signal`` per memory would have been replaced by the bulk
        ``get_signals``; we assert the bulk path is the one that
        surfaces recall_count to the low-value gate.
        """
        from loop_memory.jobs import cognitive as cog
        original_get_signal = self.store.get_signal
        original_get_signals = self.store.get_signals
        per_id_hits = 0
        bulk_hits = 0

        def counting_get_signal(mid):
            nonlocal per_id_hits
            per_id_hits += 1
            return original_get_signal(mid)

        def counting_get_signals(ids):
            nonlocal bulk_hits
            bulk_hits += 1
            return original_get_signals(ids)

        self.store.get_signal = counting_get_signal  # type: ignore[assignment]
        self.store.get_signals = counting_get_signals  # type: ignore[assignment]
        # Seed >5 memories so a regression to per-id SELECTs would
        # be visible. They are low-score, low-importance so the
        # stale gate fires and the low-value gate can use signals.
        for i in range(6):
            self.store.upsert_memory(
                kind="fact", text=f"noise {i}",
                importance=0.05, agent_id="bot",
                created_at=time.time() - 365 * 86400,
            )
        rpt = cog.cognitive_sleep(self.store, apply=False, stale_days=90,
                                  min_score=0.5, min_importance=0.3)
        self.assertGreaterEqual(rpt.counts["stale"], 6)
        # Bulk fetch must have been called exactly once for the sweep.
        self.assertEqual(bulk_hits, 1, "cognitive_sleep should bulk-fetch signals once")
        self.assertEqual(per_id_hits, 0, "cognitive_sleep must not call get_signal per row")


class CognitiveSleepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = _new_store()

    def test_dry_run_does_not_delete(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="old noise",
                                     importance=0.1, agent_id="bot",
                                     created_at=time.time() - 200 * 86400)
        rpt = cognitive_sleep(self.store, apply=False, stale_days=90,
                              min_score=0.5, min_importance=0.3)
        self.assertEqual(rpt.counts["stale"], 1)
        # Memory still exists
        self.assertIsNotNone(self.store.get_memory(m.id))

    def test_apply_deletes_and_records_audit(self) -> None:
        m = self.store.upsert_memory(kind="fact", text="old noise",
                                     importance=0.1, agent_id="bot",
                                     created_at=time.time() - 200 * 86400)
        rpt = cognitive_sleep(self.store, apply=True, stale_days=90,
                              min_score=0.5, min_importance=0.3)
        self.assertEqual(rpt.counts["forget"], 1)
        self.assertIsNone(self.store.get_memory(m.id))
        audit = self.store.list_audit(kind="stale", action="applied")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["target_id"], m.id)

    def test_suggested_merge_for_near_duplicates(self) -> None:
        self.store.upsert_memory(kind="fact", text="team uses Postgres for orders",
                                 importance=0.7, agent_id="bot", user_id="u1")
        self.store.upsert_memory(kind="fact", text="team uses Postgres for the orders table",
                                 importance=0.6, agent_id="bot", user_id="u1")
        rpt = cognitive_sleep(self.store, apply=False)
        # Should find a merge candidate
        self.assertGreaterEqual(rpt.counts["merge"], 1)


# ---------------------------------------------------------------------------
# Cognitive sleep observability through the HTTP route (Audit 2026-08-16)
# ---------------------------------------------------------------------------


class CognitiveSleepRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db = _new_store()
        # Wire the same store through the FastAPI app so the v1
        # cognitive route is exercised end-to-end.
        import tempfile as _tf
        from pathlib import Path as _P
        from fastapi.testclient import TestClient
        self._tmpdir = _P(_tf.mkdtemp(prefix="loop_route_"))
        # The store holds its own DB on disk; the app reads from
        # the same path so reads after writes stay consistent.
        from loop_memory.serve.app import create_app
        # ``create_app`` takes a MemoryStore, not a path; build
        # the store on the temp db and pass it in.
        from loop_memory.storage.sqlite_store import MemoryStore as _MS
        self.app = create_app(_MS(str(self.db)), scheduler=0)
        self.client = TestClient(self.app)

    def test_route_returns_stages_and_aborted_fields(self) -> None:
        self.store.upsert_memory(kind="fact", text="ok", importance=0.6)
        resp = self.client.post(
            "/api/v1/cognitive/sleep",
            json={"apply": False, "limit": 50},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # New fields must be present in the wire format.
        self.assertIn("stages", body)
        self.assertIn("aborted", body)
        self.assertIn("abort_reason", body)
        self.assertFalse(body["aborted"])

    def test_route_honours_explicit_deadline_seconds_zero(self) -> None:
        for i in range(5):
            self.store.upsert_memory(kind="fact", text=f"row {i}", importance=0.5)
        resp = self.client.post(
            "/api/v1/cognitive/sleep",
            json={"apply": False, "deadline_seconds": 0.0, "limit": 200},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["aborted"], body)
        self.assertTrue(body["abort_reason"].startswith("deadline exceeded"),
                        body["abort_reason"])


# ---------------------------------------------------------------------------
# Export / import / fork
# ---------------------------------------------------------------------------


class ExportImportForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = _new_store()

    def test_export_writes_bundle(self) -> None:
        self.store.upsert_wiki_page(slug="p", title="P", body="b",
                                     summary="s", tags=["t"], importance=0.7,
                                     scope="global")
        self.store.upsert_memory(kind="fact", text="x", importance=0.5,
                                  agent_id="bot", user_id="u1",
                                  external_id="x-1")
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "bundle"
            r = export_bundle(self.store, out, agent_id="bot", user_id="u1")
            self.assertEqual(r.memories, 1)
            self.assertTrue((out / "MEMORY.md").exists())
            self.assertTrue((out / "memories.jsonl").exists())
            self.assertTrue((out / "graph.json").exists())
            self.assertTrue((out / "INDEX.md").exists())
            self.assertTrue((out / "meta.json").exists())
            self.assertTrue((out / "pages" / "p.md").exists())

    def test_export_import_round_trip_is_idempotent(self) -> None:
        self.store.upsert_wiki_page(slug="p", title="P", body="b",
                                     summary="s", tags=["t"], importance=0.7,
                                     scope="global", key_facts=["p1"])
        self.store.upsert_memory(kind="fact", text="x", importance=0.5,
                                  agent_id="bot", user_id="u1",
                                  external_id="x-1")
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "bundle"
            export_bundle(self.store, out, agent_id="bot", user_id="u1")
            s2 = MemoryStore(Path(t) / "v7b.db")
            r1 = import_bundle(s2, out, agent_id="bot", user_id="u1")
            self.assertEqual(r1.pages_upserted, 1)
            self.assertEqual(r1.memories_upserted, 1)
            # Re-import is idempotent
            r2 = import_bundle(s2, out, agent_id="bot", user_id="u1")
            self.assertEqual(r2.pages_upserted, 1)
            self.assertEqual(r2.memories_upserted, 1)
            self.assertEqual(len(s2.list_memories(limit=20)), 1)

    def test_fork_snapshots_every_page(self) -> None:
        for slug in ("a", "b"):
            self.store.upsert_wiki_page(slug=slug, title=slug.upper(),
                                         body="x", summary="x", importance=0.5)
        r = fork_snapshot(self.store, branch_tag="tag-1")
        self.assertEqual(r["snapshotted"], 2)
        versions = self.store.list_wiki_versions(branch_tag="tag-1")
        self.assertEqual(len(versions), 2)


# ---------------------------------------------------------------------------
# SDK extensions
# ---------------------------------------------------------------------------


class SdkExtensionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = _new_store()
        self.client = MemoryClient.memory(self.store, agent_id="bot", user_id="alice")

    def test_namespace_sugar(self) -> None:
        alice = self.client.for_user("alice")
        m = alice.remember("alice prefers dark mode", external_id="pref-dark")
        self.assertEqual(m.user_id, "alice")
        m2 = self.client.for_agent("bot").remember("bot note", external_id="bot-1")
        self.assertEqual(m2.agent_id, "bot")

    def test_recall_adaptive(self) -> None:
        self.client.remember("Alice works on Atlas", external_id="alice-1")
        r = self.client.recall_adaptive("Alice", limit=5)
        self.assertIsNotNone(r)

    def test_cognitive_sleep_dry_run(self) -> None:
        self.client.remember("old news", external_id="old-1",
                              importance=0.1,
                              created_at=time.time() - 200*86400)
        rep = self.client.cognitive_sleep(apply=False, stale_days=90,
                                            min_score=0.5, min_importance=0.3)
        self.assertGreaterEqual(rep.counts["stale"], 1)

    def test_audit_round_trip(self) -> None:
        self.client.remember("old news", external_id="old-2",
                              importance=0.1,
                              created_at=time.time() - 200*86400)
        self.client.cognitive_sleep(apply=False, stale_days=90,
                                      min_score=0.5, min_importance=0.3)
        rows = self.client.audit(kind="stale", limit=10)
        self.assertGreaterEqual(len(rows), 1)

    def test_export_import(self) -> None:
        self.client.remember("x", external_id="x-1")
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "bundle"
            r = self.client.export(str(out), agent_id="bot", user_id="alice")
            self.assertEqual(r.memories, 1)
            s2 = MemoryStore(Path(t) / "v7c.db")
            c2 = MemoryClient.memory(s2)
            iv = c2.import_bundle(str(out), agent_id="bot", user_id="alice")
            self.assertEqual(iv.memories_upserted, 1)


# ---------------------------------------------------------------------------
# HTTP /api/v1/* routes (v7 surface)
# ---------------------------------------------------------------------------


class HttpV7RoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = _new_store()
        self.app = create_app(self.store, static_dir=None)
        self.c = TestClient(self.app)

    def test_graph_edges_route(self) -> None:
        r = self.c.post("/api/v1/graph/edges",
                         json={"src": "Alice", "dst": "Hangzhou",
                               "kind": "lives_in", "weight": 0.9})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["src"], "Alice")

    def test_graph_edges_rejects_distinct_names(self) -> None:
        r = self.c.post("/api/v1/graph/edges",
                         json={"src": "x", "dst": "x"})
        self.assertEqual(r.status_code, 400)

    def test_cognitive_sleep_route(self) -> None:
        self.c.post("/api/v1/memories",
                     json={"text": "old news", "importance": 0.1,
                           "agent_id": "bot", "external_id": "old-1",
                           "created_at": time.time() - 200*86400})
        r = self.c.post("/api/v1/cognitive/sleep",
                         json={"stale_days": 90, "min_score": 0.5,
                               "min_importance": 0.3})
        self.assertEqual(r.status_code, 200)
        self.assertIn("counts", r.json())

    def test_audit_route(self) -> None:
        r = self.c.get("/api/v1/cognitive/audit", params={"limit": 5})
        self.assertEqual(r.status_code, 200)
        self.assertIn("rows", r.json())

    def test_export_import_route(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "bundle"
            r = self.c.post("/api/v1/export",
                             json={"out_dir": str(out), "agent_id": "bot"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue((out / "MEMORY.md").exists())
            r2 = self.c.post("/api/v1/import",
                              json={"in_dir": str(out), "agent_id": "bot"})
            self.assertEqual(r2.status_code, 200)

    def test_fork_and_wiki_versions(self) -> None:
        self.c.post("/api/v1/export", json={"out_dir": "/tmp/lm_v7_bundle_test"})
        r = self.c.post("/api/v1/fork", json={"branch_tag": "test"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tag"], "test")
        r2 = self.c.get("/api/v1/wiki/versions",
                         params={"branch_tag": "test"})
        self.assertEqual(r2.status_code, 200)


# ---------------------------------------------------------------------------
# MCP v7 tools
# ---------------------------------------------------------------------------


class McpV7ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="loop_mcp_v7_"))
        self.db = self.tmp / "mcp_v7.db"
        self.prev_db = os.environ.get("LOOP_MEMORY_DB")
        self.prev_agent = os.environ.get("LOOP_MEMORY_AGENT_ID")
        os.environ["LOOP_MEMORY_DB"] = str(self.db)
        if self.prev_agent is None:
            os.environ.pop("LOOP_MEMORY_AGENT_ID", None)
        from loop_memory.storage.sqlite_store import MemoryStore
        s = MemoryStore(self.db)
        s.upsert_wiki_page(slug="atlas", title="Atlas",
                             body="Alice's project", summary="x",
                             tags=["atlas"], importance=0.8)

    def tearDown(self) -> None:
        import shutil
        if self.prev_db is None:
            os.environ.pop("LOOP_MEMORY_DB", None)
        else:
            os.environ["LOOP_MEMORY_DB"] = self.prev_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, name, arguments):
        from loop_memory.mcp import TOOL_DISPATCH
        result = TOOL_DISPATCH[name](arguments)
        self.assertEqual(len(result), 1)
        return result[0]["text"]

    def test_tools_list_contains_v7(self) -> None:
        from loop_memory.mcp import TOOLS
        names = {t["name"] for t in TOOLS}
        self.assertIn("remember_edge", names)
        self.assertIn("subgraph", names)
        self.assertIn("cognitive_sleep", names)
        self.assertIn("audit", names)

    def test_remember_edge_via_mcp(self) -> None:
        out = self._call("remember_edge", {
            "src": "Alice", "dst": "Hangzhou", "kind": "lives_in", "weight": 0.9,
        })
        self.assertIn("Alice", out)
        self.assertIn("Hangzhou", out)

    def test_cognitive_sleep_via_mcp(self) -> None:
        from loop_memory.storage.sqlite_store import MemoryStore
        MemoryStore(self.db).upsert_memory(
            kind="fact", text="old news", importance=0.1,
            created_at=time.time() - 200*86400,
        )
        out = self._call("cognitive_sleep", {"stale_days": 90,
                                              "min_score": 0.5,
                                              "min_importance": 0.3})
        self.assertIn("counts", out)

    def test_audit_via_mcp(self) -> None:
        from loop_memory.storage.sqlite_store import MemoryStore
        # Pre-seed an audit row so the tool returns the standard
        # "Audit (N rows)" header instead of the empty-state hint.
        MemoryStore(self.db).record_audit(
            kind="forget", action="applied", target_kind="memory",
            target_id="m1", reason="test", score=0.1,
        )
        out = self._call("audit", {"limit": 5})
        self.assertIn("Audit", out)


if __name__ == "__main__":
    unittest.main()

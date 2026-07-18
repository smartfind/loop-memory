"""Smoke tests for the 5-stage Evolution Consolidator + the new APIs."""
import os
import shutil
import tempfile
import unittest

from loop_memory.jobs.evolution import EvolutionConsolidator
from loop_memory.llm.providers import RuleBasedProvider
from loop_memory.storage.sqlite_store import MemoryStore


def _seed_store(path):
    store = MemoryStore(path)
    # wipe + recreate tables
    sid = store.upsert_session(
        source="test", external_id="t1",
        title="Test session", started_at=1700000000.0, ended_at=1700001000.0,
        message_count=10,
    ).id
    samples = [
        ("fact", 0.85, "User prefers dark mode and concise UI"),
        ("fact", 0.80, "User writes Python 3.14 code on macOS"),
        ("fact", 0.70, "Project loop-memory: local memory system"),
        ("episode", 0.55, "Outcome: shipped release 0.3.0 today"),
        ("episode", 0.45, "Debugged widget rendering bug"),
        ("fact", 0.40, "API keys live in ~/.loop_memory/secrets.json"),
        ("reflection", 0.50, "Noted: prefer Chinese-first i18n"),
        ("plan", 0.60, "Plan: add knowledge graph rotation"),
    ]
    for i, (kind, imp, text) in enumerate(samples):
        store.upsert_memory(
            kind=kind, text=text, importance=imp,
            source=f"test/{i}", session_id=sid,
            tags=[kind, "test"],
        )
    # Bump signals on one row so Stage 1 has something to do
    rows = store.list_memories(limit=20)
    if rows:
        store.record_signal(rows[0].id, recall=True)
        store.record_signal(rows[0].id, positive=True)
    return store


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = _seed_store(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_evolution_run_rule_based(self):
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        stats = ec.run(limit=50)
        self.assertGreater(stats.scanned, 0)
        self.assertGreater(stats.clusters, 0)
        self.assertGreater(stats.wiki_created, 0)
        # Stages dictionary has all 5 expected keys
        for k in ("score", "cluster", "distill", "wiki", "memo"):
            self.assertIn(k, stats.stages, f"missing stage {k}")
        # Pipeline runs were recorded
        runs = self.store.latest_pipeline_runs(limit=20)
        stages = {r["stage"] for r in runs}
        self.assertIn("score", stages)
        self.assertIn("wiki", stages)

    def test_pipeline_runs_track_in_out(self):
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        ec.run(limit=50)
        runs = self.store.latest_pipeline_runs(limit=20)
        # find wiki row
        wiki = next(r for r in runs if r["stage"] == "wiki")
        self.assertGreater(wiki["in_count"], 0)
        self.assertGreater(wiki["out_count"], 0)
        self.assertIsNotNone(wiki["finished_at"])

    def test_evolution_memo_persisted(self):
        ec = EvolutionConsolidator(self.store, RuleBasedProvider(), {})
        ec.run(limit=50)
        memo = self.store.get_setting("evolution_memo", "")
        self.assertTrue(memo and "rescored" in memo)

    def test_signal_bumping(self):
        mid = self.store.list_memories(limit=2)[1].id
        # start with zero recall
        self.assertEqual(self.store.get_signal(mid)["recall_count"], 0)
        self.store.bump_recalls([mid, mid, mid])
        self.assertEqual(self.store.get_signal(mid)["recall_count"], 3)
        self.assertIsNotNone(self.store.get_signal(mid)["last_recalled_at"])

    def test_top_signals(self):
        # Find a row that we haven't bumped yet (skip the seed-bumped row)
        rows_all = self.store.list_memories(limit=50)
        clean = None
        for m in rows_all:
            if self.store.get_signal(m.id)["recall_count"] == 0:
                clean = m.id; break
        self.assertTrue(clean, "expected at least one un-bumped row")
        self.store.bump_recalls([clean] * 3)
        top = self.store.top_signals(kind="recall_count", limit=5)
        self.assertGreaterEqual(len(top), 1)
        ours = next(r for r in top if r["id"] == clean)
        self.assertGreaterEqual(ours["recall_count"], 3)


if __name__ == "__main__":
    unittest.main()

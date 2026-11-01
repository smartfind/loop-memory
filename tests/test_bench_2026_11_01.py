"""Tests for the 0.4.18 retrieval-quality benchmark.

Audit 2026-11-01 (loop-memory 0.4.18).

The benchmark exists because Loop Memory shipped hybrid retrieval for
a dozen releases without ever *measuring* whether the lexical channel
ranked anything sensibly.  The methodology follows
``rohitg00/agentmemory`` v0.9.31 (2026-10-26): a fixed, versioned
corpus, the SHA-256 of the fixture bytes recorded in every report,
and an explicit statement of what the numbers do not prove.

These tests are the regression guard.  If a future change breaks the
ranking -- the exact-match rule, the replacement idf, the candidate
recall -- one of the metric assertions fails here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loop_memory.bench.runner import (
    DEGENERATE_SCORE_FLOOR,
    compare_reports,
    default_fixture_path,
    load_fixture,
    run_benchmark,
)

#: The SHA-256 of the shipped fixture.  Pinning it means the corpus
#: cannot change without the benchmark's own numbers being reviewed:
#: a silent edit to the fixture would otherwise quietly move the goal
#: posts.  Bump ``revision`` in the fixture *and* this constant
#: together.
FIXTURE_SHA256 = "417c4b5ff86a5f18d8bf1c5a540e6f9e7fcc022e5b360d3d91bb146610cddd67"
FIXTURE_REVISION = 2

#: The measured baseline for the shipped fixture (see CHANGELOG
#: 0.4.18).  Before the ranking fix the same fixture scored
#: MRR@10 = 0.6061, recall@1 = 0.2273, nDCG@10 = 0.7078.
BASELINE_MRR = 1.0
BASELINE_RECALL_AT_1 = 0.95


class FixtureTests(unittest.TestCase):
    """The shipped fixture is versioned and content-addressed."""

    def test_fixture_exists_and_parses(self) -> None:
        fixture, digest = load_fixture()
        self.assertEqual(fixture["revision"], FIXTURE_REVISION)
        self.assertEqual(digest, FIXTURE_SHA256)
        self.assertTrue(fixture["corpus"])
        self.assertTrue(fixture["wiki"])
        self.assertTrue(fixture["queries"])

    def test_fixture_sha256_matches_the_file_on_disk(self) -> None:
        raw = default_fixture_path().read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FIXTURE_SHA256)

    def test_every_query_names_known_documents(self) -> None:
        fixture, _ = load_fixture()
        known = {row["id"] for row in fixture["corpus"]}
        known |= {row["slug"] for row in fixture["wiki"]}
        for q in fixture["queries"]:
            for ref in q.get("relevant") or []:
                self.assertIn(ref, known, f"{q['id']} references an unknown document")

    def test_custom_fixture_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "custom",
                        "revision": 1,
                        "corpus": [{"id": "a", "kind": "fact", "text": "hello world"}],
                        "queries": [
                            {"id": "q", "query": "hello", "relevant": ["a"]}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            fixture, digest = load_fixture(path)
            self.assertEqual(fixture["name"], "custom")
            self.assertEqual(len(digest), 64)


class BenchmarkRunTests(unittest.TestCase):
    """The run itself."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_benchmark().to_dict()

    def test_report_records_the_fixture_identity(self) -> None:
        self.assertEqual(self.report["fixture_revision"], FIXTURE_REVISION)
        self.assertEqual(self.report["fixture_sha256"], FIXTURE_SHA256)

    def test_report_states_its_limitations(self) -> None:
        limits = " ".join(self.report["limitations"]).lower()
        self.assertIn("retrieval", limits)
        self.assertIn("not held-out answer accuracy", limits)
        self.assertIn("no provider api", limits)

    def test_metrics_meet_the_shipped_baseline(self) -> None:
        """The regression guard. A ranking regression moves these."""
        metrics = self.report["metrics"]
        self.assertGreaterEqual(metrics["mrr@10"], BASELINE_MRR)
        self.assertGreaterEqual(metrics["recall@1"], BASELINE_RECALL_AT_1)
        self.assertGreaterEqual(metrics["recall@3"], 1.0)

    def test_every_query_finds_its_relevant_document(self) -> None:
        for q in self.report["queries"]:
            self.assertIsNotNone(
                q["first_relevant_rank"],
                f"{q['query_id']} did not retrieve its relevant document",
            )
            self.assertEqual(q["first_relevant_rank"], 1)

    def test_the_run_is_deterministic(self) -> None:
        again = run_benchmark().to_dict()
        self.assertEqual(again["metrics"], self.report["metrics"])
        # Wiki pages get a generated uuid, so the identity of a wiki
        # hit differs between runs. What must be stable is the
        # *ordering* -- and that is expressed as the relevance
        # signature: for each query, which ranked slots hold a
        # relevant document.
        def _signature(report: dict) -> list[list[bool]]:
            return [
                [doc_id in set(q["relevant"]) for doc_id in q["ranked"]]
                for q in report["queries"]
            ]

        self.assertEqual(_signature(again), _signature(self.report))

    def test_the_fts5_score_collapse_is_detected(self) -> None:
        """The diagnostic that keeps the defect visible: at least one
        fixture query has every term clamped onto SQLite's 1e-6 idf
        floor."""
        self.assertGreaterEqual(self.report["degenerate_fts_queries"], 1)
        degenerate = [q for q in self.report["queries"] if q["degenerate_fts_score"]]
        for q in degenerate:
            self.assertLess(q["fts_score_max"], DEGENERATE_SCORE_FLOOR)
            # ...while the published score stays usable.
            self.assertGreater(q["score_max"], DEGENERATE_SCORE_FLOOR)

    def test_the_benchmark_builds_its_store_in_a_throwaway_directory(self) -> None:
        """The user's real database is never opened or written."""
        import loop_memory.bench.runner as runner

        created: list[str] = []
        original = runner.tempfile.mkdtemp

        def _spy(*args, **kwargs):
            path = original(*args, **kwargs)
            created.append(path)
            return path

        runner.tempfile.mkdtemp = _spy
        try:
            run_benchmark()
        finally:
            runner.tempfile.mkdtemp = original
        self.assertTrue(created)
        for path in created:
            self.assertFalse(Path(path).exists(), "the scratch store was not cleaned up")
            self.assertIn("loop-memory-bench", path)


class CompareTests(unittest.TestCase):
    """``compare_reports`` refuses to compare different fixtures."""

    def test_same_fixture_is_comparable(self) -> None:
        a = run_benchmark().to_dict()
        b = run_benchmark().to_dict()
        out = compare_reports(a, b)
        self.assertTrue(out["comparable"])
        for key in ("mrr@10", "recall@1"):
            self.assertAlmostEqual(out["metrics"][key], 0.0, places=6)

    def test_different_fixture_is_not_comparable(self) -> None:
        a = run_benchmark().to_dict()
        b = dict(a)
        b["fixture_sha256"] = "0" * 64
        out = compare_reports(a, b)
        self.assertFalse(out["comparable"])


class BenchCliTests(unittest.TestCase):
    """``loop-memory bench``."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        return subprocess.run(
            [sys.executable, "-m", "loop_memory.cli.main", "bench", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_json_output_is_a_valid_report(self) -> None:
        proc = self._run("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["fixture_sha256"], FIXTURE_SHA256)
        self.assertIn("metrics", payload)
        self.assertIn("queries", payload)

    def test_text_output_mentions_the_fixture_and_the_metrics(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sha256", proc.stdout)
        self.assertIn("mrr@10", proc.stdout)
        self.assertIn("limitations", proc.stdout)

    def test_out_writes_a_report_and_compare_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            proc = self._run("--json", "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.exists())
            payload = json.loads(out.read_text("utf-8"))
            self.assertEqual(payload["report"]["fixture_sha256"], FIXTURE_SHA256)

            proc2 = self._run("--compare", str(out))
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertIn("comparison vs baseline", proc2.stdout)


if __name__ == "__main__":
    unittest.main()

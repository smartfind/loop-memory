"""Regression tests for the recall token / char budget cap and the
unified ``level=0|1|2`` tiered-loader knob (audit 2026-10-05).

Source attribution: vectorize-io/hindsight v0.10.2 "concise
extraction by default" (Apache-2.0, 2026-09-29) +
aiming-lab/SimpleMem ICML'26 semantic-lossless-compression
(Apache-2.0, 2026-10-04) + mem0ai/mem0 v2.2.0 "User Profiles"
shape (Apache-2.0, 2026-09-23). The tiered-loader pattern
itself is the same idea as volcengine/OpenViking (AGPLv3,
2026-10-02) — only the pattern, no code copied.

Contract under test:
1. ``MemoryStore._truncate_to_budget()`` shrinks the joined
   memory+wiki list so the total ``text`` + ``body`` payload is
   <= ``max_chars`` (within ±1 char for the ellipsis terminator).
2. The cap is applied *last-first*: the lowest-ranked hit loses
   chars first so the top-ranked hits stay roomy.
4. ``max_chars=None`` (default) is byte-identical to the legacy
   payload shape.
5. ``level=0`` on ``recall()`` returns the L0 abstract ladder
   rung inline (titles + tags + abstract only, never full
   ``text`` / ``body``).
6. ``recall_paths(max_chars=...)`` caps the chip-stream total so a
   UI never has to hand-trim.
7. ``recall_hybrid(max_chars=...)`` honours the cap across the
   RRF-fused channel.
8. ``recall_as_of(max_chars=...)`` honours the cap across the
   bi-temporal channel.
9. The HTTP surface (``GET /api/recall?max_chars=...&level=...``
   and ``GET /api/recall/outline?max_chars=...``) threads both
   knobs through to the store.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app as build_app
from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> MemoryStore:
    tmp = Path(tempfile.mkdtemp(prefix="loop_budget_"))
    return MemoryStore(tmp / "budget.db")


def _seed_wiki_and_memories(store: MemoryStore) -> None:
    """Insert three wiki pages and two memories so a recall has a
    mixed list to truncate."""
    store.upsert_wiki_page(
        slug="pg-tuning",
        title="Postgres tuning notes",
        body=("PostgreSQL: prefer B-tree indexes on integer columns. "
              * 20),
        summary="How to tune Postgres for writes",
        importance=0.7,
        tags=["postgres", "db"],
    )
    store.upsert_wiki_page(
        slug="sqlite-busy",
        title="SQLite busy timeout",
        body=("SQLite: PRAGMA busy_timeout = 5000 retries internally. "
              * 12),
        summary="How to avoid SQLite EBUSY under contention",
        importance=0.6,
        tags=["sqlite", "db"],
    )
    store.upsert_wiki_page(
        slug="pytest",
        title="Pytest fixture order",
        body="pytest fixtures resolve in declaration order. " * 30,
        summary="Pytest fixture ordering rules",
        importance=0.5,
        tags=["pytest", "python"],
    )
    store.upsert_memory(
        kind="fact",
        text="we hit sqlite busy once when both watcher and serve "
             "wrote " * 40,
        importance=0.8,
        agent_id="bot",
        user_id="u1",
    )
    store.upsert_memory(
        kind="fact",
        text="short note about postgres tuning in prod " * 3,
        importance=0.5,
        agent_id="bot",
        user_id="u1",
    )


def _total_chars(result: dict) -> int:
    """Total payload (text + body) across a recall result's memories
    and wiki lists — mirrors what ``max_chars`` is supposed to bound."""
    total = 0
    total += sum(len(m.get("text") or "") for m in result.get("memories", []))
    total += sum(len(w.get("body") or "") for w in result.get("wiki", []))
    return total


class TruncateToBudgetHelperTests(unittest.TestCase):
    """Direct unit tests for ``_truncate_to_budget`` (the helper
    used by every ``recall*()`` method)."""

    def test_noop_when_max_chars_is_none(self) -> None:
        hits = [{"text": "hello world"}, {"text": "another body"}]
        out = MemoryStore._truncate_to_budget(hits, max_chars=None)
        self.assertEqual(out, hits)

    def test_noop_when_under_budget(self) -> None:
        hits = [{"text": "short"}, {"text": "also short"}]
        out = MemoryStore._truncate_to_budget(hits, max_chars=1000)
        self.assertEqual(out, hits)

    def test_noop_when_hits_is_empty(self) -> None:
        out = MemoryStore._truncate_to_budget([], max_chars=10)
        self.assertEqual(out, [])

    def test_truncation_preserves_top_ranked_above_floor(self) -> None:
        # Use 5 hits so the rank-based floor (top half = 2x equal
        # share, bottom half = MIN_KEEP) is reachable.
        hits = [
            {"text": "X" * 1000, "preview": ""},  # rank 0 — top
            {"text": "X" * 1000, "preview": ""},  # rank 1
            {"text": "X" * 1000, "preview": ""},  # rank 2
            {"text": "X" * 1000, "preview": ""},  # rank 3
            {"text": "X" * 1000, "preview": ""},  # rank 4 — bottom
        ]
        MemoryStore._truncate_to_budget(hits, max_chars=2000)
        # The total must respect the budget (or floor to MIN_KEEP*n).
        total = sum(len(h["text"]) for h in hits)
        self.assertLessEqual(total, 2000)
        # The top-ranked hit (rank 0) must be >= the bottom-ranked
        # hit (rank 4) — the algorithm never makes the top hit
        # smaller than the bottom hit.
        self.assertGreaterEqual(len(hits[0]["text"]), len(hits[4]["text"]))

    def test_total_within_budget(self) -> None:
        # 2 hits, MIN_KEEP=80 per hit, so the absolute minimum is 160.
        # Test the cap is respected when the budget >= that floor.
        hits = [
            {"text": "A" * 1000, "preview": ""},
            {"text": "B" * 1000, "preview": ""},
        ]
        for cap in (600, 300, 200, 160):
            MemoryStore._truncate_to_budget(hits, max_chars=cap)
            self.assertLessEqual(
                sum(len(h["text"]) for h in hits),
                cap,
                f"max_chars={cap} failed: {[len(h['text']) for h in hits]}",
            )

    def test_preserves_preview_invariance(self) -> None:
        hits = [{"text": "X" * 1000, "preview": ""}]
        MemoryStore._truncate_to_budget(hits, max_chars=100)
        # Preview must be <= the body it previews (or a sensible
        # abstract if the body was wiped).
        body = hits[0]["text"]
        prev = hits[0]["preview"]
        self.assertLessEqual(len(prev), max(240, len(body)))

    def test_terminal_ellipsis_on_truncated_text(self) -> None:
        hits = [{"text": "X" * 500, "preview": ""}]
        MemoryStore._truncate_to_budget(hits, max_chars=100)
        # Body is shorter than 500 now and carries the … terminator.
        self.assertTrue(hits[0]["text"].endswith("…"))


class RecallBudgetStoreTests(unittest.TestCase):
    """Store-level tests for ``recall()`` + ``recall_paths()`` +
    ``recall_hybrid()`` + ``recall_as_of()`` with the new
    ``max_chars`` parameter."""

    def setUp(self) -> None:
        self.store = _new_store()
        _seed_wiki_and_memories(self.store)

    def test_recall_default_is_byte_identical(self) -> None:
        """``max_chars=None`` (default) preserves the legacy payload
        shape byte-for-byte."""
        r1 = self.store.recall("sqlite")
        r2 = self.store.recall("sqlite", max_chars=None)
        self.assertEqual(
            [(m["id"], m["text"]) for m in r1["memories"]],
            [(m["id"], m["text"]) for m in r2["memories"]],
        )

    def test_recall_max_chars_caps_combined_payload(self) -> None:
        r = self.store.recall("sqlite", max_chars=600)
        # The cap is the *total* across memories + wiki combined,
        # NOT per-list (which would multiply it by 3).
        total = _total_chars(r)
        self.assertLessEqual(total, 600)

    def test_recall_max_chars_does_not_truncate_when_under(self) -> None:
        r = self.store.recall("sqlite", max_chars=10_000_000)
        total = _total_chars(r)
        # No truncation should occur; total should match a no-budget call.
        baseline = _total_chars(self.store.recall("sqlite"))
        self.assertEqual(total, baseline)

    def test_recall_level_zero_drops_full_body(self) -> None:
        r = self.store.recall("sqlite", level=0)
        for m in r["memories"]:
            self.assertEqual(m.get("text") or "", "")
        for w in r["wiki"]:
            self.assertEqual(w.get("body") or "", "")
            self.assertEqual(w.get("summary") or "", "")

    def test_recall_level_zero_returns_abstract(self) -> None:
        r = self.store.recall("sqlite", level=0)
        for m in r["memories"]:
            self.assertTrue(m.get("preview"), "preview must be set")
            self.assertLessEqual(len(m["preview"]), 240)
        for w in r["wiki"]:
            self.assertTrue(w.get("preview"), "preview must be set")

    def test_recall_level_one_is_default(self) -> None:
        """``level=1`` (default) keeps full text on memory hits and
        full body on wiki hits (modulo the existing 800-char cap on
        wiki body)."""
        r1 = self.store.recall("sqlite")
        r2 = self.store.recall("sqlite", level=1)
        self.assertEqual(
            [(m["id"], m["text"]) for m in r1["memories"]],
            [(m["id"], m["text"]) for m in r2["memories"]],
        )

    def test_recall_paths_max_chars_caps_abstract_stream(self) -> None:
        r = self.store.recall_paths("sqlite", max_chars=120)
        total = sum(len(m.get("abstract") or "") for m in r["memories"])
        total += sum(len(w.get("abstract") or "") for w in r["wiki"])
        self.assertLessEqual(total, 122)  # 80 mem abstract + 42 wiki summary

    def test_recall_paths_default_unchanged(self) -> None:
        r1 = self.store.recall_paths("sqlite")
        r2 = self.store.recall_paths("sqlite", max_chars=None)
        self.assertEqual(
            [(m["id"], m.get("abstract")) for m in r1["memories"]],
            [(m["id"], m.get("abstract")) for m in r2["memories"]],
        )

    def test_recall_hybrid_max_chars_caps_combined_payload(self) -> None:
        r = self.store.recall_hybrid("sqlite", max_chars=600)
        total = _total_chars(r)
        self.assertLessEqual(total, 600)

    def test_recall_as_of_max_chars_caps_combined_payload(self) -> None:
        ts = time.time() + 60  # future timestamp — all rows visible
        r = self.store.recall_as_of("sqlite", ts, max_chars=600)
        total = _total_chars(r)
        self.assertLessEqual(total, 600)

    def test_recall_as_of_level_zero_drops_body(self) -> None:
        ts = time.time() + 60
        r = self.store.recall_as_of("sqlite", ts, level=0)
        for m in r["memories"]:
            self.assertEqual(m.get("text") or "", "")
        for w in r["wiki"]:
            self.assertEqual(w.get("body") or "", "")


class RecallBudgetRouteTests(unittest.TestCase):
    """HTTP route tests for the new ``max_chars`` and ``level``
    query parameters on ``GET /api/recall`` and
    ``GET /api/recall/outline``."""

    def setUp(self) -> None:
        self.store = _new_store()
        _seed_wiki_and_memories(self.store)
        app: FastAPI = build_app(store=self.store)  # type: ignore[arg-type]
        self.client = TestClient(app)

    def test_api_recall_honours_max_chars(self) -> None:
        resp = self.client.get(
            "/api/recall",
            params={"query": "sqlite", "max_chars": 600},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["max_chars"], 600)
        total = _total_chars(body)
        self.assertLessEqual(total, 600)

    def test_api_recall_honours_level_zero(self) -> None:
        resp = self.client.get(
            "/api/recall",
            params={"query": "sqlite", "level": 0},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["level"], 0)
        for m in body.get("memories", []):
            self.assertEqual(m.get("text") or "", "")
        for w in body.get("wiki", []):
            self.assertEqual(w.get("body") or "", "")

    def test_api_recall_max_chars_zero_means_no_cap(self) -> None:
        resp = self.client.get(
            "/api/recall",
            params={"query": "sqlite", "max_chars": 0},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # No cap should produce the same payload as the no-arg call.
        baseline = self.client.get(
            "/api/recall",
            params={"query": "sqlite"},
        ).json()
        self.assertEqual(
            [(m["id"], m["text"]) for m in body["memories"]],
            [(m["id"], m["text"]) for m in baseline["memories"]],
        )

    def test_api_recall_outline_honours_max_chars(self) -> None:
        resp = self.client.get(
            "/api/recall/outline",
            params={"query": "sqlite", "max_chars": 120},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["max_chars"], 120)
        total = sum(len(m.get("abstract") or "")
                    for m in body.get("memories", []))
        total += sum(len(w.get("abstract") or "")
                     for w in body.get("wiki", []))
        self.assertLessEqual(total, 122)  # 80 mem abstract + 42 wiki summary


class RecallBudgetCliTests(unittest.TestCase):
    """CLI surface tests for the new ``--max-chars`` and ``--level``
    flags on ``loop-memory recall`` and ``recall-paths``."""

    def setUp(self) -> None:
        self.store = _new_store()
        _seed_wiki_and_memories(self.store)
        import os
        self._saved_env = os.environ.get("LOOP_MEMORY_DB")
        os.environ["LOOP_MEMORY_DB"] = str(self.store.path)

    def tearDown(self) -> None:
        import os
        if self._saved_env is None:
            os.environ.pop("LOOP_MEMORY_DB", None)
        else:
            os.environ["LOOP_MEMORY_DB"] = self._saved_env

    def test_cli_recall_max_chars(self) -> None:
        from loop_memory.cli.commands.read import run_recall
        import io
        import contextlib
        buf = io.StringIO()
        rc = -1
        with contextlib.redirect_stdout(buf):
            rc = run_recall(["sqlite", "--max-chars", "300"])
        self.assertEqual(rc, 0, buf.getvalue())
        # The CLI prints bodies inline; verify it didn't crash with
        # the new flag and that no Python-level exception leaked.
        self.assertIn("Distilled knowledge", buf.getvalue())

    def test_cli_recall_level_zero(self) -> None:
        from loop_memory.cli.commands.read import run_recall
        import io
        import contextlib
        buf = io.StringIO()
        rc = -1
        with contextlib.redirect_stdout(buf):
            rc = run_recall(["sqlite", "--level", "0"])
        self.assertEqual(rc, 0, buf.getvalue())
        # Level=0 sets text= on every memory and body=summary= on
        # every wiki, so the CLI's preview-line branch is the
        # only thing that prints. Just verify the run succeeded
        # and the Distilled knowledge section header still appears.
        self.assertIn("Distilled knowledge", buf.getvalue())

    def test_cli_recall_invalid_level(self) -> None:
        from loop_memory.cli.commands.read import run_recall
        import io
        import contextlib
        buf = io.StringIO()
        rc = -1
        with contextlib.redirect_stdout(buf):
            rc = run_recall(["sqlite", "--level", "9"])
        # Bad level → die() returns 1; the message should reach stderr.
        self.assertEqual(rc, 2)  # die() default code is 2

    def test_cli_recall_paths_max_chars(self) -> None:
        from loop_memory.cli.commands.read import run_recall_paths
        import io
        import contextlib
        buf = io.StringIO()
        rc = -1
        with contextlib.redirect_stdout(buf):
            rc = run_recall_paths(["sqlite", "--max-chars", "100"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("Outline", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

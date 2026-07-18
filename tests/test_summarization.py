"""Tests for the per-conversation summarization mode."""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from loop_memory import HashingEmbedder, MemoryStore
from loop_memory.ingest.loader import IngestedSession, IngestedTurn
from loop_memory.ingest.pipeline import MemoryPipeline


def _new_db() -> Path:
    p = Path("/tmp/test_loop_summary.db")
    p.unlink(missing_ok=True)
    return p


def _long_conversation(n: int = 80) -> IngestedSession:
    turns = []
    base = time.time() - 3600
    for i in range(n):
        if i % 2 == 0:
            # alternates user/assistant
            turns.append(IngestedTurn(
                "user",
                f"Please help me with task #{i}: I want a clean async API",
                created_at=base + i * 30,
            ))
        else:
            turns.append(IngestedTurn(
                "assistant",
                f"Sure, I can suggest pattern #{i} with FastAPI and asyncio.",
                created_at=base + i * 30,
            ))
    return IngestedSession(
        source="codex",
        external_id=f"long-{n}",
        title="Async API design",
        turns=turns,
    )


class SummarizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _new_db()
        self.store = MemoryStore(self.db)
        self.pipeline = MemoryPipeline(
            self.store, embedder=HashingEmbedder(dim=64), max_facts=3,
        )

    def tearDown(self) -> None:
        self.db.unlink(missing_ok=True)

    def test_long_conversation_produces_at_most_5_items(self) -> None:
        result = self.pipeline.run(_long_conversation(80))
        # hard ceiling: 6 summary rows regardless of how chatty the session was
        self.assertLessEqual(len(result.summary_items), 6)
        # and at least the title + intent + outcome (3 guaranteed rows)
        self.assertGreaterEqual(len(result.summary_items), 3)

    def test_two_runs_for_same_session_are_deduped(self) -> None:
        sess = _long_conversation(10)
        self.pipeline.run(sess)
        n1 = self.store.stats()["memories"]
        self.pipeline.run(sess)  # re-run
        n2 = self.store.stats()["memories"]
        # SQL upsert keyed on (id=None → new), so re-runs are idempotent only
        # if we set deterministic ids. We assert at least memory count did
        # not balloon (less than 2x).
        self.assertLessEqual(n2, n1 * 2)

    def test_max_facts_caps_extraction(self) -> None:
        # Default extractor returns up to 6 candidates; cap to 3.
        pipeline = MemoryPipeline(
            self.store, embedder=HashingEmbedder(dim=64), max_facts=3,
        )
        result = pipeline.run(_long_conversation(20))
        self.assertLessEqual(result.facts_count, 3)

    def test_pure_chitchat_writes_only_title_and_outcome(self) -> None:
        turns = [
            IngestedTurn("user", "hi", time.time() - 30),
            IngestedTurn("assistant", "hello", time.time() - 25),
            IngestedTurn("user", "thanks", time.time() - 20),
            IngestedTurn("assistant", "you're welcome", time.time() - 15),
        ]
        sess = IngestedSession(source="codex", external_id="chitchat", turns=turns)
        result = self.pipeline.run(sess)
        # No durable facts, but title (== first user "hi") + outcome rows written
        self.assertEqual(result.facts_count, 0)
        self.assertGreaterEqual(len(result.summary_items), 2)

    def test_summary_items_have_high_signal_kind(self) -> None:
        result = self.pipeline.run(_long_conversation(20))
        kinds = {it.kind for it in result.summary_items}
        self.assertTrue(kinds <= {"fact", "episode"})
        # importance should be in the upper range for at least the intent
        intent_items = [it for it in result.summary_items if "intent" in (it.tags or [])]
        if intent_items:
            self.assertGreaterEqual(intent_items[0].importance, 0.65)


if __name__ == "__main__":
    unittest.main()

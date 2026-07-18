"""Tests for the ingest layer (loader + pipeline)."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from loop_memory import HashingEmbedder, MemoryStore
from loop_memory.ingest.loader import ClaudeLoader, CodexLoader, HermesLoader
from loop_memory.ingest.pipeline import MemoryPipeline


def _tmp_db() -> Path:
    p = Path("/tmp/test_loop_memory_ingest.db")
    p.unlink(missing_ok=True)
    return p


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _tmp_db()
        self.store = MemoryStore(self.db)
        self.pipeline = MemoryPipeline(self.store, embedder=HashingEmbedder(dim=64))

    def tearDown(self) -> None:
        self.db.unlink(missing_ok=True)

    def test_codex_loader_parses_json(self) -> None:
        data = [
            {"role": "user", "content": "hi, I'm Mia.", "ts": 1_700_000_000},
            {"role": "assistant", "content": "hey Mia, what can I do?", "ts": 1_700_000_010},
        ]
        path = Path("/tmp/codex_test.json")
        path.write_text(json.dumps(data))
        try:
            sess = CodexLoader().load_one(path)
            self.assertIsNotNone(sess)
            self.assertEqual(sess.source, "codex")
            self.assertEqual(len(sess.turns), 2)
            self.assertEqual(sess.turns[0].role, "user")
            self.assertEqual(sess.title, "hi, I'm Mia.")
        finally:
            path.unlink(missing_ok=True)

    def test_claude_loader_parses_jsonl(self) -> None:
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Hi there"}}),
        ]
        path = Path("/tmp/claude_test.jsonl")
        path.write_text("\n".join(lines))
        try:
            sess = ClaudeLoader().load_one(path)
            self.assertIsNotNone(sess)
            self.assertEqual(sess.source, "claude")
            self.assertEqual(len(sess.turns), 2)
        finally:
            path.unlink(missing_ok=True)

    def test_hermes_loader_parses_jsonl(self) -> None:
        lines = [
            json.dumps({"role": "user", "content": "ping"}),
            json.dumps({"role": "assistant", "content": "pong"}),
        ]
        path = Path("/tmp/hermes_test.jsonl")
        path.write_text("\n".join(lines))
        try:
            sess = HermesLoader().load_one(path)
            self.assertIsNotNone(sess)
            self.assertEqual(sess.source, "hermes")
        finally:
            path.unlink(missing_ok=True)

    def test_pipeline_writes_session_and_facts(self) -> None:
        from loop_memory.ingest.loader import IngestedSession, IngestedTurn
        sess = IngestedSession(
            source="codex",
            external_id="manual",
            title="manual session",
            turns=[
                IngestedTurn("user", "I love hiking and dislike traffic.", time.time()),
                IngestedTurn("assistant", "Noted.", time.time()),
            ],
        )
        result = self.pipeline.run(sess)
        # new pipeline: per-session summary, not per-turn
        self.assertGreaterEqual(result.facts_count, 1)
        self.assertGreaterEqual(len(result.summary_items), 3)
        self.assertEqual(self.store.stats()["memories"] > 0, True)


if __name__ == "__main__":
    unittest.main()

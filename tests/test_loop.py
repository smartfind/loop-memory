"""Smoke tests covering the canonical Retrieve → Generate → Reflect → Store loop."""

from __future__ import annotations

import unittest

from loop_memory import EchoLLM, HashingEmbedder, LongTermMemory, LoopEngine, MemoryItem


class LoopTests(unittest.TestCase):
    def test_recall_personal_facts(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=128))
        engine.turn("Hi, my name is Mia.")
        engine.turn("What's my name?")

        hits = engine.recall("name")
        joined = " ".join(item.text.lower() for item in hits)
        self.assertIn("mia", joined)
        self.assertGreater(len(engine.long), 0)

    def test_dedup(self) -> None:
        ltm = LongTermMemory()
        a = MemoryItem(text="User loves matcha.", importance=0.7)
        a.embedding = [1.0, 0.0]
        b = MemoryItem(text="User loves matcha.", importance=0.8)
        b.embedding = [0.99, 0.01]
        ltm.add(a)
        ltm.add(b)
        self.assertEqual(len(ltm), 1, "near-duplicate should be merged")

    def test_plan_lifecycle(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=64))
        goal = engine.push_plan("Plan a trip to Hangzhou.")
        self.assertIsNotNone(engine.procedural.current())
        done = engine.complete_current_plan()
        self.assertEqual(done.text, goal.text)
        self.assertIsNone(engine.procedural.current())

    def test_expiry_gc(self) -> None:
        ltm = LongTermMemory()
        fresh = MemoryItem(text="current", importance=0.5, ttl=10_000)
        fresh.embedding = [1.0, 0.0]
        ltm.add(fresh)
        ltm.gc()
        self.assertEqual(len(ltm), 1)

    def test_diagnostics_shape(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=64))
        result = engine.turn("I like cats.")
        self.assertIn("retrieved", result.diagnostics)
        self.assertIn("long_term_size", result.diagnostics)
        self.assertIn("gen_ms", result.diagnostics)


if __name__ == "__main__":
    unittest.main()

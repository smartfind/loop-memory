"""Tests for the reflection / summarization stages."""

from __future__ import annotations

import unittest

from loop_memory import EchoLLM, HashingEmbedder, LoopEngine


class ReflectionTests(unittest.TestCase):
    def test_name_extracted_as_fact(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=64))
        engine.turn("Hello! My name is Aiden.")
        joined = "\n".join(it.text for it in engine.long.search())
        self.assertIn("Aiden", joined)

    def test_preferences_extracted(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=64))
        engine.turn("I really love hiking and I dislike crowded places.")
        joined = "\n".join(it.text.lower() for it in engine.long.search())
        self.assertTrue("hiking" in joined or "love" in joined)

    def test_custom_reflector_overrides_default(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=64))
        from loop_memory.memory.types import MemoryItem

        seen: list[str] = []

        def custom_reflector(text: str):
            seen.append(text)
            return [MemoryItem(text=f"reflected: {text}", importance=0.9)]

        engine.set_reflector(custom_reflector)
        engine.turn("hello world")
        # set_reflector stays in effect for subsequent turns
        engine.turn("again")
        self.assertEqual(seen, ["hello world", "again"])
        # The custom factory's output was stored, so it should be retrievable.
        joined = "\n".join(it.text for it in engine.long.search())
        self.assertIn("reflected: hello world", joined)

    def test_scratchpad_compaction(self) -> None:
        # Force compaction by setting capacity = 2.
        engine = LoopEngine(
            llm=EchoLLM(),
            embedder=HashingEmbedder(dim=64),
            short_capacity=2,
            compact_at=2,
        )
        # Two user/assistant pairs fill 4 items > capacity; trigger compaction.
        engine.turn("first")
        engine.turn("second")
        # After reaching compact_at, scratchpad should be cleared and
        # a single summary should have been pushed into long-term.
        summary_hits = [
            it for it in engine.long.search()
            if it.kind == "reflection" and it.text.startswith("summary:")
        ]
        self.assertGreaterEqual(len(summary_hits), 1)

    def test_invalid_input_rejected(self) -> None:
        engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=64))
        with self.assertRaises(ValueError):
            engine.turn("")
        with self.assertRaises(ValueError):
            engine.turn("   ")

    def test_missing_llm_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LoopEngine(llm=None, embedder=HashingEmbedder(dim=64))  # type: ignore[arg-type]

"""@agent_memory decorator + remember() helper (audit 2026-10-18).

The decorator wraps a callable so every invocation reads relevant
memories before running the body and writes a fresh interaction
memory after. ``remember()`` is a sugar for one-shot seeding.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from loop_memory.decorators import agent_memory, remember
from loop_memory.storage.sqlite_store import MemoryStore


class RememberHelperTests(unittest.TestCase):
    """``remember(store, text, ...)`` writes a single memory and returns its id."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "rem.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remember_returns_id(self) -> None:
        mid = remember(self.store, "User prefers dark mode")
        self.assertIsInstance(mid, str)
        self.assertTrue(mid)
        item = self.store.get_memory(mid)
        self.assertIsNotNone(item)
        self.assertEqual(item.text, "User prefers dark mode")

    def test_remember_sets_tags_and_source(self) -> None:
        mid = remember(self.store, "Project uses blue/green deploy",
                      tags=["deploy", "infra"])
        item = self.store.get_memory(mid)
        self.assertIn("deploy", item.tags)
        self.assertIn("infra", item.tags)
        self.assertEqual(item.source, "remember")

    def test_remember_requires_memory_store(self) -> None:
        with self.assertRaises(TypeError):
            remember("not-a-store", "hello")

    def test_remember_uses_default_kind(self) -> None:
        mid = remember(self.store, "fact text")
        item = self.store.get_memory(mid)
        self.assertEqual(item.kind, "fact")

    def test_remember_dedup_collapses_identical(self) -> None:
        """Audit 2026-10-18: remember() is built on upsert_memory
        so the content-hash dedup kicks in for byte-identical text."""
        a = remember(self.store, "Same fact")
        b = remember(self.store, "Same fact")
        self.assertEqual(a, b)


class AgentMemoryDecoratorTests(unittest.TestCase):
    """``@agent_memory(store, ...)`` wrapping a callable."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "deco.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_injected_via_memory_context_kwarg(self) -> None:
        remember(self.store, "Deploy process uses blue/green", tags=["deploy"])
        remember(self.store, "User prefers dark mode", tags=["ui"])

        captured = {}

        @agent_memory(self.store, agent_id="codex")
        def ask(question: str, memory_context: str = "") -> str:
            captured["memory_context"] = memory_context
            return f"answer to: {question}"

        result = ask("What is the deploy process?")
        self.assertIn("memory_context", captured,
                      "decorator must inject the recall")
        self.assertIn("Deploy", captured["memory_context"])
        self.assertIn("answer to:", result)

    def test_post_call_stores_interaction(self) -> None:
        @agent_memory(self.store, agent_id="codex")
        def ask(question: str, memory_context: str = "") -> str:
            return f"echo:{question}"

        before = len(self.store.list_memories(limit=100, kind="interaction"))
        ask("hello world")
        ask("good morning")
        after = len(self.store.list_memories(limit=100, kind="interaction"))
        self.assertEqual(after - before, 2,
                         "every call writes a new interaction row")

    def test_dict_return_gets_memories_attached(self) -> None:
        remember(self.store, "Some background fact")

        @agent_memory(self.store, agent_id="codex")
        def search(query: str) -> dict:
            return {"query": query, "answer": "found"}

        result = search("background")
        self.assertIn("query", result)
        self.assertIn("answer", result)
        self.assertIn("memories", result,
                      "dict return surfaces the recall list")
        self.assertIsInstance(result["memories"], list)

    def test_plain_string_return_leaves_recall_alone(self) -> None:
        """A non-dict return stays untouched -- no surprise mutation
        of the caller's value."""
        remember(self.store, "Background context")

        @agent_memory(self.store, agent_id="codex")
        def plain(q: str) -> str:
            return q.upper()

        result = plain("test")
        self.assertEqual(result, "TEST")
        self.assertNotIn("memories", result)  # str doesn't get dict keys

    def test_no_string_arg_skips_store(self) -> None:
        """``plain(21)`` has no string query, so no interaction row
        is written. The decorator stays invisible to numeric-only
        callers."""
        @agent_memory(self.store, agent_id="codex")
        def double(x: int) -> int:
            return x * 2

        before = len(self.store.list_memories(limit=100, kind="interaction"))
        for i in range(3):
            self.assertEqual(double(i), i * 2)
        after = len(self.store.list_memories(limit=100, kind="interaction"))
        self.assertEqual(before, after,
                         "no string arg -> no interaction written")

    def test_dict_return_with_no_recall_has_no_memories_key(self) -> None:
        """When recall returns nothing, ``memories`` should NOT be
        injected (the wrapper only attaches when there are hits)."""

        @agent_memory(self.store, agent_id="codex")
        def noop(query: str) -> dict:
            return {"echo": query}

        result = noop("zzzzzqqqq-no-such-thing")
        self.assertNotIn("memories", result,
                         "no hits -> no surprise key on the dict")

    def test_recall_failure_does_not_break_body(self) -> None:
        """If the store raises during recall, the body still runs."""

        class _BrokenStore(MemoryStore):
            def recall_hybrid(self, *args, **kw):
                raise RuntimeError("simulated store outage")

        store = _BrokenStore(os.path.join(self.tmp, "broken.db"))

        @agent_memory(store, agent_id="codex")
        def ask(question: str, memory_context: str = "") -> str:
            return f"ok:{question}"

        # Body runs even though recall_hybrid raises
        self.assertEqual(ask("hello"), "ok:hello")

    def test_store_failure_after_body_does_not_break_return(self) -> None:
        """If the store raises while writing the post-call memory,
        the wrapper still returns the body's result."""

        class _BrokenStore(MemoryStore):
            def upsert_memory(self, *args, **kw):
                raise RuntimeError("simulated store outage")

        store = _BrokenStore(os.path.join(self.tmp, "broken2.db"))

        @agent_memory(store, agent_id="codex")
        def ask(question: str, memory_context: str = "") -> str:
            return f"ok:{question}"

        self.assertEqual(ask("hello"), "ok:hello")

    def test_decorator_requires_memory_store(self) -> None:
        with self.assertRaises(TypeError):
            @agent_memory("not-a-store")
            def f():
                return 1

    def test_limit_validation(self) -> None:
        with self.assertRaises(ValueError):
            @agent_memory(self.store, limit=0)
            def f():
                return 1
        with self.assertRaises(ValueError):
            @agent_memory(self.store, limit=51)
            def f():
                return 1

    def test_metadata_attribute_set(self) -> None:
        """The wrapper carries ``__wrapped_agent_memory__`` so a
        meta-classifier (e.g. ``doctor``) can spot decorated callables."""

        @agent_memory(self.store)
        def f():
            return 1

        self.assertTrue(getattr(f, "__wrapped_agent_memory__", False))

    def test_recall_limit_respected(self) -> None:
        for i in range(10):
            remember(self.store, f"Background fact number {i}")

        @agent_memory(self.store, agent_id="codex", limit=3)
        def ask(question: str, memory_context: str = "") -> int:
            return memory_context.count("\n") + (1 if memory_context else 0)

        # The context string should contain at most 3 lines + some
        # preamble. The exact count depends on BM25 ranking -- we
        # only assert the limit is being applied (fewer than 10
        # context lines from 10 stored facts).
        result = ask("Background")
        self.assertLessEqual(result, 5)


class AgentMemoryMultiTurnTests(unittest.TestCase):
    """A multi-turn scenario where each call sees prior calls' memory."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "multi.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_later_calls_recall_earlier_interactions(self) -> None:
        """Multi-turn scenario: the second call's recall list contains
        the first call's stored interaction (when the query has
        enough lexical overlap with the stored text).

        We seed a custom fact in addition to the prior interaction so
        the BM25 fallback actually surfaces it -- otherwise the FTS5
        trigram tokenizer might miss the exact-match.
        """
        # Pre-seed a fact that the next call will recall.
        remember(self.store, "Previous deploy ran at 14:00 today",
                 tags=["history"])

        @agent_memory(self.store, agent_id="codex")
        def ask(question: str, memory_context: str = "") -> str:
            return f"echo:{question}"

        # First call: should recall nothing (store only has the
        # pre-seeded fact that doesn't match BM25) and write a new
        # interaction row.
        ask("what time was previous deploy?")
        first_call_interactions = self.store.list_memories(
            limit=10, kind="interaction"
        )
        self.assertEqual(len(first_call_interactions), 1)
        # Second call: should recall the first call's interaction
        # (stored text shares "deploy" with the new query).
        captured = {}

        @agent_memory(self.store, agent_id="codex")
        def ask2(question: str, memory_context: str = "") -> str:
            captured["mc"] = memory_context
            return f"echo:{question}"

        ask2("any update on deploy?")
        self.assertIn("mc", captured)
        self.assertIn("what time was previous deploy", captured["mc"],
                      "earlier interaction surfaces in later recall")


if __name__ == "__main__":
    unittest.main()

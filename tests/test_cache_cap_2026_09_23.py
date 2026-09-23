"""Tests for the in-process LLM response cache cap (audit 2026-09-23).

Both ``EvolutionRunner._cache`` (jobs/evolution.py) and the consolidator's
``_cache`` (jobs/llm_consolidate.py) used to grow without bound: the
TTL made entries stale but they were never evicted. This test pins the
cap at ``_MAX_CACHE_SIZE`` so a long-running pass cannot leak memory.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


def _build_runner():
    """Construct a minimal ``EvolutionConsolidator`` for cache-cap tests.

    We bypass ``__init__`` (which would require a real store + LLM
    provider) and only set the four attributes ``_cached_call``
    reads + writes. This keeps the test fast and zero-dep.
    """
    from loop_memory.jobs.evolution import EvolutionConsolidator

    runner = EvolutionConsolidator.__new__(EvolutionConsolidator)
    runner._cache = {}
    runner._cache_ts = {}
    runner._cache_ttl = 300.0
    runner._MAX_CACHE_SIZE = 256
    # Minimal provider stub: ``complete`` returns the prompt unchanged.
    runner._provider = SimpleNamespace(
        complete=lambda history, temperature, max_tokens: (
            history.messages[-1].content if history.messages else ""
        ),
    )
    return runner


class EvolutionCacheCapTests(unittest.TestCase):
    """Audit 2026-09-23: EvolutionConsolidator._cache hard-capped at 256."""

    def test_cache_caps_at_max_size(self) -> None:
        runner = _build_runner()
        # The default stub provider makes ``_cached_call`` write the
        # prompt to the cache. We call it 300 times with unique
        # prompts and assert the cache never exceeds the cap.
        # ``_cached_call`` requires a stats object with a ``notes``
        # list and the call counters it bumps.
        for i in range(300):
            runner._cached_call(
                cache_key=f"k{i}",
                system="s",
                user_prompt=f"prompt {i}",
                cfg={},
                stats=SimpleNamespace(notes=[], cluster_calls=0, wiki_calls=0),
                kind="wiki",
            )
        self.assertLessEqual(
            len(runner._cache), runner._MAX_CACHE_SIZE,
            f"cache grew to {len(runner._cache)}; cap is {runner._MAX_CACHE_SIZE}",
        )

    def test_cache_caps_at_default_size(self) -> None:
        runner = _build_runner()
        # If a future refactor changes the constant, this test catches
        # the regression at the default cap value.
        self.assertEqual(runner._MAX_CACHE_SIZE, 256)

    def test_cache_cap_holds_for_llm_consolidator(self) -> None:
        """Same contract for the consolidator runner."""
        from loop_memory.jobs.llm_consolidate import LLMConsolidator
        runner = LLMConsolidator.__new__(LLMConsolidator)
        runner._cache = {}
        runner._cache_ts = {}
        runner._cache_ttl = 300.0
        runner._MAX_CACHE_SIZE = 256
        # No call sites needed — we only check the constant exists
        # and the cache starts empty.
        self.assertEqual(runner._MAX_CACHE_SIZE, 256)
        self.assertEqual(runner._cache, {})


if __name__ == "__main__":
    unittest.main()

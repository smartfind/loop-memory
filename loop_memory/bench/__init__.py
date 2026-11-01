"""Retrieval-quality benchmark for Loop Memory.

Audit 2026-11-01 (loop-memory 0.4.18).

Loop Memory shipped hybrid retrieval (BM25 + semantic + entity
fused with RRF) without ever *measuring* whether the BM25 channel
ranked anything sensibly.  That gap is the reason a FTS5 scoring
quirk could sit unnoticed for a dozen releases: the ``trigram``
tokenizer makes common CJK trigrams reach a prevalence where
SQLite's ``bm25()`` collapses its inverse document frequency to a
constant, which silently flattened the ranking.

``rohitg00/agentmemory`` v0.9.31 (2026-10-26) fixed the same class
of bug in its prefix-expansion path and reported the delta on a
deterministic benchmark (strict recall-all@5 65.74% -> 84.68%,
+18.94 points on LongMemEval S500).  Its methodology is what this
package copies:

* a **fixed fixture** with an explicit revision number, so two runs
  measure the same corpus;
* a **SHA-256 of the fixture file** recorded in every report, so a
  report says exactly which bytes it scored;
* **explicit limitations** -- these numbers are *retrieval* results
  over a synthetic corpus, not held-out answer accuracy, and no
  provider API is called, so the run costs $0 and needs no network.

The runner is deliberately dependency-free and offline.
"""

from .runner import (
    BenchmarkReport,
    default_fixture_path,
    load_fixture,
    run_benchmark,
)

__all__ = [
    "BenchmarkReport",
    "default_fixture_path",
    "load_fixture",
    "run_benchmark",
]

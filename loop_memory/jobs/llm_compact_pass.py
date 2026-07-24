"""Optional LLM-driven memory fusion.

Placeholder for the moment — the wiki consolidator already produces
high-quality distilled pages. Re-implementing memory-layer fusion here
would burn the same context twice. Keep this module around so the
``Compactor`` config knob can switch modes without an import error.
"""
from __future__ import annotations

from typing import Any

from ..storage.sqlite_store import MemoryStore


def llm_fuse_pass(store: MemoryStore, *, force: bool = False) -> int:
    """Stub. Returns 0. The real implementation lives in
    :mod:`jobs.evolution` — this pass is intentionally a no-op until
    we have evidence the heuristic layer is leaving valuable signal
    on the floor.
    """
    return 0


__all__ = ["llm_fuse_pass"]
